"""Project-level memory.

FixPilot remembers what a repository *is*: how to run its tests, what it treats
as conventions, which files are load-bearing, and the operational facts a new
contributor would need a week to learn.  Memory is versioned JSON on disk, so the
phone can reconnect to the workstation and keep the same context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Settings
from ..repo.indexer import RepoIndex
from ..store import read_json, write_json
from ..util import jaccard, now_iso, new_id, short_hash, tokens

SCHEMA_VERSION = 2

FRAMEWORK_SIGNALS: dict[str, tuple[str, ...]] = {
    "django": ("django", "manage.py", "settings.py"),
    "flask": ("flask",),
    "fastapi": ("fastapi", "starlette"),
    "express": ("express",),
    "react": ("react", "react-dom"),
    "next": ("next",),
    "vue": ("vue",),
    "svelte": ("svelte",),
    "pytest": ("pytest",),
    "unittest": ("unittest",),
    "jest": ("jest",),
    "vitest": ("vitest",),
    "spring": ("spring-boot", "springframework"),
    "rails": ("rails",),
    "gin": ("gin-gonic",),
}


@dataclass(slots=True)
class MemoryFact:
    """One durable statement about the project, with provenance."""

    id: str
    kind: str                # architecture | command | convention | gotcha | dependency | preference | outcome
    statement: str
    evidence: str = ""
    confidence: float = 0.6
    source: str = "index"    # index | session:<id> | user
    created_at: str = ""
    last_used: str = ""
    hits: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "statement": self.statement,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "hits": self.hits,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MemoryFact":
        return cls(
            id=payload.get("id") or new_id("fact", 8),
            kind=payload.get("kind", "architecture"),
            statement=payload.get("statement", ""),
            evidence=payload.get("evidence", ""),
            confidence=float(payload.get("confidence", 0.6)),
            source=payload.get("source", "index"),
            created_at=payload.get("created_at") or now_iso(),
            last_used=payload.get("last_used", ""),
            hits=int(payload.get("hits", 0)),
        )


@dataclass(slots=True)
class ProjectMemory:
    repo_key: str
    repo_root: str
    name: str = ""
    version: int = SCHEMA_VERSION
    updated_at: str = ""
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    test_command: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    key_files: list[dict] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    facts: list[MemoryFact] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": self.version,
            "repo_key": self.repo_key,
            "repo_root": self.repo_root,
            "name": self.name,
            "updated_at": self.updated_at,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "test_command": self.test_command,
            "build_commands": self.build_commands,
            "package_managers": self.package_managers,
            "entrypoints": self.entrypoints,
            "key_files": self.key_files,
            "conventions": self.conventions,
            "facts": [f.to_dict() for f in self.facts],
            "preferences": self.preferences,
            "stats": self.stats,
            "git": self.git,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ProjectMemory":
        memory = cls(
            repo_key=payload.get("repo_key", "unknown"),
            repo_root=payload.get("repo_root", ""),
            name=payload.get("name", ""),
            version=int(payload.get("schema", SCHEMA_VERSION)),
        )
        memory.updated_at = payload.get("updated_at", "")
        memory.languages = payload.get("languages", {})
        memory.frameworks = payload.get("frameworks", [])
        memory.test_command = payload.get("test_command", [])
        memory.build_commands = payload.get("build_commands", [])
        memory.package_managers = payload.get("package_managers", [])
        memory.entrypoints = payload.get("entrypoints", [])
        memory.key_files = payload.get("key_files", [])
        memory.conventions = payload.get("conventions", [])
        memory.preferences = payload.get("preferences", {})
        memory.stats = payload.get("stats", {})
        memory.git = payload.get("git", {})
        memory.facts = [MemoryFact.from_dict(item) for item in payload.get("facts", [])]
        return memory

    # -- queries -------------------------------------------------------
    def recall(self, query: str, *, kinds: Iterable[str] | None = None, limit: int = 8) -> list[MemoryFact]:
        query_tokens = tokens(query or "")
        wanted = set(kinds) if kinds else None
        scored: list[tuple[float, MemoryFact]] = []
        for fact in self.facts:
            if wanted and fact.kind not in wanted:
                continue
            overlap = jaccard(query_tokens, tokens(fact.statement + " " + fact.evidence)) if query_tokens else fact.confidence
            score = overlap * 0.7 + fact.confidence * 0.3 + min(fact.hits, 5) * 0.01
            if score > 0.05:
                scored.append((score, fact))
        scored.sort(key=lambda item: -item[0])
        chosen = [fact for _, fact in scored[:limit]]
        for fact in chosen:
            fact.hits += 1
            fact.last_used = now_iso()
        return chosen

    def facts_of(self, kind: str) -> list[MemoryFact]:
        return [f for f in self.facts if f.kind == kind]

    def brief(self, limit: int = 6) -> str:
        """Token-cheap project briefing handed to language models."""
        lines = [
            f"Project: {self.name or self.repo_key}",
            f"Languages: {', '.join(list(self.languages)[:5]) or 'unknown'}",
        ]
        if self.frameworks:
            lines.append(f"Frameworks: {', '.join(self.frameworks[:6])}")
        if self.test_command:
            lines.append(f"Tests: {' '.join(self.test_command)}")
        if self.conventions:
            lines.append(f"Conventions: {', '.join(self.conventions[:5])}")
        high = sorted(self.facts, key=lambda f: -f.confidence)[:limit]
        if high:
            lines.append("Known facts:")
            lines.extend(f"  - [{f.kind}] {f.statement}" for f in high)
        return "\n".join(lines)

    def summary(self) -> dict:
        return {
            "repo_key": self.repo_key,
            "name": self.name,
            "updated_at": self.updated_at,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "test_command": self.test_command,
            "build_commands": self.build_commands,
            "conventions": self.conventions[:10],
            "key_files": self.key_files[:8],
            "facts": len(self.facts),
            "facts_by_kind": {kind: len(self.facts_of(kind)) for kind in {f.kind for f in self.facts}},
            "preferences": self.preferences,
            "git": self.git,
            "stats": self.stats,
        }


class ProjectMemoryStore:
    """Load / update / persist :class:`ProjectMemory` per repository."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo_key = short_hash(str(settings.repo_root), length=12)
        self.path = settings.memory_dir / self.repo_key / "project.json"
        self._memory: ProjectMemory | None = None

    # -- access --------------------------------------------------------
    def load(self) -> ProjectMemory:
        if self._memory is not None:
            return self._memory
        payload = read_json(self.path)
        if payload:
            self._memory = ProjectMemory.from_dict(payload)
        else:
            self._memory = ProjectMemory(repo_key=self.repo_key, repo_root=str(self.settings.repo_root))
        return self._memory

    def save(self, memory: ProjectMemory | None = None) -> None:
        memory = memory or self.load()
        memory.updated_at = now_iso()
        write_json(self.path, memory.to_dict())

    # -- learning ------------------------------------------------------
    def learn_from_index(self, index: RepoIndex) -> ProjectMemory:
        memory = self.load()
        memory.languages = {k: v for k, v in sorted(index.stats.languages.items(), key=lambda kv: -kv[1])}
        memory.test_command = list(index.test_command)
        memory.build_commands = list(index.build_commands)
        memory.conventions = list(dict.fromkeys(index.conventions))
        memory.stats = index.stats.to_dict()
        memory.git = {
            "is_git": index.is_git,
            "head": index.git_head,
            "branch": index.git_branch,
        }
        dependency_names = {name.lower() for values in index.dependencies.values() for name in values}
        memory.package_managers = sorted(index.dependencies.keys())
        memory.frameworks = sorted(
            framework
            for framework, signals in FRAMEWORK_SIGNALS.items()
            if any(signal in name for name in dependency_names for signal in signals)
        )
        memory.key_files = index._top_files(limit=10)
        memory.name = memory.name or self._guess_name(index)
        memory.entrypoints = self._detect_entrypoints(index)
        self._upsert_fact(
            memory,
            kind="command",
            statement=f"Run the test suite with: {' '.join(index.test_command)}" if index.test_command else "No test command detected yet",
            evidence="repository fingerprint and build metadata",
            confidence=0.9 if index.test_command else 0.35,
            source="index",
        )
        if index.stats.languages:
            top = list(index.stats.languages.items())[0]
            self._upsert_fact(
                memory,
                kind="architecture",
                statement=f"Primarily {top[0]} ({top[1]} files); {index.stats.symbols} symbols indexed across {index.stats.files} files",
                evidence="static index",
                confidence=0.95,
            )
        for path in memory.entrypoints[:3]:
            self._upsert_fact(memory, kind="architecture", statement=f"Entry point: {path}", evidence="convention scan", confidence=0.7)
        self.save(memory)
        return memory

    def record_fact(
        self,
        kind: str,
        statement: str,
        *,
        evidence: str = "",
        confidence: float = 0.6,
        source: str = "user",
    ) -> MemoryFact:
        memory = self.load()
        fact = self._upsert_fact(
            memory, kind=kind, statement=statement, evidence=evidence, confidence=confidence, source=source
        )
        self.save(memory)
        return fact

    def record_outcome(
        self,
        *,
        session_id: str,
        summary: str,
        success: bool,
        files: list[str],
        strategy: str = "",
    ) -> MemoryFact | None:
        memory = self.load()
        if not success and not files:
            return None
        statement = summary.strip() or ("verified fix" if success else "unverified attempt")
        return self._upsert_fact(
            memory,
            kind="outcome",
            statement=statement,
            evidence=f"{'verified' if success else 'failed'} · strategy={strategy or 'unknown'} · files={', '.join(files[:4])}",
            confidence=0.85 if success else 0.4,
            source=f"session:{session_id}",
        ) and self.save(memory) or memory.facts[-1]

    def set_preference(self, key: str, value: Any) -> None:
        memory = self.load()
        memory.preferences[key] = value
        self.save(memory)

    def forget(self, fact_id: str) -> bool:
        memory = self.load()
        before = len(memory.facts)
        memory.facts = [f for f in memory.facts if f.id != fact_id]
        self.save(memory)
        return len(memory.facts) < before

    def prune(self, max_facts: int = 400) -> int:
        memory = self.load()
        if len(memory.facts) <= max_facts:
            return 0
        memory.facts.sort(key=lambda f: (-f.confidence, f.hits, f.created_at))
        removed = len(memory.facts) - max_facts
        memory.facts = memory.facts[:max_facts]
        self.save(memory)
        return removed

    # -- internals -----------------------------------------------------
    @staticmethod
    def _upsert_fact(
        memory: ProjectMemory,
        *,
        kind: str,
        statement: str,
        evidence: str = "",
        confidence: float = 0.6,
        source: str = "index",
    ) -> MemoryFact:
        for fact in memory.facts:
            if fact.kind == kind and _similar(fact.statement, statement):
                fact.confidence = max(fact.confidence, confidence)
                fact.evidence = evidence or fact.evidence
                fact.last_used = now_iso()
                return fact
        fact = MemoryFact(
            id=new_id("fact", 8),
            kind=kind,
            statement=statement,
            evidence=evidence,
            confidence=confidence,
            source=source,
            created_at=now_iso(),
        )
        memory.facts.append(fact)
        return fact

    @staticmethod
    def _guess_name(index: RepoIndex) -> str:
        for candidate in ("pyproject.toml", "package.json"):
            payload = index.dependencies
            if candidate == "package.json" and "npm-scripts" in payload:
                return index.root.name
        return index.root.name

    @staticmethod
    def _detect_entrypoints(index: RepoIndex) -> list[str]:
        candidates: list[str] = []
        patterns = (
            re.compile(r"^(__main__|main|app|server|cli|manage|index)\.(py|js|ts|mjs)$"),
            re.compile(r"^(bin|cmd)/"),
        )
        for path, facts in index.files.items():
            name = path.rsplit("/", 1)[-1]
            if any(p.search(name) or p.search(path) for p in patterns):
                candidates.append(path)
            elif facts.has_main:
                candidates.append(path)
        funcs = sorted(set(candidates), key=lambda p: (p.count("/"), len(p), p))
        return funcs[:8]


def _similar(a: str, b: str) -> bool:
    return jaccard(tokens(a), tokens(b)) > 0.7
