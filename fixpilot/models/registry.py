"""Model registry and capability metadata.

FixPilot is *local-first*: the default path is an open-source model running
through Ollama on the paired workstation.  Cloud OpenAI-compatible endpoints
are optional escalation targets and are always marked as leaving the machine.
The deterministic core engine is registered as a first-class provider so the
agent still works with **zero** models installed (useful offline, in CI, and
for demos).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Task taxonomy the router reasons about.
TASK_TRIAGE = "triage"
TASK_ROOT_CAUSE = "root_cause"
TASK_PATCH = "patch_generation"
TASK_TEST_REPAIR = "test_repair"
TASK_EXPLAIN = "explain"
TASK_SUMMARIZE = "summarize"
TASK_PLAN = "plan"
TASK_VISION = "vision"

TASKS = (
    TASK_TRIAGE,
    TASK_ROOT_CAUSE,
    TASK_PATCH,
    TASK_TEST_REPAIR,
    TASK_EXPLAIN,
    TASK_SUMMARIZE,
    TASK_PLAN,
    TASK_VISION,
)

# Capabilities a profile can advertise.
CAP_CODE = "code"
CAP_REASONING = "reasoning"
CAP_VISION = "vision"
CAP_FAST = "fast"
CAP_LONG_CONTEXT = "long-context"
CAP_TOOLS = "tools"

# Provider ids
PROVIDER_CORE = "core"          # deterministic engine inside FixPilot
PROVIDER_OLLAMA = "ollama"      # local open-source models
PROVIDER_OPENAI = "openai-compat"  # any OpenAI-compatible gateway (cloud or LAN)
PROVIDER_MOCK = "mock"          # tests only


@dataclass(slots=True)
class ModelProfile:
    key: str
    label: str
    provider: str
    model: str
    capabilities: tuple[str, ...]
    context_tokens: int = 8192
    params_b: float = 0.0
    local: bool = True
    cost_per_1k_tokens: float = 0.0
    quality: float = 0.5          # 0..1 rough capability prior
    speed: float = 0.5            # 0..1 rough latency prior
    notes: str = ""
    available: bool | None = None  # None = unknown
    tags: tuple[str, ...] = field(default_factory=tuple)

    def supports(self, *caps: str) -> bool:
        return all(cap in self.capabilities for cap in caps)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "context_tokens": self.context_tokens,
            "params_b": self.params_b,
            "local": self.local,
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
            "quality": self.quality,
            "speed": self.speed,
            "notes": self.notes,
            "available": self.available,
            "tags": list(self.tags),
        }


def default_profiles(ollama_fast: str, ollama_reason: str, ollama_vision: str, openai_model: str, openai_enabled: bool) -> list[ModelProfile]:
    """The catalogue FixPilot ships with.  Availability is probed at runtime."""
    profiles = [
        ModelProfile(
            key="core-deterministic",
            label="FixPilot Core (deterministic)",
            provider=PROVIDER_CORE,
            model="deterministic-v1",
            capabilities=(CAP_CODE, CAP_REASONING, CAP_FAST, CAP_TOOLS),
            context_tokens=100_000,
            local=True,
            quality=0.55,
            speed=0.98,
            notes="Evidence-driven static analysis + template patching. Always available, never sends code off-device.",
            tags=("always-on", "offline"),
        ),
        ModelProfile(
            key=f"ollama:{ollama_fast}",
            label=f"{ollama_fast} (local, fast)",
            provider=PROVIDER_OLLAMA,
            model=ollama_fast,
            capabilities=(CAP_CODE, CAP_FAST),
            context_tokens=32_768,
            params_b=1.5,
            local=True,
            quality=0.45,
            speed=0.9,
            notes="Best for triage, log parsing and short explanations on a modest laptop.",
            tags=("recommended-default",),
        ),
        ModelProfile(
            key=f"ollama:{ollama_reason}",
            label=f"{ollama_reason} (local, reasoning)",
            provider=PROVIDER_OLLAMA,
            model=ollama_reason,
            capabilities=(CAP_CODE, CAP_REASONING, CAP_LONG_CONTEXT),
            context_tokens=32_768,
            params_b=7.0,
            local=True,
            quality=0.72,
            speed=0.55,
            notes="Root-cause reasoning and patch authoring quality tier.",
            tags=("recommended-default",),
        ),
        ModelProfile(
            key=f"ollama:{ollama_vision}",
            label=f"{ollama_vision} (local, vision)",
            provider=PROVIDER_OLLAMA,
            model=ollama_vision,
            capabilities=(CAP_VISION, CAP_CODE),
            context_tokens=8192,
            params_b=11.0,
            local=True,
            quality=0.6,
            speed=0.35,
            notes="Reads stack-trace screenshots and UI bug captures without a round trip to the cloud.",
            tags=("vision",),
        ),
    ]
    if openai_enabled:
        profiles.append(
            ModelProfile(
                key=f"openai:{openai_model}",
                label=f"{openai_model} (cloud escalation)",
                provider=PROVIDER_OPENAI,
                model=openai_model,
                capabilities=(CAP_CODE, CAP_REASONING, CAP_VISION, CAP_LONG_CONTEXT, CAP_TOOLS),
                context_tokens=128_000,
                local=False,
                cost_per_1k_tokens=0.15,
                quality=0.85,
                speed=0.75,
                notes="Escalation only — payloads are redacted and the route is recorded in the session audit trail.",
                tags=("cloud", "opt-in"),
            )
        )
    return profiles


class ModelRegistry:
    def __init__(self, profiles: list[ModelProfile] | None = None) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        for profile in profiles or []:
            self.register(profile)

    def register(self, profile: ModelProfile) -> None:
        self._profiles[profile.key] = profile

    def get(self, key: str) -> ModelProfile | None:
        return self._profiles.get(key)

    def all(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    def by_provider(self, provider: str) -> list[ModelProfile]:
        return [p for p in self._profiles.values() if p.provider == provider]

    def usable(self, strategy: str = "local-first", allow_cloud: bool = True) -> list[ModelProfile]:
        usable = []
        for profile in self._profiles.values():
            if profile.available is False:
                continue
            if profile.provider == PROVIDER_MOCK:
                continue
            if not profile.local and not allow_cloud:
                continue
            if strategy == "local-only" and not profile.local:
                continue
            if strategy == "offline" and profile.provider != PROVIDER_CORE:
                continue
            usable.append(profile)
        return usable

    def mark_available(self, provider: str, models: set[str]) -> None:
        """Attach probe results: an Ollama tag list or an OpenAI model list."""
        for profile in self._profiles.values():
            if profile.provider != provider:
                continue
            if not models:
                profile.available = False
            elif profile.provider == PROVIDER_CORE:
                profile.available = True
            else:
                base = profile.model.split(":")[0]
                profile.available = any(
                    model == profile.model or model.split(":")[0] == base for model in models
                )

    def set_available(self, key: str, value: bool) -> None:
        profile = self._profiles.get(key)
        if profile:
            profile.available = value

    def summary(self) -> list[dict]:
        return [p.to_dict() for p in sorted(self._profiles.values(), key=lambda p: (not p.local, -p.quality))]
