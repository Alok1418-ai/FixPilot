"""Intelligent task → model routing.

Different jobs deserve different brains.  Parsing a stack trace is a 1.5B-param
job; authoring a patch that survives a test run is not.  The router scores every
*available* profile against the requirements of the task, honours the privacy
strategy (local-first by default), and returns an ordered escalation ladder so a
weak local model never becomes a dead end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..store import AuditLog
from .providers import BaseProvider, Completion
from .registry import (
    CAP_CODE,
    CAP_FAST,
    CAP_LONG_CONTEXT,
    CAP_REASONING,
    CAP_VISION,
    PROVIDER_CORE,
    PROVIDER_MOCK,
    TASK_EXPLAIN,
    TASK_PATCH,
    TASK_PLAN,
    TASK_ROOT_CAUSE,
    TASK_SUMMARIZE,
    TASK_TEST_REPAIR,
    TASK_TRIAGE,
    TASK_VISION,
    ModelProfile,
    ModelRegistry,
    default_profiles,
)

#: What each task needs: (required capabilities, minimum context tokens, quality
#: weight, speed weight).
TASK_REQUIREMENTS: dict[str, tuple[tuple[str, ...], int, float, float]] = {
    TASK_TRIAGE: ((CAP_FAST,), 4_000, 0.35, 0.65),
    TASK_ROOT_CAUSE: ((CAP_REASONING,), 16_000, 0.75, 0.25),
    TASK_PATCH: ((CAP_CODE,), 16_000, 0.80, 0.20),
    TASK_TEST_REPAIR: ((CAP_CODE,), 8_000, 0.65, 0.35),
    TASK_EXPLAIN: ((CAP_CODE,), 8_000, 0.55, 0.45),
    TASK_SUMMARIZE: ((CAP_FAST,), 4_000, 0.30, 0.70),
    TASK_PLAN: ((CAP_REASONING,), 8_000, 0.7, 0.3),
    TASK_VISION: ((CAP_VISION,), 4_000, 0.7, 0.3),
}


@dataclass(slots=True)
class RouteDecision:
    task: str
    profile_key: str
    provider: str
    model: str
    reason: str
    privacy: str
    score: float = 0.0
    ladder: list[str] = field(default_factory=list)
    local: bool = True

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "profile": self.profile_key,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "privacy": self.privacy,
            "score": round(self.score, 3),
            "ladder": self.ladder,
            "local": self.local,
        }


@dataclass
class ModelUsage:
    calls: int = 0
    failures: int = 0
    total_latency_ms: int = 0
    tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "avg_latency_ms": int(self.total_latency_ms / self.calls) if self.calls else 0,
            "tokens": self.tokens,
        }


class ModelRouter:
    def __init__(self, settings: Settings, providers: dict[str, BaseProvider], audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.providers = providers
        self.audit = audit
        models = settings.models
        self.registry = ModelRegistry(
            default_profiles(
                ollama_fast=models.ollama_fast,
                ollama_reason=models.ollama_reason,
                ollama_vision=models.ollama_vision,
                openai_model=models.openai_model,
                openai_enabled=bool(models.openai_base_url),
            )
        )
        self.usage: dict[str, ModelUsage] = {}
        self._probed_at = 0.0
        self.last_route: RouteDecision | None = None

    # -- discovery -----------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._probed_at < 15:
            return
        self._probed_at = time.time()
        for provider_id, provider in self.providers.items():
            if provider_id == PROVIDER_CORE:
                self.registry.mark_available(provider_id, {"deterministic-v1"})
                continue
            ok, models = provider.available()
            self.registry.mark_available(provider_id, models if ok else set())

    def available_profiles(self) -> list[ModelProfile]:
        self.refresh()
        strategy = self.settings.models.strategy
        return self.registry.usable(strategy=strategy, allow_cloud=self.cloud_allowed())

    def cloud_allowed(self) -> bool:
        strategy = self.settings.models.strategy
        return bool(self.settings.models.openai_base_url) and strategy in {"local-first", "cloud-first"}

    # -- routing -------------------------------------------------------
    def route(self, task: str, *, needs_vision: bool = False, context_tokens: int = 2_000, best: bool = False) -> RouteDecision:
        self.refresh()
        task = task if task in TASK_REQUIREMENTS else TASK_TRIAGE
        required, min_context, quality_weight, speed_weight = TASK_REQUIREMENTS[task]
        if needs_vision:
            required = tuple(set(required) | {CAP_VISION})
            task = TASK_VISION if task in {TASK_TRIAGE, TASK_ROOT_CAUSE} else task
        strategy = self.settings.models.strategy
        candidates = self.available_profiles()
        if not candidates:
            core = self.registry.get("core-deterministic")
            if core is None:  # pragma: no cover - registry always has core
                raise RuntimeError("no model profiles available")
            return RouteDecision(
                task=task,
                profile_key=core.key,
                provider=core.provider,
                model=core.model,
                reason="no model backend reachable — using deterministic offline engine",
                privacy="never leaves device",
                score=0.0,
                ladder=[core.key],
                local=True,
            )

        scored: list[tuple[float, ModelProfile, list[str]]] = []
        for profile in candidates:
            missing = [cap for cap in required if cap not in profile.capabilities]
            if missing and profile.provider != PROVIDER_CORE:
                continue
            if missing and profile.provider == PROVIDER_CORE:
                # core engine can always act, but never wins on quality
                pass
            notes: list[str] = []
            score = profile.quality * quality_weight + profile.speed * speed_weight
            if missing:
                score -= 0.35 * len(missing)
                notes.append(f"lacks {'/'.join(missing)}")
            if profile.context_tokens < context_tokens:
                score -= 0.3
                notes.append("context may truncate")
            if profile.local:
                score += 0.45 if strategy == "local-first" else 0.15
                notes.append("stays on device")
            else:
                score += 0.2 if strategy == "cloud-first" else -0.35
                notes.append("cloud (redacted)")
            score -= profile.cost_per_1k_tokens * 0.5
            if best:
                score += profile.quality * 0.5 - profile.speed * 0.2
            scored.append((score, profile, notes))

        if not scored:
            core = self.registry.get("core-deterministic")
            assert core is not None
            scored = [(0.0, core, ["deterministic fallback"])]

        scored.sort(key=lambda item: -item[0])
        top_score, top, notes = scored[0]
        ladder = [profile.key for _, profile, _ in scored]
        decision = RouteDecision(
            task=task,
            profile_key=top.key,
            provider=top.provider,
            model=top.model,
            reason=self._explain(top, task, notes),
            privacy="local only" if top.local else "redacted payload to cloud endpoint",
            score=top_score,
            ladder=ladder,
            local=top.local,
        )
        self.last_route = decision
        return decision

    @staticmethod
    def _explain(profile: ModelProfile, task: str, notes: list[str]) -> str:
        bits = [f"{profile.label} is the best fit for {task.replace('_', ' ')}"]
        if notes:
            bits.append("(" + "; ".join(notes) + ")")
        return " ".join(bits)

    def escalate(self, decision: RouteDecision) -> RouteDecision | None:
        """Next rung of the ladder after a failure (e.g. unusable output)."""
        ladder = list(decision.ladder)
        if decision.profile_key in ladder:
            ladder = ladder[ladder.index(decision.profile_key) + 1 :]
        if not ladder:
            return None
        profile = self.registry.get(ladder[0])
        if profile is None:
            return None
        return RouteDecision(
            task=decision.task,
            profile_key=profile.key,
            provider=profile.provider,
            model=profile.model,
            reason=f"escalated after unusable output from {decision.profile_key}",
            privacy="local only" if profile.local else "redacted payload to cloud endpoint",
            ladder=ladder,
            local=profile.local,
        )

    # -- execution -----------------------------------------------------
    def call(
        self,
        task: str,
        prompt: str,
        *,
        system: str = "",
        images: list[str] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        needs_vision: bool = False,
        best: bool = False,
        escalate_on_error: bool = True,
    ) -> tuple[Completion, RouteDecision | None]:
        decision = self.route(task, needs_vision=needs_vision, context_tokens=len(prompt) // 4 + 500, best=best)
        attempts: list[RouteDecision] = [decision]
        completion = self._dispatch(decision, prompt, system, images, max_tokens, temperature)
        if not completion.ok and escalate_on_error:
            while True:
                nxt = self.escalate(attempts[-1])
                if nxt is None:
                    break
                attempts.append(nxt)
                completion = self._dispatch(nxt, prompt, system, images, max_tokens, temperature)
                if completion.ok:
                    break
        return completion, decision

    def _dispatch(
        self,
        decision: RouteDecision,
        prompt: str,
        system: str,
        images: list[str] | None,
        max_tokens: int | None,
        temperature: float | None,
    ) -> Completion:
        provider = self.providers.get(decision.provider)
        profile = self.registry.get(decision.profile_key)
        if provider is None or profile is None:
            return Completion(error=f"provider {decision.provider} unavailable", provider=decision.provider, model=decision.model)
        completion = provider.complete(
            prompt,
            model=decision.model,
            system=system,
            images=images,
            max_tokens=max_tokens or self.settings.models.max_tokens,
            temperature=self.settings.models.temperature if temperature is None else temperature,
        )
        usage = self.usage.setdefault(decision.profile_key, ModelUsage())
        usage.calls += 1
        usage.total_latency_ms += completion.latency_ms
        usage.tokens += completion.prompt_tokens_est + completion.output_tokens_est
        if not completion.ok:
            usage.failures += 1
        if self.audit is not None:
            self.audit.record(
                {
                    "kind": "model.call",
                    "task": decision.task,
                    "model": decision.profile_key,
                    "privacy": decision.privacy,
                    "latency_ms": completion.latency_ms,
                    "error": completion.error or None,
                }
            )
        return completion

    # -- introspection -------------------------------------------------
    def describe(self) -> dict:
        self.refresh()
        profiles = []
        for profile in self.registry.all():
            item = profile.to_dict()
            item["usage"] = self.usage.get(profile.key, ModelUsage()).to_dict()
            profiles.append(item)
        return {
            "strategy": self.settings.models.strategy,
            "cloud_allowed": self.cloud_allowed(),
            "profiles": profiles,
            "tasks": {
                task: {
                    "requires": list(req[0]),
                    "min_context": req[1],
                }
                for task, req in TASK_REQUIREMENTS.items()
            },
            "last_route": self.last_route.to_dict() if self.last_route else None,
        }

    def explain_route(self, task: str) -> dict[str, Any]:
        decision = self.route(task)
        return decision.to_dict()

    def register_mock(self, responses: dict[str, str]) -> None:
        """Test hook: force a deterministic provider for every task."""
        from .providers import MockProvider

        provider = MockProvider(self.settings, responses)
        self.providers = {PROVIDER_MOCK: provider}
        from .registry import ModelProfile

        self.registry = ModelRegistry(
            [
                ModelProfile(
                    key="mock:test",
                    label="mock",
                    provider=PROVIDER_MOCK,
                    model="mock",
                    capabilities=(CAP_CODE, CAP_REASONING, CAP_FAST, CAP_VISION, CAP_TOOLS, CAP_LONG_CONTEXT),
                    context_tokens=200_000,
                    quality=1.0,
                    speed=1.0,
                    available=True,
                )
            ]
        )
        self._probed_at = 0.0
