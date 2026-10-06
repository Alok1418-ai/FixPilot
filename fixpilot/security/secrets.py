"""Secret detection and write protection.

Two jobs:

1. **Detect** credentials anywhere they might leak — prompts sent to a model,
   tool output shown on the phone, or a diff about to be applied.
2. **Block** the agent (and any generated patch) from touching files whose
   whole purpose is to hold credentials.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# --- credential-bearing paths (never auto-writable by an agent patch) ------

SENSITIVE_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^\.env(\..+)?$", "environment file"),
    (r"^\.envrc$", "direnv environment file"),
    (r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", "SSH key"),
    (r"\.(pem|key|p12|pfx|jks|keystore|asc)$", "private key material"),
    (r"^\.?npmrc$", "npm registry credentials"),
    (r"^\.?pypirc$", "PyPI credentials"),
    (r"^\.?netrc$", "netrc credentials"),
    (r"^\.git-credentials$", "stored git credentials"),
    (r"^credentials(\.json|\.yaml|\.yml|\.ini)?$", "cloud credentials"),
    (r"^secrets?\.(json|ya?ml|toml|ini|env)$", "secrets file"),
    (r"^service[-_]account.*\.json$", "service account key"),
    (r"^\.aws/", "AWS config/credentials"),
    (r"^\.ssh/", "SSH directory"),
    (r"^\.gnupg/", "GPG keyring"),
    (r"^\.kube/config$", "kubeconfig"),
    (r"^\.docker/config\.json$", "docker registry credentials"),
    (r"^\.terraform/", "terraform state"),
    (r"\.tfstate(\.backup)?$", "terraform state (holds secrets)"),
    (r"^\.htpasswd$", "password file"),
    (r"^shadow$|^passwd$", "system account file"),
    (r"^\.fixpilot/", "FixPilot internal state"),
    (r"^\.git/", "git internals"),
)

_SENSITIVE_COMPILED = tuple((re.compile(p, re.I), label) for p, label in SENSITIVE_NAME_PATTERNS)

# --- content patterns -----------------------------------------------------

CONTENT_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bsk-[A-Za-z0-9_\-]{20,}\b", "OpenAI-style API key", "high"),
    (r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b", "Anthropic API key", "high"),
    (r"\b(ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b", "GitHub token", "high"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "GitHub fine-grained token", "high"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id", "high"),
    (r"\bAIza[0-9A-Za-z_\-]{35}\b", "Google API key", "high"),
    (r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b", "Slack token", "high"),
    (r"\b[A-Za-z0-9_]{24}\.[A-Za-z0-9_]{6}\.[A-Za-z0-9_\-]{27,}\b", "Discord bot token", "medium"),
    (r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b", "JWT", "medium"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block", "critical"),
    (r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-/+.]{12,})", "assigned credential", "high"),
    (r"(?i)\b(password|passwd|pwd|db_pass)\b\s*[:=]\s*[\"']?([^\s\"';,]{8,})", "assigned password", "high"),
    (r"(?i)\bpostgres(?:ql)?://[^\s:@/]+:[^\s@/]+@", "database URL with password", "critical"),
    (r"(?i)\bmysql://[^\s:@/]+:[^\s@/]+@", "database URL with password", "critical"),
    (r"(?i)\bmongodb(\+srv)?://[^\s:@/]+:[^\s@/]+@", "database URL with password", "critical"),
    (r"(?i)\bredis://[^\s:@/]*:[^\s@/]+@", "redis URL with password", "high"),
    (r"(?i)\b(amqp|kafka)://[^\s:@/]+:[^\s@/]+@", "message broker URL with password", "high"),
    (r"(?i)-----BEGIN OPENSSH PRIVATE KEY-----", "OpenSSH private key", "critical"),
    (r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{20,}", "bearer token", "medium"),
    (r"(?i)\b(aws_secret_access_key)\b\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{20,})", "AWS secret key", "high"),
)

_CONTENT_COMPILED = tuple((re.compile(p), label, sev) for p, label, sev in CONTENT_PATTERNS)

PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:changeme|change_me|your[-_]?|xxx+|placeholder|example|dummy|fake|test|todo|none|null|redacted|\.\.\.|<>|\$\{.*\}|process\.env|os\.environ)"
)

ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")


def is_sensitive_path(path: str) -> tuple[bool, str]:
    """Is this path credential-bearing (and therefore off-limits to patches)?"""
    normalized = str(PurePosixPath(path.replace("\\", "/"))).lstrip("/")
    name = PurePosixPath(normalized).name
    for pattern, label in _SENSITIVE_COMPILED:
        if pattern.search(normalized) or pattern.search(name):
            return True, label
    return False, ""


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


@dataclass(slots=True)
class SecretFinding:
    label: str
    severity: str
    line: int
    preview: str
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "severity": self.severity,
            "line": self.line,
            "preview": self.preview,
            "path": self.path,
        }


def _looks_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.match(value.strip())) or value.strip() in {"", '""', "''"}


def _redact_value_only(match: re.Match[str]) -> str:
    """Redact the credential itself, keeping ``api_key=`` style labels readable."""
    if not match.lastindex:
        return "[REDACTED]"
    start, end = match.span(match.lastindex)
    outer_start, outer_end = match.span(0)
    if _looks_placeholder(match.group(match.lastindex)):
        return match.group(0)
    return match.string[outer_start:start] + "[REDACTED]" + match.string[end:outer_end]


def scan_text(text: str, *, path: str = "", entropy: bool = True, limit: int = 40) -> list[SecretFinding]:
    """Find credential-looking material in a blob of text."""
    if not text:
        return []
    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(findings) >= limit:
            break
        if len(line) > 4000:
            line = line[:4000]
        for pattern, label, severity in _CONTENT_COMPILED:
            match = pattern.search(line)
            if not match:
                continue
            captured = match.group(match.lastindex or 0) or match.group(0)
            if _looks_placeholder(captured):
                continue
            findings.append(
                SecretFinding(label=label, severity=severity, line=lineno, preview=_mask(match.group(0)), path=path)
            )
            break
        else:
            if entropy and not line.lstrip().startswith(("#", "//", "*")):
                for candidate in ENTROPY_RE.findall(line):
                    if _looks_placeholder(candidate):
                        continue
                    if shannon_entropy(candidate) >= 3.9 and not re.fullmatch(r"[0-9a-fA-F]+", candidate):
                        findings.append(
                            SecretFinding(label="high-entropy string", severity="low", line=lineno, preview=_mask(candidate), path=path)
                        )
                        break
    return findings


def _mask(value: str, keep: int = 4) -> str:
    value = value.strip()
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"


@dataclass(slots=True)
class WriteDecision:
    allowed: bool
    reason: str = ""
    severity: str = "none"

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "severity": self.severity}


class SecretScanner:
    """Central policy object for secrets — used by prompts, diffs and the API."""

    def __init__(self, block_writes: bool = True) -> None:
        self.block_writes = block_writes
        self.findings: list[SecretFinding] = []

    def check_write(self, path: str, content: str = "") -> WriteDecision:
        sensitive, label = is_sensitive_path(path)
        if sensitive and self.block_writes:
            return WriteDecision(False, f"refusing to modify {label} ({path}) — credentials are protected by policy", "high")
        if sensitive:
            return WriteDecision(True, f"{label}: allowed by configuration but review carefully", "medium")
        if content:
            found = scan_text(content, path=path, entropy=False)
            critical = [f for f in found if f.severity in {"critical", "high"}]
            if critical:
                preview = ", ".join(sorted({f.label for f in critical}))
                return WriteDecision(
                    False,
                    f"patch would write credential material into {path} ({preview}) — move it to a secret store",
                    "high",
                )
        return WriteDecision(True)

    def sanitize(self, text: str) -> str:
        """Redact secrets before text leaves the machine."""
        from ..util import redact

        cleaned = redact(text or "")
        for pattern, _label, severity in _CONTENT_COMPILED:
            if severity in {"critical", "high", "medium"}:
                cleaned = pattern.sub(_redact_value_only, cleaned)
        return cleaned

    def audit(self, text: str, *, path: str = "", context: str = "") -> list[SecretFinding]:
        found = scan_text(text, path=path)
        for item in found:
            item.path = item.path or path
        if found:
            self.findings.extend(found)
        return found
