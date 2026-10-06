"""Skill framework.

A *skill* is a small, composable unit of developer expertise: it receives the
codebase index, the parsed bug input and the sandbox, and returns evidence plus
hypotheses.  Skills never write to the repository — they only observe, so a
buggy skill cannot damage a user's working tree.  Patch authoring is a skill
too, but it is a *post-ranking* skill: it only runs once the ranker has picked
candidates worth fixing.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Sequence

from ..config import Settings
from ..memory.lessons import LessonStore
from ..memory.project import ProjectMemoryStore
from ..memory.sessions import Session
from ..models.media import IngestedInput
from ..models.router import ModelRouter
from ..repo.graph import CodeGraph
from ..repo.githistory import GitHistory
from ..repo.indexer import RepoIndex
from ..repo.symbols import is_test_path
from ..security.executor import SandboxExecutor
from ..util import clamp, jaccard, tokens

EVIDENCE_KINDS = (
    "traceback",
    "source",
    "history",
    "test",
    "dependency",
    "log",
    "pattern",
    "reproduction",
    "memory",
    "screenshot",
)

#: How believable is a whole *category* of root cause, before any evidence?
CATEGORY_PRIORS: dict[str, float] = {
    "exception": 0.85,
    "null-safety": 0.8,
    "boundary": 0.75,
    "state": 0.7,
    "concurrency": 0.65,
    "dependency": 0.7,
    "regression": 0.75,
    "config": 0.6,
    "ui": 0.6,
    "input-validation": 0.7,
    "resource": 0.6,
    "test": 0.5,
    "unknown": 0.4,
}


# --------------------------------------------------------------------------
# evidence / hypotheses
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    """One verifiable fact the agent can point at."""

    kind: str
    claim: str
    detail: str = ""
    path: str = ""
    lineno: int = 0
    snippet: str = ""
    confidence: float = 0.5
    source: str = ""
    skill: str = ""
    id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "claim": self.claim,
            "detail": self.detail,
            "path": self.path,
            "lineno": self.lineno,
            "snippet": self.snippet[:1200],
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "skill": self.skill,
        }

    def render(self) -> str:
        where = f"{self.path}:{self.lineno}" if self.path else self.source
        return f"[{self.kind}] {self.claim}" + (f" ({where})" if where else "")


@dataclass(slots=True)
class Hypothesis:
    """A candidate explanation for the reported failure."""

    cause: str
    category: str = "logic"
    explanation: str = ""
    confidence: float = 0.4
    evidence_ids: list[str] = field(default_factory=list)
    file: str = ""
    lineno: int = 0
    symbol: str = ""
    strategy: str = ""
    fix_plan: str = ""
    verification_plan: str = ""
    falsifier: str = ""
    skill: str = ""
    ranked_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cause": self.cause,
            "category": self.category,
            "explanation": self.explanation,
            "confidence": round(self.confidence, 2),
            "evidence_ids": self.evidence_ids,
            "file": self.file,
            "lineno": self.lineno,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "fix_plan": self.fix_plan,
            "verification_plan": self.verification_plan,
            "falsifier": self.falsifier,
            "skill": self.skill,
            "score": round(self.ranked_score, 3),
        }

    def key(self) -> str:
        return f"{self.category}:{self.file or ''}:{self.symbol or ''}:{self.cause[:60]}"


@dataclass(slots=True)
class SkillResult:
    """What a skill hands back to the engine."""

    skill: str
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "evidence": [e.to_dict() for e in self.evidence],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "outputs": self.outputs,
            "notes": self.notes,
            "duration_ms": self.duration_ms,
        }


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SkillContext:
    """Everything a skill is allowed to look at."""

    settings: Settings
    index: RepoIndex
    graph: CodeGraph
    history: GitHistory
    executor: SandboxExecutor
    router: ModelRouter
    memory: ProjectMemoryStore
    lessons: LessonStore
    ingested: IngestedInput
    session: Session
    emit: Any = None
    scratch: dict[str, Any] = field(default_factory=dict)

    # -- input ---------------------------------------------------------
    @property
    def text(self) -> str:
        return self.ingested.text or ""

    @property
    def signals(self):
        return self.ingested.signals

    def signal_frames(self) -> list:
        return [s for s in self.signals if s.kind == "frame"]

    def signal_errors(self) -> list:
        return [s for s in self.signals if s.kind in {"error", "assertion", "test_failure", "panic"}]

    # -- repository helpers -------------------------------------------
    def resolve_path(self, raw: str) -> str:
        """Map a path from a log line onto a file in the index."""
        if not raw:
            return ""
        candidate = raw.replace("\\", "/")
        if candidate in self.index.files:
            return candidate
        stripped = candidate.lstrip("./")
        for prefix in ("file://",):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
        parts = [p for p in stripped.split("/") if p not in {"", "."}]
        for size in range(len(parts), 0, -1):
            suffix = "/".join(parts[-size:])
            if suffix in self.index.files:
                return suffix
        for path in self.index.files:
            if path.endswith(parts[-1]) and len(parts) > 0:
                return path
        return ""

    def symbol_at(self, path: str, lineno: int):
        """The innermost symbol containing ``path:lineno``, if any."""
        best = None
        for symbol in self.index.symbols.values():
            if symbol.path != path:
                continue
            if symbol.lineno <= lineno <= symbol.end_lineno:
                if best is None or (symbol.end_lineno - symbol.lineno) < (best.end_lineno - best.lineno):
                    best = symbol
        return best

    # -- plumbing ------------------------------------------------------
    def log(self, kind: str, payload: dict | None = None) -> None:
        if callable(self.emit):
            self.emit(kind, payload)

    def evidence(self, **kwargs) -> Evidence:
        evidence = Evidence(**kwargs)
        evidence.id = f"ev_{len(self.scratch.get('evidence', [])) + 1}"
        self.scratch.setdefault("evidence", []).append(evidence)
        return evidence


# --------------------------------------------------------------------------
# skill
# --------------------------------------------------------------------------


class Skill:
    """Base class: subclasses set class attributes and implement ``run``."""

    name = "skill"
    title = "Skill"
    description = ""
    version = "1.0"
    priority = 50
    cost = "fast"                      # fast | normal | deep
    requires: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()     # regexes matched against the report text
    trigger_signals: tuple[str, ...] = ()
    trigger_failures: tuple[str, ...] = ()
    always = False
    post_ranking = False               # only runs after hypotheses are ranked

    def applies(self, ctx: SkillContext) -> bool:
        if self.languages and ctx.index.stats.languages:
            if not set(self.languages) & set(ctx.index.stats.languages):
                return False
        if self.always:
            return True
        text = ctx.text or ""
        if any(re.search(pattern, text, re.I) for pattern in self.triggers):
            return True
        signal_kinds = {s.kind for s in ctx.signals}
        if set(self.trigger_signals) & signal_kinds:
            return True
        failures = set(ctx.scratch.get("failure_categories", []) or [])
        if set(self.trigger_failures) & failures:
            return True
        return False

    def score(self, ctx: SkillContext) -> float:
        if not self.applies(ctx):
            return 0.0
        score = 0.35
        text = ctx.text or ""
        hits = sum(1 for pattern in self.triggers if re.search(pattern, text, re.I))
        if hits:
            score += min(0.4, 0.15 * hits)
        overlap = set(self.trigger_signals) & {s.kind for s in ctx.signals}
        score += min(0.35, 0.2 * len(overlap))
        if self.always:
            score = max(score, 0.5)
        if ctx.ingested.hint_intent and ctx.ingested.hint_intent in {"explain", "investigate", "fix"}:
            score += 0.05
        return clamp(score)

    def run(self, ctx: SkillContext) -> SkillResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "priority": self.priority,
            "cost": self.cost,
            "languages": list(self.languages),
            "triggers": list(self.triggers),
            "trigger_signals": list(self.trigger_signals),
            "always": self.always,
            "module": type(self).__module__,
        }


class SkillRegistry:
    """Ordered collection of skills, ranked per investigation."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill | type[Skill], *, replace: bool = False) -> Skill:
        instance = skill() if isinstance(skill, type) else skill
        if instance.name in self._skills and not replace:
            raise ValueError(f"skill {instance.name!r} already registered")
        self._skills[instance.name] = instance
        return instance

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: (-s.priority, s.name))

    def select(
        self,
        ctx: SkillContext,
        *,
        limit: int = 8,
        min_score: float = 0.2,
        cost_budget: str = "deep",
    ) -> list[tuple[Skill, float]]:
        allowed_costs = {"fast"} if cost_budget == "fast" else {"fast", "normal"} if cost_budget == "normal" else {"fast", "normal", "deep"}
        scored: list[tuple[Skill, float]] = []
        for skill in self.all():
            if skill.cost not in allowed_costs:
                continue
            if skill.post_ranking:
                continue  # needs a ranked hypothesis; the engine runs these separately
            score = skill.score(ctx)
            if score >= min_score:
                scored.append((skill, score))
        scored.sort(key=lambda item: (-item[1], -item[0].priority))
        return scored[:limit]

    def discover(self, package: str = "fixpilot.skills") -> int:
        """Import every submodule of ``package`` so third-party skills register."""
        count = 0
        try:
            module = importlib.import_module(package)
        except ImportError:
            return 0
        for _, name, is_pkg in pkgutil.iter_modules(module.__path__, prefix=f"{package}."):
            if is_pkg:
                continue
            try:
                importlib.import_module(name)
                count += 1
            except Exception:  # pragma: no cover - a broken external skill must not break startup
                continue
        return count

    def summary(self) -> list[dict]:
        return [skill.describe() for skill in self.all()]

    def __len__(self) -> int:
        return len(self._skills)


# --------------------------------------------------------------------------
# ranking helpers
# --------------------------------------------------------------------------


def rank_hypotheses(hypotheses: list[Hypothesis], ctx: SkillContext) -> list[Hypothesis]:
    """Blend skill confidence with evidence quality, category prior and history."""
    evidence_by_id = {e.id: e for e in ctx.scratch.get("evidence", [])}
    for hypothesis in hypotheses:
        support = [evidence_by_id.get(eid) for eid in hypothesis.evidence_ids]
        support = [e for e in support if e is not None]
        evidence_strength = 0.0
        if support:
            kinds = {e.kind for e in support}
            evidence_strength = clamp(sum(e.confidence for e in support) / len(support))
            if "traceback" in kinds:
                evidence_strength += 0.15
            if "history" in kinds:
                evidence_strength += 0.08
            if len(kinds) >= 3:
                evidence_strength += 0.07
        prior = CATEGORY_PRIORS.get(hypothesis.category, 0.4)
        base = hypothesis.confidence * 0.55 + evidence_strength * 0.3 + prior * 0.15
        if hypothesis.file and is_test_path(hypothesis.file):
            base *= 0.6  # the fix rarely lives in the test file itself
        if hypothesis.file and ctx.index.files.get(hypothesis.file):
            facts = ctx.index.files[hypothesis.file]
            risky = {entry.get("tag") for entry in (facts.risk_signals or []) if isinstance(entry, dict)}
            if facts.language == "python" and risky & {"bare-except", "swallowed-exception", "mutable-default"}:
                base += 0.1
        hypothesis.ranked_score = clamp(base)
    return sorted(hypotheses, key=lambda h: -h.ranked_score)


def dedupe_hypotheses(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Merge hypotheses that describe the same cause (keeps the strongest)."""
    merged: dict[str, Hypothesis] = {}
    for hypothesis in hypotheses:
        key = hypothesis.key()
        existing = merged.get(key)
        if existing is None:
            merged[key] = hypothesis
            continue
        existing.confidence = max(existing.confidence, hypothesis.confidence)
        existing.evidence_ids = list(dict.fromkeys(existing.evidence_ids + hypothesis.evidence_ids))
        if not existing.explanation:
            existing.explanation = hypothesis.explanation
        if not existing.strategy:
            existing.strategy = hypothesis.strategy
    return list(merged.values())


def text_similarity(a: str, b: str) -> float:
    return jaccard(tokens(a), tokens(b))


def dataclass_fields(cls) -> list[str]:
    return [f.name for f in fields(cls)]


__all__: Sequence[str] = [
    "CATEGORY_PRIORS",
    "EVIDENCE_KINDS",
    "Evidence",
    "Hypothesis",
    "Skill",
    "SkillContext",
    "SkillRegistry",
    "SkillResult",
    "dataclass_fields",
    "dedupe_hypotheses",
    "rank_hypotheses",
    "text_similarity",
]
