"""Multimodal ingestion: voice transcripts, pasted logs, screenshots.

The phone is the input device, which means messy input: half-spoken sentences,
logs copied out of a terminal, and screenshots of a crash dialog with no text
layer at all.  This module normalises all of it into the same
:class:`IngestedInput` shape the agent loop consumes.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..security.secrets import SecretScanner, scan_text
from ..store import AuditLog
from ..util import clean_text, file_hash, new_id, now_iso, snippet_around, truncate_bytes, words

# --------------------------------------------------------------------------
# Log / stack-trace parsing
# --------------------------------------------------------------------------

TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\)", re.M)
PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$', re.M)
PY_ERROR = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit|Fault|Timeout|Failure))\b(?::\s*(?P<msg>.*))?$", re.M)
JS_FRAME = re.compile(r"^\s*at\s+(?:(?P<func>[\w$.<>\[\] ]+?)\s+\()?(?P<file>[^\s()]+?):(?P<line>\d+):(?P<col>\d+)\)?$", re.M)
GO_PANIC = re.compile(r"^(?:panic|fatal error):\s*(?P<msg>.*)$", re.M)
JAVA_FRAME = re.compile(r"^\s*at\s+(?P<class>[\w$.]+)\.(?P<func>[\w$<>]+)\((?P<file>[\w$]+\.java):(?P<line>\d+)\)", re.M)
GENERIC_FILELINE = re.compile(r"(?P<file>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}):(?P<line>\d+)(?::(?P<col>\d+))?")
ASSERT_LINE = re.compile(r"^\s*(?:E\s+)?(?P<kind>AssertionError|assert|expected|FAILED|ERROR)\b(?P<rest>.*)$", re.M)
TEST_SUMMARY = re.compile(r"^(?P<status>PASSED|FAILED|ERROR|OK|FAIL)\b.*$", re.M)
PYTEST_FAILED = re.compile(r"^(?P<node>::?[\w./\-]+::[\w\[\].\-]+|[\w./\-]+\.py::[\w\[\].\-]+)\s+(?P<status>FAILED|ERROR)", re.M)
LOG_LEVELS = re.compile(r"\b(?P<level>CRITICAL|FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b")


@dataclass(slots=True)
class LogSignal:
    """A single recoverable fact pulled out of a log or transcript."""

    kind: str            # frame | error | assertion | test_failure | panic | level | file_ref
    value: str = ""
    path: str = ""
    lineno: int = 0
    function: str = ""
    message: str = ""
    language: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "path": self.path,
            "lineno": self.lineno,
            "function": self.function,
            "message": self.message,
            "language": self.language,
            "raw": self.raw[:300],
        }


@dataclass(slots=True)
class IngestedInput:
    """Normalised command input regardless of channel."""

    id: str
    channel: str                                   # text | voice | log | image | mixed
    text: str = ""
    transcript: str = ""
    language: str = "en"
    signals: list[LogSignal] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    hint_intent: str = ""
    confidence: float = 0.0
    created_at: str = ""
    raw_length: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "channel": self.channel,
            "text": self.text,
            "transcript": self.transcript,
            "language": self.language,
            "signals": [s.to_dict() for s in self.signals],
            "attachments": self.attachments,
            "hint_intent": self.hint_intent,
            "confidence": round(self.confidence, 2),
            "created_at": self.created_at,
            "raw_length": self.raw_length,
            "warnings": self.warnings,
        }


# Spoken punctuation / code words that phones transcribe literally.
SPOKEN_FIXES: tuple[tuple[str, str], ...] = (
    (r"\b(?:dot|period)\s+(?=[a-z])", "."),
    (r"\bslash\b", "/"),
    (r"\bbackslash\b", "\\"),
    (r"\bunderscore\b", "_"),
    (r"\bhyphen\b|\bdash\b", "-"),
    (r"\bopen paren(?:thesis)?\b", "("),
    (r"\bclose paren(?:thesis)?\b", ")"),
    (r"\bequals\s+equals\b", "=="),
    (r"\bnew\s?line\b", "\n"),
    (r"\bat\s+sign\b", "@"),
    (r"\bcolon\b", ":"),
    (r"\bcomma\b", ","),
    (r"\bquote\b", '"'),
)

FILLERS = re.compile(r"\b(?:um+|uh+|erm+|like|you know|so yeah|kind of|basically)\b[,]?", re.I)

INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rollback", re.compile(r"\b(roll\s?back|revert|undo|go back|restore)\b", re.I)),
    ("verify", re.compile(r"\b(run (the )?tests?|verify|check (if )?(it|that) (works|passes)|test it)\b", re.I)),
    ("apply", re.compile(r"\b(apply (the )?(fix|patch)|do it|go ahead|ship it|make the change)\b", re.I)),
    ("explain", re.compile(r"\b(explain|why|what caused|root cause|walk me through|how come)\b", re.I)),
    ("investigate", re.compile(r"\b(investigate|dig into|diagnose|debug|look into|triage|analy[sz]e)\b", re.I)),
    ("fix", re.compile(r"\b(fix|repair|patch|resolve|correct|handle)\b", re.I)),
    ("review", re.compile(r"\b(review|read|summar(y|ise|ize)|status|what(?:'s| is) happening)\b", re.I)),
)


_SCRUBBER: SecretScanner | None = None


def scrub_secrets(text: str) -> tuple[str, int]:
    """Redact credential values from raw intake text.

    Bug reports are pasted straight out of terminals, so they routinely contain
    live tokens. Redaction happens *here* — at the boundary — so nothing derived
    from the report (sessions, evidence, prompts, reports, the phone UI) can
    carry the value. Line structure is preserved, so stack traces still parse.
    """
    global _SCRUBBER
    if not text:
        return "", 0
    if _SCRUBBER is None:
        _SCRUBBER = SecretScanner()
    findings = scan_text(text, entropy=False)
    if not findings:
        return text, 0
    return _SCRUBBER.sanitize(text), len(findings)


class InputAdapter:
    """Turns raw phone input into a normalised, evidence-rich input object."""

    def __init__(self, settings: Settings, audit: AuditLog | None = None) -> None:
        self.settings = settings
        self.audit = audit

    # -- entry points --------------------------------------------------
    def from_text(self, text: str, *, language: str = "en") -> IngestedInput:
        text, redacted = scrub_secrets(text or "")
        signals = extract_signals(text)
        warnings = []
        if redacted:
            warnings.append(f"{redacted} credential value(s) redacted at intake")
        if self.audit is not None and redacted:
            self.audit.record(
                {"actor": "intake", "event": "secrets.redacted", "count": redacted, "channel": "text"}
            )
        return IngestedInput(
            id=new_id("in"),
            channel="log" if _looks_like_log(text, signals) else "text",
            text=clean_text(text),
            language=language,
            signals=signals,
            hint_intent=detect_intent(text),
            confidence=0.9 if signals else 0.7,
            created_at=now_iso(),
            raw_length=len(text or ""),
            warnings=warnings,
        )

    def from_voice(self, transcript: str, *, language: str = "en", confidence: float = 0.8) -> IngestedInput:
        repaired, redacted = scrub_secrets(repair_transcript(transcript))
        signals = extract_signals(repaired)
        channel = "log" if _looks_like_log(repaired, signals) else "voice"
        return IngestedInput(
            id=new_id("in"),
            channel=channel,
            text=repaired,
            transcript=transcript,
            language=language,
            signals=signals,
            hint_intent=detect_intent(repaired),
            confidence=max(0.4, min(0.95, confidence)),
            created_at=now_iso(),
            raw_length=len(transcript or ""),
            warnings=[f"{redacted} credential value(s) redacted at intake"] if redacted else [],
        )

    def from_image(
        self,
        *,
        filename: str,
        data_url_or_base64: str = "",
        ocr_text: str = "",
        user_note: str = "",
        language: str = "en",
    ) -> IngestedInput:
        stored = self._persist_attachment(filename, data_url_or_base64)
        ocr_text, ocr_redacted = scrub_secrets(ocr_text)
        user_note, note_redacted = scrub_secrets(user_note)
        redacted = ocr_redacted + note_redacted
        signals = extract_signals(ocr_text or user_note)
        has_text_signal = bool(signals)
        vision_signal = ocr_text.strip() or user_note.strip()
        if not ocr_text.strip() and stored.get("path"):
            # No text layer: classify the screenshot so the router can pick a
            # vision-capable model and the prompt can be specific.
            stored["vision_hint"] = classify_screenshot(stored.get("path", ""), stored.get("bytes", 0))
        return IngestedInput(
            id=new_id("in"),
            channel="image",
            text=clean_text(user_note or ocr_text),
            language=language,
            signals=signals,
            attachments=[stored],
            hint_intent=detect_intent(user_note or "fix this screenshot").strip() or "fix",
            confidence=0.75 if has_text_signal else (0.5 if stored.get("path") else 0.3),
            created_at=now_iso(),
            raw_length=len(data_url_or_base64 or ocr_text or ""),
            warnings=(
                ([f"{redacted} credential value(s) redacted at intake"] if redacted else [])
                + ([] if vision_signal or stored.get("vision_hint") else ["no OCR text and no vision model may be available"])
            ),
        )

    def merge(self, parts: list[IngestedInput], *, hint: str = "") -> IngestedInput:
        """Combine several inputs (voice note + pasted log + screenshot)."""
        if not parts:
            return self.from_text(hint)
        text = "\n\n".join(p.text for p in parts if p.text)
        if hint:
            text = f"{hint}\n{text}"
        signals: list[LogSignal] = []
        attachments: list[dict] = []
        channels: list[str] = []
        for part in parts:
            signals.extend(part.signals)
            attachments.extend(part.attachments)
            channels.append(part.channel)
        deduped = _dedupe_signals(signals)
        channel = "mixed" if len(set(channels)) > 1 else channels[0]
        return IngestedInput(
            id=new_id("in"),
            channel=channel,
            text=clean_text(text),
            language=parts[0].language,
            signals=deduped,
            attachments=attachments,
            hint_intent=detect_intent(text) or parts[0].hint_intent,
            confidence=max(p.confidence for p in parts),
            created_at=now_iso(),
            raw_length=sum(p.raw_length for p in parts),
            warnings=[w for p in parts for w in p.warnings],
        )

    # -- helpers -------------------------------------------------------
    def _persist_attachment(self, filename: str, data: str) -> dict:
        info: dict = {
            "filename": filename or "screenshot.png",
            "stored_at": now_iso(),
            "bytes": 0,
            "path": "",
            "sha": "",
        }
        if not data:
            return info
        payload = data
        if data.startswith("data:"):
            _, _, payload = data.partition(",")
        try:
            blob = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            info["error"] = "invalid base64 payload"
            return info
        info["bytes"] = len(blob)
        info["sha"] = file_hash_from_bytes(blob)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "screenshot.png")[:80]
        target = self.settings.uploads_dir / f"{new_id('img', 8)}-{safe_name}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            info["path"] = str(target)
        except OSError as exc:
            info["error"] = f"could not store attachment: {exc}"
        if self.audit:
            self.audit.record({"kind": "input.attachment", "file": info["filename"], "bytes": info["bytes"], "sha": info["sha"]})
        return info


def file_hash_from_bytes(blob: bytes) -> str:
    import hashlib

    return hashlib.sha256(blob).hexdigest()[:16]


# --------------------------------------------------------------------------
# Transcript repair & intent
# --------------------------------------------------------------------------


def repair_transcript(transcript: str) -> str:
    """Undo the literalness of speech-to-text on code-heavy sentences."""
    text = clean_text(transcript)
    text = FILLERS.sub(" ", text)
    # Avoid mangling real sentences: only apply spoken punctuation when the
    # utterance already smells like code (identifiers, file extensions).
    if _looks_like_code(text):
        for pattern, replacement in SPOKEN_FIXES:
            text = re.sub(pattern, replacement, text, flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _looks_like_code(text: str) -> bool:
    if re.search(r"\.(py|js|ts|tsx|jsx|java|go|rb|rs|json|yaml|yml)\b", text, re.I):
        return True
    if re.search(r"[a-z]+_[a-z]+|\w+\.\w+\(", text):
        return True
    return len(re.findall(r"\b(?:def|class|import|return|function|const|var)\b", text, re.I)) >= 2


def detect_intent(text: str) -> str:
    for intent, pattern in INTENT_PATTERNS:
        if pattern.search(text or ""):
            return intent
    return ""


def _looks_like_log(text: str, signals: list[LogSignal]) -> bool:
    if not text:
        return False
    if any(s.kind in {"frame", "error", "panic", "test_failure"} for s in signals):
        return True
    markers = len(re.findall(r"\b(?:ERROR|FATAL|CRITICAL|WARNING|Traceback|Exception)\b", text))
    return markers >= 2


# --------------------------------------------------------------------------
# Signal extraction
# --------------------------------------------------------------------------


def extract_signals(text: str) -> list[LogSignal]:
    """Pull structured bug evidence out of arbitrary text."""
    if not text:
        return []
    text = clean_text(text)
    signals: list[LogSignal] = []

    if TRACEBACK_START.search(text):
        frames = [
            LogSignal(
                kind="frame",
                path=match.group("file"),
                lineno=int(match.group("line")),
                function=(match.group("func") or "").strip(),
                language="python",
                raw=match.group(0),
            )
            for match in PY_FRAME.finditer(text)
        ]
        # The deepest frame is the most likely culprit; keep the original order
        # (most recent call last) so ranking can prefer the tail.
        signals.extend(frames)
        for match in PY_ERROR.finditer(text):
            signals.append(
                LogSignal(kind="error", value=match.group("type"), message=(match.group("msg") or "").strip(), language="python", raw=match.group(0))
            )

    for match in JAVA_FRAME.finditer(text):
        signals.append(
            LogSignal(kind="frame", path=match.group("file"), lineno=int(match.group("line")), function=f"{match.group('class')}.{match.group('func')}", language="java", raw=match.group(0))
        )

    for match in JS_FRAME.finditer(text):
        path = match.group("file")
        if path.startswith(("node:", "internal/")) or path in {"<anonymous>"}:
            continue
        signals.append(
            LogSignal(kind="frame", path=path, lineno=int(match.group("line")), function=(match.group("func") or "").strip(), language="javascript", raw=match.group(0))
        )

    for match in GO_PANIC.finditer(text):
        signals.append(LogSignal(kind="panic", message=match.group("msg").strip(), language="go", raw=match.group(0)))

    for match in PYTEST_FAILED.finditer(text):
        signals.append(LogSignal(kind="test_failure", value=match.group("node"), message=match.group("status"), raw=match.group(0)))

    for match in ASSERT_LINE.finditer(text):
        signals.append(LogSignal(kind="assertion", value=match.group("kind"), message=match.group("rest").strip()[:200], raw=match.group(0)))

    if not any(s.kind == "frame" for s in signals):
        for match in list(GENERIC_FILELINE.finditer(text))[:12]:
            path = match.group("file")
            signals.append(LogSignal(kind="file_ref", path=path, lineno=int(match.group("line")), raw=match.group(0)))

    levels = {m.group("level").upper() for m in LOG_LEVELS.finditer(text)}
    for level in sorted(levels):
        if level in {"CRITICAL", "FATAL", "ERROR"}:
            signals.append(LogSignal(kind="level", value=level, raw=level))

    for match in TEST_SUMMARY.finditer(text):
        signals.append(LogSignal(kind="level", value=match.group("status"), raw=match.group(0).strip()[:120]))

    return _dedupe_signals(signals)


def _dedupe_signals(signals: list[LogSignal]) -> list[LogSignal]:
    seen: set[tuple] = set()
    out: list[LogSignal] = []
    for signal in signals:
        key = (signal.kind, signal.path, signal.lineno, signal.value, signal.message[:60])
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
    return out


def classify_screenshot(path: str, size: int = 0) -> str:
    """Cheap heuristic classification when no OCR/vision text is available.

    PNG/JPEG headers do not tell us much, so this is intentionally explicit
    about being a *hint* — the real classification comes from the vision model.
    """
    import os

    name = os.path.basename(path).lower()
    if any(token in name for token in ("traceback", "error", "crash", "stack")):
        return "stack_trace_screenshot"
    if any(token in name for token in ("ui", "screen", "app", "render", "layout")):
        return "ui_screenshot"
    if size and size < 40_000:
        return "likely_text_crop"
    return "unclassified_screenshot"


def looks_like_ui_bug(text: str) -> bool:
    """Detect front-end/UI complaints, which need different evidence."""
    patterns = (
        r"\b(button|modal|layout|css|styl(e|ing)|overflow|align|responsive|viewport|render(ing)?|dark mode|font|spacing|click|tap)\b",
    )
    return any(re.search(p, text or "", re.I) for p in patterns)


def extract_quoted_identifiers(text: str) -> list[str]:
    """Identifiers the developer explicitly named — strong localisation hints."""
    found: list[str] = []
    for pattern in (r"`([^`]+)`", r"'([A-Za-z_][\w.]{2,})'", r'"([A-Za-z_][\w.]{2,})"'):
        found.extend(re.findall(pattern, text or ""))
    identifiers = [token.strip() for token in found if re.fullmatch(r"[A-Za-z_][\w.]*", token.strip())]
    return list(dict.fromkeys(identifiers))[:12]


def summary_of(text: str, limit: int = 160) -> str:
    body, truncated = truncate_bytes(re.sub(r"\s+", " ", text or "").strip(), limit)
    return body + ("…" if truncated else "")


def first_error_line(text: str) -> str:
    match = PY_ERROR.search(text or "")
    if match:
        return match.group(0).strip()
    match = GO_PANIC.search(text or "")
    if match:
        return match.group(0).strip()
    for line in (text or "").splitlines():
        if LOG_LEVELS.search(line) and re.search(r"\b(ERROR|FATAL|CRITICAL)\b", line, re.I):
            return line.strip()[:200]
    return snippet_around(text or "", "Error", 80).strip().splitlines()[0] if text else ""


def keyword_signature(text: str, limit: int = 12) -> list[str]:
    stop = {
        "the", "a", "an", "is", "was", "were", "and", "or", "to", "of", "in", "on", "for", "with", "that",
        "this", "it", "i", "we", "my", "me", "when", "then", "but", "not", "no", "yes", "from", "at", "by",
    }
    counts: dict[str, int] = {}
    for word in words(text or ""):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in stop:
            continue
        counts[lowered] = counts.get(lowered, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]
