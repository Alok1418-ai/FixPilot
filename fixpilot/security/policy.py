"""Command policy: what the agent may run, what needs a human, what never runs.

FixPilot executes real commands on a real machine.  Every command therefore
passes through :class:`CommandPolicy` *before* the sandbox executes it, and the
decision is written to the audit log.  Three outcomes:

``allow``    read-only or test/build work that is safe by construction
``confirm``  mutating work (builds, formatters, dependency installs) that the
             developer must approve once per session
``deny``     destructive, exfiltrating, or privilege-escalating work
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"

SAFE = "safe"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"

SHELL_METACHARACTERS = re.compile(r"[;&|`$><\n\\]|(\$\()|(\|\|)|(&&)")
SHELL_TOKEN_PREFIX = re.compile(r"^\s*(?:sudo|doas|pkexec|su)\b")


@dataclass(slots=True)
class Verdict:
    decision: str = ALLOW
    risk: str = SAFE
    category: str = "general"
    reasons: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision == CONFIRM

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "risk": self.risk,
            "category": self.category,
            "reasons": self.reasons,
            "mitigations": self.mitigations,
            "argv": self.argv,
            "command": " ".join(shlex.quote(a) for a in self.argv),
        }


# --------------------------------------------------------------------------
# Rule tables
# --------------------------------------------------------------------------

# (compiled pattern over the space-joined command, decision, risk, category, reason)
RULES: tuple[tuple[str, str, str, str, str], ...] = (
    # --- privilege escalation / system control -------------------------
    (r"^\s*(sudo|doas|pkexec|su|runas)\b", DENY, CRITICAL, "privilege-escalation", "privilege escalation is never permitted"),
    (r"\b(shutdown|reboot|halt|poweroff|systemctl|service\s+\w+\s+(start|stop|restart)|init\s+[0-9])\b", DENY, CRITICAL, "system-control", "system/service control is outside the sandbox"),
    (r"\b(kill|pkill|killall)\s+(-9\s+)?(-1|0)\b", DENY, CRITICAL, "system-control", "refusing to signal every process"),
    (r":\(\)\s*\{.*\};:", DENY, CRITICAL, "fork-bomb", "fork bomb"),
    (r"\b(useradd|usermod|groupadd|passwd|chsh|crontab)\b", DENY, CRITICAL, "system-control", "account/system modification is not permitted"),
    (r"\bmount|umount|fdisk|mkfs|mkswap|swapoff|losetup\b", DENY, CRITICAL, "disk", "disk/filesystem operations are not permitted"),
    (r"\bdd\s+if=", DENY, CRITICAL, "disk", "raw disk writes are not permitted"),
    (r">\s*/dev/(sd|nvme|hd)", DENY, CRITICAL, "disk", "raw device writes are not permitted"),

    # --- destruction ---------------------------------------------------
    (r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|~|/\*|\$HOME|\*)", DENY, CRITICAL, "destructive", "recursive delete of a root/home path"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*(/|~)(\s|$)", DENY, CRITICAL, "destructive", "refusing to delete root or home"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fdx]|push\s+.*(--force|-f)\b|checkout\s+--\s+\.|stash\s+(drop|clear)|branch\s+-D\s+(main|master))", DENY, HIGH, "destructive-git", "history-destroying git operation requires a human, not an agent"),
    (r"\bgit\s+filter-branch|\bgit\s+reflog\s+expire|\bgit\s+gc\s+--prune=now", DENY, HIGH, "destructive-git", "history rewriting is not automated"),
    (r"\bgit\s+push\b(?!.*\b(--dry-run)\b)", CONFIRM, MEDIUM, "remote-write", "pushing to a remote is a human decision"),
    (r"\btruncate\s+-s\s*0|\bshred\b|\bwipefs\b", DENY, HIGH, "destructive", "irreversible data destruction"),
    (r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]*\s+/(\s|$)", DENY, HIGH, "permissions", "world-writable root path"),
    (r"\bchown\s+-R\b", DENY, MEDIUM, "permissions", "recursive ownership change outside agent scope"),
    (r"\b(chmod|chown)\b.*(\.git/|\.ssh/|/etc/)", DENY, HIGH, "permissions", "permission change on sensitive paths"),

    # --- secrets / exfiltration ---------------------------------------
    (r"\b(cat|less|more|head|tail|bat|strings|grep|rg|awk|sed)\b.*(\.env\b|\.env\.|id_rsa|id_ed25519|\.pem\b|\.p12\b|\.git-credentials|\.npmrc|\.pypirc|\.netrc|credentials(\.json)?|\.aws/|\.ssh/|\.kube/config)", DENY, HIGH, "secret-read", "reading credential material is blocked"),
    (r"\b(env|printenv|set)\b\s*\|\s*(curl|wget|nc|ncat|python)\b", DENY, HIGH, "exfiltration", "environment dumping to the network is blocked"),
    (r"\b(curl|wget|nc|ncat|telnet|scp|rsync|ssh|ftp)\b.*(\-\-data|\-\-data-binary|-d\s+@|-F\s+|--upload-file|-T\s+)", DENY, HIGH, "exfiltration", "uploading local files to the network is blocked"),
    (r"\b(base64|openssl\s+enc)\b.*\|\s*(curl|wget|nc)\b", DENY, HIGH, "exfiltration", "encoded exfiltration is blocked"),
    (r"\bprintenv\b|\benv\s*$", CONFIRM, MEDIUM, "secret-read", "environment listing can leak credentials"),

    # --- network -------------------------------------------------------
    (r"\b(curl|wget|http|httpie|nc|ncat|telnet|ftp|sftp|scp|ssh|rsync|ping|traceroute|dig|nslookup)\b", DENY, MEDIUM, "network", "network access is disabled (FIXPILOT_ALLOW_NETWORK=1 to enable, still requires confirmation)"),
    (r"\b(pip|pip3|npm|yarn|pnpm|poetry|pipenv|bundler|bundle|gem|cargo|go|apt|apt-get|brew|choco|dnf|yum)\s+(install|add|get|update|upgrade|require|sync|fetch)\b", DENY, HIGH, "dependency-install", "installing dependencies changes the environment; enable deliberately"),
    (r"\b(npm|yarn|pnpm)\s+(publish|login|adduser|token)\b", DENY, CRITICAL, "publish", "publishing packages is never automated"),
    (r"\bdocker\s+(run|exec|compose\s+up|build)\b", CONFIRM, MEDIUM, "containers", "container execution is outside the default sandbox"),
    (r"\bkubectl\s+(delete|apply|scale|drain|cordon|exec)\b", DENY, CRITICAL, "cluster", "cluster mutation is not permitted"),
    (r"\bterraform\s+(apply|destroy|import|state)\b", DENY, CRITICAL, "infrastructure", "infrastructure mutation is not permitted"),
    (r"\baws\s+.*\b(delete|terminate|create|put|rm)\b|\bgcloud\s+.*\b(delete|create)\b|\baz\s+.*\b(delete|create)\b", DENY, CRITICAL, "cloud", "cloud resource mutation is not permitted"),
    (r"\bgh\s+(repo\s+delete|release\s+delete|auth\s+token)\b", DENY, CRITICAL, "cloud", "destructive remote operations are not permitted"),

    # --- interpretation (arbitrary code with real side effects) --------
    (r"^\s*(python[0-9.]*|node|ruby|perl|php|bash|sh|zsh|fish)\s+-\s*[ce]\b", CONFIRM, MEDIUM, "interpreter", "inline interpreter execution is powerful but opaque"),
    (r"\bbash\s+-c\b|\bsh\s+-c\b", DENY, MEDIUM, "interpreter", "shell wrappers are not used; FixPilot runs argv directly"),

    # --- review-required build/test work ------------------------------
    (r"^\s*(python3?\s+-m\s+pytest|pytest|python3?\s+-m\s+unittest|tox|nox)\b", ALLOW, SAFE, "test", "test runner"),
    (r"^\s*(npm|yarn|pnpm)\s+(test|run\s+test)",
     ALLOW, SAFE, "test", "test runner"),
    (r"^\s*(npx\s+)?(jest|vitest|mocha|jasmine|cypress)\b", ALLOW, SAFE, "test", "test runner"),
    (r"^\s*go\s+test\b", ALLOW, SAFE, "test", "test runner"),
    (r"^\s*cargo\s+test\b", ALLOW, SAFE, "test", "test runner"),
    (r"^\s*mvn\b.*\btest\b|^\s*gradle\w*\b.*\btest\b", ALLOW, SAFE, "test", "test runner"),
    (r"^\s*make\s+(test|check)\b", ALLOW, SAFE, "test", "test target"),
    (r"^\s*(ruff|flake8|pylint|mypy|pyright|black\s+--check|isort\s+--check|eslint|tsc\s+--noEmit|golangci-lint|shellcheck|rubocop)\b",
     ALLOW, SAFE, "lint", "read-only static analysis"),
    (r"^\s*(black|isort|prettier|ruff\s+format|gofmt|rustfmt)\b", CONFIRM, MEDIUM, "format", "formatters rewrite files"),
    (r"^\s*(npm|yarn|pnpm)\s+run\s+(build|lint|typecheck|check)\b", CONFIRM, LOW, "build", "build script may write artifacts"),
    (r"^\s*(make|gradle\w*|mvn|cargo|go)\b.*\b(build|assemble|compile|package)\b", CONFIRM, LOW, "build", "build step may write artifacts"),
    (r"^\s*python3?\s+-m\s+compileall\b", ALLOW, SAFE, "build", "syntax check only"),
    (r"^\s*(python3?|node|ts-node)\s+[\w./\-]+\.(py|js|mjs|ts)\b", CONFIRM, LOW, "script", "running a repository script"),

    # --- read-only inspection -----------------------------------------
    (r"^\s*(ls|dir|cat|head|tail|wc|grep|rg|ag|find|tree|stat|file|du|df|sort|uniq|cut|tr|jq|diff|comm|column|nl|sed\s+-n|awk)\b",
     ALLOW, SAFE, "inspect", "read-only inspection"),
    (r"^\s*git\s+(status|log|diff|show|blame|branch|remote|rev-parse|ls-files|describe|shortlog|tag|stash\s+list|config\s+--get|worktree\s+list)\b",
     ALLOW, SAFE, "inspect-git", "read-only git"),
    (r"^\s*git\s+(add|restore|apply|commit|rm|mv|switch|checkout)\b", CONFIRM, MEDIUM, "git-write", "git working-tree change"),
    (r"^\s*(which|whereis|type|command\s+-v|uname|whoami|id|date|pwd|echo|basename|dirname|realpath)\b",
     ALLOW, SAFE, "inspect", "informational command"),
    (r"^\s*(pip|pip3|poetry|npm|yarn|pnpm)\s+(list|show|outdated|why|ls|view|info|audit)\b", ALLOW, SAFE, "inspect-packages", "dependency inventory"),
    (r"^\s*python3?\s+--version|^\s*node\s+--version|^\s*git\s+--version", ALLOW, SAFE, "inspect", "version probe"),
    (r"^\s*mkdir\b|^\s*touch\b|^\s*cp\b|^\s*mv\b", CONFIRM, LOW, "filesystem-write", "filesystem mutation inside the repo"),
)

_COMPILED_RULES = tuple((re.compile(pattern, re.I | re.M), decision, risk, category, reason) for pattern, decision, risk, category, reason in RULES)

#: Commands the agent may use for its own bookkeeping without user review.
INTERNAL_ALLOWLIST = {
    "git-apply": ALLOW,
    "git-revert": ALLOW,
    "git-diff": ALLOW,
}


class CommandPolicy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        allow_network = settings.sandbox.allow_network
        allow_install = settings.sandbox.allow_dependency_install
        rules = []
        for pattern, decision, risk, category, reason in RULES:
            # Network / install rules are *softened*, never removed, when the
            # operator opts in: still a confirmation, never silent.
            if category == "network" and allow_network and decision == DENY:
                decision, risk = CONFIRM, MEDIUM
                reason = "network access enabled by operator — confirm the destination"
            if category == "dependency-install" and allow_install and decision == DENY:
                decision, risk = CONFIRM, MEDIUM
                reason = "dependency install enabled by operator — confirm package source"
            rules.append((re.compile(pattern, re.I | re.M), decision, risk, category, reason))
        self.rules = tuple(rules)
        self.extra_allow = tuple(settings.sandbox.extra_allow)

    # -- evaluation ----------------------------------------------------
    def evaluate(self, argv: list[str] | str, *, cwd: Path | str | None = None, purpose: str = "") -> Verdict:
        if isinstance(argv, str):
            return self.evaluate_string(argv, cwd=cwd, purpose=purpose)
        if not argv:
            return Verdict(DENY, LOW, "empty", ["no command given"], [], [])
        command = " ".join(shlex.quote(part) for part in argv)
        joined = " ".join(argv)
        verdict = Verdict(decision=ALLOW, risk=SAFE, category="general", reasons=[], mitigations=[], argv=list(argv))

        # 1. explicit operator allowlist wins (still audited)
        for pattern in self.extra_allow:
            if re.search(pattern, joined):
                verdict.category = "operator-allow"
                verdict.reasons.append(f"matches operator allowlist entry {pattern!r}")
                return self._finalize(verdict, joined, cwd)

        # 2. shell metacharacters are impossible: we always exec argv directly
        for part in argv:
            if SHELL_METACHARACTERS.search(part) and part not in {"-", "--"} and not part.startswith("-"):
                verdict.decision = CONFIRM
                verdict.risk = MEDIUM
                verdict.category = "shell-metacharacters"
                verdict.reasons.append(f"argument {part!r} contains shell metacharacters; it is passed literally, not through a shell")
                verdict.mitigations.append("FixPilot never uses shell=True; arguments are exec'd literally")
                break

        # 3. ordered rule table (first match wins, most severe listed first)
        for pattern, decision, risk, category, reason in self.rules:
            if pattern.search(joined):
                verdict.decision = decision
                verdict.risk = risk
                verdict.category = category
                verdict.reasons.append(reason)
                break
        else:
            if not verdict.reasons:
                verdict.decision = CONFIRM
                verdict.risk = LOW
                verdict.category = "unknown"
                verdict.reasons.append("command is not in the reviewed allowlist; a human must approve it once")

        if self.settings.sandbox.allow_network is False and verdict.category == "network":
            verdict.decision = DENY
        if purpose:
            verdict.reasons.append(f"purpose: {purpose}")
        verdict.argv = list(argv)
        self._last_command = command  # type: ignore[attr-defined]
        return self._finalize(verdict, joined, cwd)

    def evaluate_string(self, command: str, *, cwd: Path | str | None = None, purpose: str = "") -> Verdict:
        """Parse + judge a raw command string coming from the phone."""
        raw = (command or "").strip()
        if not raw:
            return Verdict(DENY, LOW, "empty", ["empty command"], [], [])
        if SHELL_METACHARACTERS.search(raw):
            return Verdict(
                decision=DENY,
                risk=MEDIUM,
                category="shell-metacharacters",
                reasons=[
                    "command contains shell metacharacters (;, &, |, $, backticks, redirection) — FixPilot refuses to run compound shell commands"
                ],
                mitigations=["run the individual commands separately; FixPilot execs argv directly without a shell"],
                argv=[],
            )
        if SHELL_TOKEN_PREFIX.match(raw):
            return Verdict(DENY, CRITICAL, "privilege-escalation", ["privilege escalation is never permitted"], [], [])
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            return Verdict(DENY, LOW, "parse-error", [f"could not parse command: {exc}"], [], [])
        return self.evaluate(argv, cwd=cwd, purpose=purpose)

    def evaluate_patch_write(self, path: str) -> Verdict:
        from .secrets import is_sensitive_path

        sensitive, label = is_sensitive_path(path)
        if sensitive:
            return Verdict(DENY, HIGH, "secret-write", [f"{path} is a protected {label}"], ["store configuration changes in a secret manager"], [path])
        return Verdict(ALLOW, SAFE, "patch-write", ["path is writable by the agent"], [], [path])

    # -- internals -----------------------------------------------------
    def _finalize(self, verdict: Verdict, joined: str, cwd: Path | str | None) -> Verdict:
        # Path-escape checks: never operate outside the repository root.
        root = self.settings.repo_root
        for token in verdict.argv:
            if token.startswith("/") and not token.startswith(str(root)) and token not in {"/dev/null", "/tmp"}: 
                if token.startswith(("/etc", "/usr", "/var", "/System", "/bin", "/sbin", "/boot", "/root", "/home")):
                    if verdict.decision == ALLOW:
                        verdict.decision = CONFIRM
                        verdict.risk = MEDIUM
                        verdict.category = "path-escape"
                    verdict.reasons.append(f"touches a system path outside the repo: {token}")
                    verdict.mitigations.append("agent file writes are confined to the repository root")
        if cwd is not None:
            try:
                resolved = Path(cwd).resolve()
                if root.resolve() not in resolved.parents and resolved != root.resolve():
                    verdict.decision = DENY
                    verdict.risk = HIGH
                    verdict.reasons.append(f"working directory {resolved} is outside the repository root")
            except OSError:
                pass
        return verdict

    def describe(self) -> dict:
        return {
            "network_enabled": self.settings.sandbox.allow_network,
            "dependency_install_enabled": self.settings.sandbox.allow_dependency_install,
            "extra_allow": list(self.extra_allow),
            "rules": len(self.rules),
            "categories": sorted({rule[3] for rule in self.rules}),
        }
