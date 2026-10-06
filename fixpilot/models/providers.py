"""Model providers: Ollama (local), OpenAI-compatible (cloud/LAN), mock.

All HTTP uses :mod:`urllib.request` from the standard library, so FixPilot
keeps its "no install step" property.  Providers are defensive: every call is
time-boxed, returns a structured error instead of raising, and reports whether
prompt payloads left the device.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..util import redact


@dataclass(slots=True)
class Completion:
    text: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    prompt_tokens_est: int = 0
    output_tokens_est: int = 0
    error: str = ""
    left_device: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "prompt_tokens_est": self.prompt_tokens_est,
            "output_tokens_est": self.output_tokens_est,
            "error": self.error or None,
            "left_device": self.left_device,
            "chars": len(self.text),
        }


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class BaseProvider:
    name = "base"
    local = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> tuple[bool, set[str]]:
        return True, set()

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: str = "",
        images: list[str] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        timeout: int | None = None,
    ) -> Completion:  # pragma: no cover - interface
        raise NotImplementedError


class OllamaProvider(BaseProvider):
    """Local open-source models served by Ollama."""

    name = "ollama"
    local = True

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base = settings.models.ollama_url.rstrip("/")
        self._probe: tuple[bool, set[str]] | None = None
        self._probe_at = 0.0

    # -- discovery -----------------------------------------------------
    def available(self) -> tuple[bool, set[str]]:
        now = time.time()
        if self._probe is not None and now - self._probe_at < 20:
            return self._probe
        ok, models = False, set()
        try:
            with urllib.request.urlopen(f"{self.base}/api/tags", timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            models = {item.get("name", "") for item in payload.get("models", []) if item.get("name")}
            ok = bool(models)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            ok = False
        self._probe = (ok, models)
        self._probe_at = now
        return self._probe

    # -- inference -----------------------------------------------------
    def complete(self, prompt: str, *, model: str, system: str = "", images: list[str] | None = None, max_tokens: int = 1024, temperature: float = 0.1, timeout: int | None = None) -> Completion:
        started = time.perf_counter()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_message: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_message["images"] = [self._encode_image(path) for path in images if path]
        messages.append(user_message)
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            request = urllib.request.Request(
                f"{self.base}/api/chat",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout or self.settings.models.request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            text = (payload.get("message") or {}).get("content", "") or payload.get("response", "")
            return Completion(
                text=text.strip(),
                provider=self.name,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_tokens_est=estimate_tokens(prompt),
                output_tokens_est=estimate_tokens(text),
                left_device=False,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError) as exc:
            return Completion(
                provider=self.name,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _encode_image(path: str) -> str:
        try:
            with open(path, "rb") as handle:
                return base64.b64encode(handle.read()).decode("ascii")
        except OSError:
            return ""


class OpenAICompatProvider(BaseProvider):
    """Any OpenAI-compatible endpoint (cloud gateway or LAN vLLM/LM Studio)."""

    name = "openai-compat"
    local = False

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.base = settings.models.openai_base_url.rstrip("/")
        self.key = settings.models.openai_api_key
        self._probe: tuple[bool, set[str]] | None = None
        self._probe_at = 0.0

    def available(self) -> tuple[bool, set[str]]:
        if not self.base:
            return False, set()
        now = time.time()
        if self._probe is not None and now - self._probe_at < 30:
            return self._probe
        ok, models = False, set()
        try:
            request = urllib.request.Request(f"{self.base}/models", headers=self._headers())
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            models = {item.get("id", "") for item in payload.get("data", []) if item.get("id")}
            ok = bool(models)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError):
            ok = False
        self._probe = (ok, models)
        self._probe_at = now
        return self._probe

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def complete(self, prompt: str, *, model: str, system: str = "", images: list[str] | None = None, max_tokens: int = 1024, temperature: float = 0.1, timeout: int | None = None) -> Completion:
        started = time.perf_counter()
        # Defence in depth: never ship an obvious secret to a third party.
        safe_prompt = redact(prompt)
        content: Any = safe_prompt
        if images:
            content = [{"type": "text", "text": safe_prompt}]
            for path in images:
                encoded = OllamaProvider._encode_image(path)
                if encoded:
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        try:
            request = urllib.request.Request(
                f"{self.base}/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout or self.settings.models.request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            choice = (payload.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            usage = payload.get("usage") or {}
            return Completion(
                text=text,
                provider=self.name,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                prompt_tokens_est=int(usage.get("prompt_tokens") or estimate_tokens(safe_prompt)),
                output_tokens_est=int(usage.get("completion_tokens") or estimate_tokens(text)),
                left_device=True,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, ValueError) as exc:
            return Completion(
                provider=self.name,
                model=model,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                left_device=True,
            )


class CoreProvider(BaseProvider):
    """The deterministic engine: a callable into FixPilot itself."""

    name = "core"
    local = True

    def __init__(self, settings: Settings, handler: Callable[..., Completion] | None = None) -> None:
        super().__init__(settings)
        self.handler = handler

    def complete(self, prompt: str, *, model: str = "deterministic-v1", system: str = "", images: list[str] | None = None, max_tokens: int = 1024, temperature: float = 0.1, timeout: int | None = None) -> Completion:
        if self.handler is None:
            return Completion(provider=self.name, model=model, error="core handler not wired")
        return self.handler(prompt, system=system, max_tokens=max_tokens)


class MockProvider(BaseProvider):
    """Deterministic provider used by the test-suite."""

    name = "mock"
    local = True

    def __init__(self, settings: Settings, responses: dict[str, str] | None = None) -> None:
        super().__init__(settings)
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, model: str = "mock", system: str = "", images: list[str] | None = None, max_tokens: int = 1024, temperature: float = 0.1, timeout: int | None = None) -> Completion:
        self.calls.append({"prompt": prompt, "system": system, "model": model, "images": images or []})
        for needle, response in self.responses.items():
            if needle.lower() in prompt.lower():
                return Completion(text=response, provider=self.name, model=model, output_tokens_est=estimate_tokens(response))
        default = self.responses.get("*", "{}")
        return Completion(text=default, provider=self.name, model=model, output_tokens_est=estimate_tokens(default))


def build_providers(settings: Settings) -> dict[str, BaseProvider]:
    from .registry import PROVIDER_CORE, PROVIDER_OLLAMA, PROVIDER_OPENAI

    return {
        PROVIDER_OLLAMA: OllamaProvider(settings),
        PROVIDER_OPENAI: OpenAICompatProvider(settings),
        PROVIDER_CORE: CoreProvider(settings),
    }
