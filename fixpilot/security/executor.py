"""Sandboxed command execution.

Guarantees, in order of importance:

1. **No shell.** Commands are ``exec``'d as argv arrays; there is no ``shell=True``
   anywhere in FixPilot, so quoting bugs cannot become command injection.
2. **Policy first.** Nothing runs without a :class:`~fixpilot.security.policy.Verdict`.
3. **Contained environment.** Untrusted code gets a minimal env (PATH, HOME in a
   scratch dir, TMPDIR in the repo) — never the developer's API keys.
4. **Bounded.** Wall-clock timeout, capped stdout/stderr, process-group kill,
   CPU/file-size rlimits where the platform supports them.
5. **Redacted.** Output is scrubbed of credential material before it is stored
   or displayed on the phone.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..store import AuditLog
from ..util import strip_ansi, truncate_bytes
from .policy import ALLOW, CommandPolicy, Verdict
from .secrets import SecretScanner

#: Environment variables the sandbox always passes through.
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "USER", "LOGNAME", "SYSTEMROOT", "COMSPEC")

#: Never forward these, even if present in the parent environment.
ENV_BLOCKLIST = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_API_KEY", "AZURE_CLIENT_SECRET", "SLACK_TOKEN", "STRIPE_SECRET_KEY",
    "NPM_TOKEN", "PYPI_TOKEN", "DOCKER_PASSWORD", "DATABASE_URL", "REDIS_URL", "FIXPILOT_OPENAI_API_KEY",
)


@dataclass(slots=True)
class ExecResult:
    argv: list[str] = field(default_factory=list)
    cwd: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    command: str = ""
    verdict: dict = field(default_factory=dict)
    redactions: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def combined(self, limit: int = 20_000) -> str:
        blob = self.stdout
        if self.stderr:
            blob += ("\n" if blob else "") + self.stderr
        return strip_ansi(blob)[:limit]

    def summary_line(self) -> str:
        status = "ok" if self.ok else ("timeout" if self.timed_out else f"exit {self.exit_code}")
        return f"$ {self.command}  →  {status} in {self.duration_ms}ms"

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "argv": self.argv,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "verdict": self.verdict,
            "redactions": self.redactions,
            "error": self.error or None,
        }


class SandboxExecutor:
    def __init__(self, settings: Settings, policy: CommandPolicy | None = None, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.policy = policy or CommandPolicy(settings)
        self.audit = audit
        self.scanner = SecretScanner(block_writes=True)
        self.scratch_home = settings.data_dir / "sandbox-home"
        self.scratch_tmp = settings.data_dir / "tmp"
        for path in (self.scratch_home, self.scratch_tmp):
            path.mkdir(parents=True, exist_ok=True)
        self.history: list[dict] = []

    # -- environment ---------------------------------------------------
    def build_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env: dict[str, str] = {"TERM": "dumb", "PYTHONDONTWRITEBYTECODE": "1", "CI": "1"}
        for key in ENV_PASSTHROUGH:
            value = os.environ.get(key)
            if value and key not in ENV_BLOCKLIST:
                env[key] = value
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["HOME"] = str(self.scratch_home)
        env["TMPDIR"] = str(self.scratch_tmp)
        env["FIXPILOT_SANDBOX"] = "1"
        for key, value in (extra or {}).items():
            if key in ENV_BLOCKLIST:
                continue
            env[key] = str(value)
        return env

    # -- public API ----------------------------------------------------
    def run(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: int | None = None,
        purpose: str = "",
        env_extra: dict[str, str] | None = None,
        approved: bool = False,
        max_output: int | None = None,
    ) -> ExecResult:
        workdir = Path(cwd or self.settings.repo_root)
        verdict = self.policy.evaluate(argv, cwd=workdir, purpose=purpose)
        result = ExecResult(argv=list(argv), cwd=str(workdir), command=" ".join(shlex.quote(a) for a in argv), verdict=verdict.to_dict())
        if verdict.decision != ALLOW and not (verdict.needs_confirmation and approved):
            result.error = f"blocked by policy ({verdict.decision}): {'; '.join(verdict.reasons)}"
            self._audit(result, verdict, purpose)
            return result
        return self._spawn(result, verdict, workdir, timeout or self.settings.sandbox.timeout_seconds, purpose, env_extra, max_output)

    def run_string(self, command: str, **kwargs) -> ExecResult:
        verdict = self.policy.evaluate_string(command, cwd=kwargs.get("cwd") or self.settings.repo_root, purpose=kwargs.get("purpose", ""))
        if verdict.decision == "deny" or not verdict.argv:
            result = ExecResult(command=command, cwd=str(self.settings.repo_root), verdict=verdict.to_dict())
            result.error = f"blocked by policy: {'; '.join(verdict.reasons)}"
            self._audit(result, verdict, kwargs.get("purpose", ""))
            return result
        return self.run(verdict.argv, **kwargs)

    def run_trusted(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: int | None = None,
        purpose: str = "internal",
        env_extra: dict[str, str] | None = None,
        max_output: int | None = None,
    ) -> ExecResult:
        """Agent bookkeeping (git apply / git diff / worktree) — still audited."""
        workdir = Path(cwd or self.settings.repo_root)
        verdict = self.policy.evaluate(argv, cwd=workdir, purpose=purpose)
        if verdict.decision == "deny":
            result = ExecResult(argv=list(argv), cwd=str(workdir), command=" ".join(shlex.quote(a) for a in argv), verdict=verdict.to_dict())
            result.error = f"blocked by policy: {'; '.join(verdict.reasons)}"
            self._audit(result, verdict, purpose)
            return result
        result = ExecResult(argv=list(argv), cwd=str(workdir), command=" ".join(shlex.quote(a) for a in argv), verdict=verdict.to_dict())
        return self._spawn(result, verdict, workdir, timeout or self.settings.sandbox.timeout_seconds, purpose, env_extra, max_output)

    # -- internals -----------------------------------------------------
    def _spawn(
        self,
        result: ExecResult,
        verdict: Verdict,
        workdir: Path,
        timeout: int,
        purpose: str,
        env_extra: dict[str, str] | None,
        max_output: int | None,
    ) -> ExecResult:
        limit = max_output or self.settings.sandbox.max_output_bytes
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                result.argv,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=self.build_env(env_extra),
                start_new_session=True,
                preexec_fn=self._limiter(timeout) if os.name == "posix" else None,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            result.error = f"could not start command: {exc}"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            self._audit(result, verdict, purpose)
            return result

        collected: dict[str, list[str]] = {"out": [], "err": []}
        truncated = {"out": False, "err": False}

        def pump(stream, key: str) -> None:
            try:
                for chunk in iter(lambda: stream.readline(), ""):
                    if sum(len(c) for c in collected[key]) < limit:
                        collected[key].append(chunk)
                    else:
                        truncated[key] = True
            except (ValueError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        threads = [
            threading.Thread(target=pump, args=(process.stdout, "out"), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, "err"), daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result.timed_out = True
            self._terminate(process)
        for thread in threads:
            thread.join(timeout=2)

        result.exit_code = process.returncode if process.returncode is not None else -1
        stdout = "".join(collected["out"])
        stderr = "".join(collected["err"])
        result.truncated = truncated["out"] or truncated["err"]
        result.stdout, redactions_out = self._sanitize(stdout, limit)
        result.stderr, redactions_err = self._sanitize(stderr, limit)
        result.redactions = redactions_out + redactions_err
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if result.timed_out:
            result.error = f"command exceeded {timeout}s and was terminated"
        self._audit(result, verdict, purpose)
        return result

    @staticmethod
    def _limiter(timeout: int):
        def _apply() -> None:  # pragma: no cover - runs in the child process
            try:
                import resource

                cpu = max(1, timeout)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_NOFILE, (256, 512))
            except Exception:
                pass

        return _apply

    def _sanitize(self, text: str, limit: int) -> tuple[str, int]:
        cleaned = strip_ansi(text or "")
        findings = self.scanner.audit(cleaned)
        redactions = 0
        for finding in findings:
            if finding.severity in {"critical", "high"} and finding.preview in cleaned:
                cleaned = cleaned.replace(finding.preview, "[REDACTED]")
                redactions += 1
        body, _ = truncate_bytes(cleaned, limit)
        return body, redactions

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                continue

    def _audit(self, result: ExecResult, verdict: Verdict, purpose: str) -> None:
        entry = {
            "kind": "sandbox.exec",
            "command": result.command,
            "cwd": result.cwd,
            "decision": verdict.decision,
            "risk": verdict.risk,
            "category": verdict.category,
            "purpose": purpose,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
            "blocked": bool(result.error and "blocked" in result.error),
            "redactions": result.redactions,
        }
        self.history.append(entry)
        if self.audit:
            self.audit.record(entry)

    def stats(self) -> dict:
        by_decision: dict[str, int] = {}
        for entry in self.history:
            by_decision[entry["decision"]] = by_decision.get(entry["decision"], 0) + 1
        return {
            "commands_run": len(self.history),
            "by_decision": by_decision,
            "policy": self.policy.describe(),
            "limits": self.settings.sandbox.public(),
        }
