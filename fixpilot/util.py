"""Small shared helpers: ids, time, hashing, redaction, text windows."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# --------------------------------------------------------------------------
# Time & ids
# --------------------------------------------------------------------------


def now_iso() -> str:
    """UTC timestamp, second precision, Z-suffixed (stable across hosts)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str, length: int = 10) -> str:
    return f"{prefix}_{secrets.token_hex(length // 2 + length % 2)[:length]}"


def short_hash(*parts: str, length: int = 12) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest[:length]


def stable_hash(value: Any, length: int = 16) -> str:
    return hashlib.sha256(repr(value).encode("utf-8", "replace")).hexdigest()[:length]


def file_hash(path: Path, length: int = 16) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:length]


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def clean_text(text: str, limit: int | None = None) -> str:
    """Normalise noisy terminal / OCR text."""
    if not text:
        return ""
    text = strip_ansi(text.replace("\r\n", "\n").replace("\r", "\n"))
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = text.strip()
    if limit is not None and len(text) > limit:
        text = text[:limit] + "\n... [truncated]"
    return text


WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def words(text: str) -> list[str]:
    return WORD_RE.findall(text or "")


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in words(text or ""):
        for part in split_identifier(raw):
            if len(part) > 1:
                out.add(part.lower())
    return out


def split_identifier(name: str) -> list[str]:
    """``HTTPServerError`` / ``get_user_id`` -> [http, server, error, ...]."""
    if not name:
        return []
    parts = re.split(r"[_\-\s]+", name)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        chunks = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part)
        out.extend(chunks or [part])
    return out


def snippet_around(text: str, needle: str, radius: int = 400) -> str:
    idx = text.find(needle)
    if idx < 0:
        return text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    return text[start:end]


def line_window(lines: list[str], lineno: int, before: int = 3, after: int = 3) -> str:
    """1-based line number -> surrounding source text."""
    idx = max(0, lineno - 1)
    start = max(0, idx - before)
    end = min(len(lines), idx + after + 1)
    return "\n".join(lines[start:end])


def excerpt(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore"), True


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_text(path: Path, limit: int | None = None) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            data = handle.read() if limit is None else handle.read(limit)
    except OSError:
        return ""
    return data


def is_probably_binary(path: Path, probe: int = 2048) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(probe)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    if not chunk:
        return False
    printable = sum(1 for byte in chunk if 32 <= byte < 127 or byte in (9, 10, 13))
    return printable / len(chunk) < 0.85


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f}{unit}" if unit == "B" else f"{count:.1f}{unit}"
        count /= 1024
    return f"{count:.1f}GB"


def rel_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    bucket: list[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


# --------------------------------------------------------------------------
# Redaction (defence in depth — see sandbox/secrets.py for the scanner)
# --------------------------------------------------------------------------

REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})"), r"\1=[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b(ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}\b"), "gh_[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "AKIA[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer [REDACTED]"),
)


def redact(text: str) -> str:
    """Best-effort secret removal for anything leaving the machine."""
    if not text:
        return ""
    out = text
    for pattern, replacement in REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_env(env: dict[str, str]) -> dict[str, str]:
    return {key: redact(value) for key, value in env.items()}


# --------------------------------------------------------------------------
# Scoring helpers
# --------------------------------------------------------------------------


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def confidence_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.3:
        return "low"
    return "speculative"


@dataclass(slots=True)
class Timer:
    """Tiny context manager used to time agent stages."""

    label: str = ""
    start: float = 0.0
    elapsed_ms: int = 0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
