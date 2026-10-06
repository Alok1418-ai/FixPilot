"""Lessons learned: the project's debugging memory.

Every completed session leaves a lesson behind — symptom signature, evidence
that mattered, the strategy that worked (or did not), and how it was verified.
When a *similar* symptom appears again, FixPilot replays the lesson instead of
rediscovering the same root cause: the cheapest possible fix is the one you
already know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..store import read_json, write_json
from ..util import jaccard, now_iso, new_id, short_hash, tokens
from ..models.media import IngestedInput, LogSignal, keyword_signature


@dataclass(slots=True)
class Lesson:
    id: str
    symptom: str
    signature: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    root_cause: str = ""
    strategy: str = ""
    patch_summary: str = ""
    verification: str = ""
    outcome: str = "verified"          # verified | failed | rolled_back
    confidence: float = 0.5
    session_id: str = ""
    repo_key: str = ""
    reuse_count: int = 0
    created_at: str = ""
    last_reused: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symptom": self.symptom,
            "signature": self.signature,
            "error_types": self.error_types,
            "files": self.files,
            "root_cause": self.root_cause,
            "strategy": self.strategy,
            "patch_summary": self.patch_summary,
            "verification": self.verification,
            "outcome": self.outcome,
            "confidence": round(self.confidence, 2),
            "session_id": self.session_id,
            "repo_key": self.repo_key,
            "reuse_count": self.reuse_count,
            "created_at": self.created_at,
            "last_reused": self.last_reused,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Lesson":
        return cls(
            id=payload.get("id") or new_id("lesson", 8),
            symptom=payload.get("symptom", ""),
            signature=list(payload.get("signature", [])),
            error_types=list(payload.get("error_types", [])),
            files=list(payload.get("files", [])),
            root_cause=payload.get("root_cause", ""),
            strategy=payload.get("strategy", ""),
            patch_summary=payload.get("patch_summary", ""),
            verification=payload.get("verification", ""),
            outcome=payload.get("outcome", "verified"),
            confidence=float(payload.get("confidence", 0.5)),
            session_id=payload.get("session_id", ""),
            repo_key=payload.get("repo_key", ""),
            reuse_count=int(payload.get("reuse_count", 0)),
            created_at=payload.get("created_at") or now_iso(),
            last_reused=payload.get("last_reused", ""),
        )

    def render(self, detailed: bool = True) -> str:
        if not detailed:
            return f"“{self.symptom}” → {self.root_cause} (fix: {self.strategy})"
        parts = [
            f"Symptom: {self.symptom}",
            f"Root cause: {self.root_cause or 'unknown'}",
            f"Strategy that {'worked' if self.outcome == 'verified' else 'was tried'}: {self.strategy or 'unknown'}",
        ]
        if self.patch_summary:
            parts.append(f"Patch: {self.patch_summary[:300]}")
        if self.verification:
            parts.append(f"Verification: {self.verification[:200]}")
        parts.append(f"Outcome: {self.outcome} (confidence {self.confidence:.2f}, reused {self.reuse_count}x)")
        return "\n".join(parts)


@dataclass(slots=True)
class LessonMatch:
    lesson: Lesson
    score: float
    why: str

    def to_dict(self) -> dict:
        return {"lesson": self.lesson.to_dict(), "score": round(self.score, 3), "why": self.why}


class LessonStore:
    """Ranked recall over a repository's debugging history."""

    MAX_LESSONS = 300

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo_key = short_hash(str(settings.repo_root), length=12)
        self.path = settings.memory_dir / self.repo_key / "lessons.json"
        self._lessons: list[Lesson] | None = None

    # -- storage -------------------------------------------------------
    def all(self) -> list[Lesson]:
        if self._lessons is None:
            payload = read_json(self.path, {"lessons": []}) or {"lessons": []}
            self._lessons = [Lesson.from_dict(item) for item in payload.get("lessons", [])]
            self._prune()
        return self._lessons

    def _flush(self) -> None:
        lessons = self.all()
        lessons.sort(key=lambda lesson: (-lesson.confidence, lesson.created_at), reverse=False)
        write_json(self.path, {"repo_key": self.repo_key, "updated_at": now_iso(), "lessons": [l.to_dict() for l in lessons]})

    def _prune(self) -> None:
        lessons = self._lessons or []
        if len(lessons) <= self.MAX_LESSONS:
            return
        lessons.sort(key=lambda lesson: (lesson.confidence, lesson.reuse_count, lesson.created_at))
        self._lessons = lessons[len(lessons) - self.MAX_LESSONS :]

    # -- writing -------------------------------------------------------
    def record(
        self,
        *,
        session_id: str,
        ingested: IngestedInput | None,
        root_cause: str,
        strategy: str,
        patch_summary: str,
        verification: str,
        outcome: str,
        files: list[str],
        confidence: float = 0.6,
    ) -> Lesson:
        symptom = (ingested.text[:300] if ingested and ingested.text else "").strip()
        lesson = Lesson(
            id=new_id("lesson", 8),
            symptom=symptom or "unspecified symptom",
            signature=keyword_signature(symptom, limit=14),
            error_types=sorted({s.value for s in (ingested.signals if ingested else []) if s.kind == "error"},
                               ) or sorted({_error_token(s) for s in (ingested.signals if ingested else []) if s.kind == "assertion"} - {""}),
            files=sorted(set(files))[:12],
            root_cause=root_cause[:600],
            strategy=strategy[:200],
            patch_summary=patch_summary[:600],
            verification=verification[:300],
            outcome=outcome,
            confidence=max(0.15, min(0.98, confidence)),
            session_id=session_id,
            repo_key=self.repo_key,
            created_at=now_iso(),
        )
        # Merge with a near-identical earlier lesson instead of duplicating it.
        for existing in self.all():
            if existing.session_id and existing.session_id == session_id:
                continue
            if _same_symptom(existing, lesson):
                existing.root_cause = lesson.root_cause or existing.root_cause
                existing.strategy = lesson.strategy or existing.strategy
                existing.patch_summary = lesson.patch_summary or existing.patch_summary
                existing.verification = lesson.verification or existing.verification
                existing.outcome = lesson.outcome
                existing.confidence = max(existing.confidence, lesson.confidence)
                existing.reuse_count += 1
                self._flush()
                return existing
        self.all().append(lesson)
        self._prune()
        self._flush()
        return lesson

    # -- reading -------------------------------------------------------
    def find_similar(
        self,
        *,
        text: str,
        signals: list[LogSignal] | None = None,
        files: list[str] | None = None,
        limit: int = 3,
        min_score: float = 0.18,
    ) -> list[LessonMatch]:
        query_tokens = tokens(text or "")
        error_types = {s.value for s in (signals or []) if s.kind == "error"}
        file_set = set(files or [])
        matches: list[LessonMatch] = []
        for lesson in self.all():
            if lesson.outcome == "failed" and lesson.confidence < 0.4:
                continue
            token_score = jaccard(query_tokens, set(lesson.signature))
            error_score = len(error_types & set(lesson.error_types)) / max(1, len(error_types)) if error_types else 0.0
            file_score = len(file_set & set(lesson.files)) / max(1, len(file_set)) if file_set else 0.0
            score = token_score * 0.5 + error_score * 0.3 + file_score * 0.35
            score *= 0.9 + lesson.confidence * 0.2
            if score < min_score:
                continue
            why_bits = []
            if token_score:
                why_bits.append(f"{int(token_score * 100)}% symptom similarity")
            if error_score:
                why_bits.append(f"same error type ({', '.join(sorted(error_types & set(lesson.error_types)))})")
            if file_score:
                why_bits.append("overlapping files")
            matches.append(LessonMatch(lesson=lesson, score=score, why="; ".join(why_bits) or "weak similarity"))
        matches.sort(key=lambda m: -m.score)
        chosen = matches[:limit]
        for match in chosen:
            match.lesson.reuse_count += 1
            match.lesson.last_reused = now_iso()
        if chosen:
            self._flush()
        return chosen

    def strategy_hints(self, matches: list[LessonMatch]) -> list[str]:
        hints: list[str] = []
        for match in matches:
            if match.lesson.outcome == "verified" and match.lesson.strategy:
                hints.append(
                    f"A verified fix for a similar symptom used strategy '{match.lesson.strategy}' "
                    f"(files: {', '.join(match.lesson.files[:3]) or 'n/a'})"
                )
            elif match.lesson.strategy:
                hints.append(
                    f"Similar symptom previously attempted via '{match.lesson.strategy}' with outcome '{match.lesson.outcome}' — treat as a lead, not a conclusion"
                )
        return hints[:4]

    def summary(self) -> dict:
        lessons = self.all()
        return {
            "repo_key": self.repo_key,
            "total": len(lessons),
            "verified": len([l for l in lessons if l.outcome == "verified"]),
            "failed": len([l for l in lessons if l.outcome == "failed"]),
            "reused": sum(l.reuse_count for l in lessons),
            "recent": [l.to_dict() for l in sorted(lessons, key=lambda l: l.created_at, reverse=True)[:5]],
        }


def _same_symptom(a: Lesson, b: Lesson) -> bool:
    if a.signature and b.signature and jaccard(set(a.signature), set(b.signature)) > 0.7:
        return True
    if a.error_types and b.error_types and set(a.error_types) == set(b.error_types) and set(a.files) & set(b.files):
        return True
    return False


def _error_token(signal: LogSignal) -> str:
    if signal.value:
        return signal.value
    text = signal.message or signal.raw
    match = re.search(r"\b([A-Za-z_]+(?:Error|Exception|Failure|Failed))\b", text)
    return match.group(1) if match else ""
