"""Configuration & runtime paths for FixPilot.

Everything is overridable through environment variables prefixed with
``FIXPILOT_``.  No third-party dependency is required; ``.env`` files are
parsed by hand so a phone-paired workstation can be configured without a
toolchain.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIRNAME = ".fixpilot"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"FIXPILOT_{name}", default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader (no shell expansion, no interpolation)."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return loaded
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


@dataclass(slots=True)
class RepoLimits:
    """Guard rails for codebase ingestion."""

    max_file_bytes: int = 512 * 1024
    max_files: int = 20_000
    max_symbols: int = 200_000
    max_git_commits: int = 400


@dataclass(slots=True)
class SandboxLimits:
    """Every command the agent runs passes through these limits."""

    timeout_seconds: int = _env_int("CMD_TIMEOUT", 300)
    quick_timeout_seconds: int = _env_int("CMD_TIMEOUT_QUICK", 60)
    max_output_bytes: int = _env_int("CMD_MAX_OUTPUT", 256 * 1024)
    max_patch_bytes: int = _env_int("MAX_PATCH_BYTES", 200 * 1024)
    max_patch_files: int = _env_int("MAX_PATCH_FILES", 12)
    allow_network: bool = _env_bool("ALLOW_NETWORK", False)
    allow_dependency_install: bool = _env_bool("ALLOW_DEP_INSTALL", False)
    extra_allow: list[str] = field(default_factory=lambda: _env_list("ALLOW_COMMANDS"))

    def public(self) -> dict[str, object]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "quick_timeout_seconds": self.quick_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_patch_bytes": self.max_patch_bytes,
            "max_patch_files": self.max_patch_files,
            "allow_network": self.allow_network,
            "allow_dependency_install": self.allow_dependency_install,
            "extra_allow": list(self.extra_allow),
        }


@dataclass(slots=True)
class AuthSettings:
    """Login gate for the phone-facing server.

    On by default: without it, anyone who can reach the port receives a working
    mutation token from ``GET /`` and can approve patches and run commands.
    """

    enabled: bool = _env_bool("AUTH", True)
    # Self-service account creation. Off by default on purpose — a LAN-exposed
    # server must not be enrol-able by strangers.
    allow_signup: bool = _env_bool("ALLOW_SIGNUP", False)
    session_hours: int = _env_int("AUTH_SESSION_HOURS", 12)

    def public(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "allow_signup": self.allow_signup,
            "session_hours": self.session_hours,
        }


@dataclass(slots=True)
class ModelSettings:
    """Local-first model routing configuration."""

    # "local-first" | "cloud-first" | "local-only" | "offline"
    strategy: str = _env("MODEL_STRATEGY", "local-first")
    ollama_url: str = _env("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_fast: str = _env("OLLAMA_FAST", "qwen2.5-coder:1.5b")
    ollama_reason: str = _env("OLLAMA_REASON", "qwen2.5-coder:7b")
    ollama_vision: str = _env("OLLAMA_VISION", "llama3.2-vision:11b")
    openai_base_url: str = _env("OPENAI_BASE_URL", "")
    openai_api_key: str = _env("OPENAI_API_KEY", "")
    openai_model: str = _env("OPENAI_MODEL", "gpt-4o-mini")
    request_timeout: int = _env_int("MODEL_TIMEOUT", 120)
    max_tokens: int = _env_int("MODEL_MAX_TOKENS", 2048)
    temperature: float = float(_env("MODEL_TEMPERATURE", "0.1") or 0.1)
    # Deterministic engine always participates as an evidence source; it is
    # used as the *fallback* whenever no model backend is reachable.
    allow_offline_core: bool = _env_bool("ALLOW_OFFLINE_CORE", True)

    def public(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "ollama_url": self.ollama_url,
            "openai_base_url": self.openai_base_url or None,
            "openai_model": self.openai_model if self.openai_base_url else None,
            "openai_key_present": bool(self.openai_api_key),
            "request_timeout": self.request_timeout,
            "allow_offline_core": self.allow_offline_core,
        }


@dataclass(slots=True)
class GitHubSettings:
    """Read-only GitHub insights for the dashboard (pull requests, issues, CI).

    The token is optional on purpose: a public repository works unauthenticated
    at 60 requests/hour, and the dashboard says so rather than failing.  The
    token itself is never part of :meth:`public` — only its presence.
    """

    # FIXPILOT_GITHUB_TOKEN first, then the conventional GITHUB_TOKEN / GH_TOKEN
    # that `gh` and GitHub Actions already export.
    token: str = _env("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get("GH_TOKEN", "").strip()
    # owner/name. Empty → auto-detected from the served repository's git remote.
    repo: str = _env("GITHUB_REPO")
    api_base: str = _env("GITHUB_API", "https://api.github.com")
    cache_seconds: int = _env_int("GITHUB_CACHE_SECONDS", 300)
    timeout: int = _env_int("GITHUB_TIMEOUT", 20)
    weeks: int = max(2, min(26, _env_int("GITHUB_WEEKS", 8)))
    max_pull_requests: int = _env_int("GITHUB_MAX_PRS", 40)
    max_issues: int = _env_int("GITHUB_MAX_ISSUES", 60)
    # Per-PR detail (additions/deletions/review counts) costs one call each.
    detail_budget: int = _env_int("GITHUB_DETAIL_PRS", 20)
    # One CI call per commit is the expensive part of the sync.
    check_budget: int = _env_int("GITHUB_CHECK_BUDGET", 25)
    workflow_runs: bool = _env_bool("GITHUB_WORKFLOW_RUNS", True)
    # Allow unauthenticated reads of public repositories (60 req/hour).
    anonymous: bool = _env_bool("GITHUB_ANONYMOUS", True)

    def public(self) -> dict[str, object]:
        return {
            "repo": self.repo or None,
            "api_base": self.api_base,
            "token_present": bool(self.token),
            "anonymous_reads": self.anonymous,
            "cache_seconds": self.cache_seconds,
            "weeks": self.weeks,
            "max_pull_requests": self.max_pull_requests,
            "max_issues": self.max_issues,
            "check_budget": self.check_budget,
            "workflow_runs": self.workflow_runs,
        }


@dataclass(slots=True)
class Settings:
    """Root settings object."""

    host: str = _env("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8787)
    repo_root: Path = field(default_factory=lambda: Path(_env("REPO") or os.getcwd()).resolve())
    data_dir: Path = field(default_factory=Path)
    autopush: bool = _env_bool("AUTO_PUSH", False)
    require_approval: bool = _env_bool("REQUIRE_APPROVAL", True)
    max_refine_iterations: int = _env_int("MAX_REFINE_ITERATIONS", 3)
    demo_mode: bool = _env_bool("DEMO_MODE", False)
    repo_limits: RepoLimits = field(default_factory=RepoLimits)
    sandbox: SandboxLimits = field(default_factory=SandboxLimits)
    models: ModelSettings = field(default_factory=ModelSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    github: GitHubSettings = field(default_factory=GitHubSettings)

    def __post_init__(self) -> None:
        if not str(self.data_dir) or str(self.data_dir) == ".":
            self.data_dir = self.repo_root / DEFAULT_DATA_DIRNAME
        self.data_dir = Path(self.data_dir).resolve()
        self.repo_root = Path(self.repo_root).resolve()

    # -- derived paths -------------------------------------------------
    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    @property
    def audit_log(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def github_dir(self) -> Path:
        return self.data_dir / "github"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.sessions_dir,
            self.memory_dir,
            self.worktrees_dir,
            self.uploads_dir,
            self.github_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def public(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "repo_root": str(self.repo_root),
            "data_dir": str(self.data_dir),
            "require_approval": self.require_approval,
            "auto_push": self.autopush,
            "max_refine_iterations": self.max_refine_iterations,
            "demo_mode": self.demo_mode,
            "python": sys.version.split()[0],
            "sandbox": self.sandbox.public(),
            "models": self.models.public(),
            "auth": self.auth.public(),
            "github": self.github.public(),
        }


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings singleton (loaded after .env parsing)."""
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = Settings()
    return _SETTINGS
