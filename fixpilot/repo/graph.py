"""Code graph: import edges, symbol call edges, and change-impact analysis.

Change impact matters because a *safe* patch must not break the callers.  The
graph answers: "if I touch this file, which files are in the blast radius, and
which of those have tests?"
"""

from __future__ import annotations

import posixpath
from collections import deque
from dataclasses import dataclass, field

from .indexer import RepoIndex
from .symbols import is_test_path
from ..util import tokens


@dataclass(slots=True)
class ImpactReport:
    changed: list[str] = field(default_factory=list)
    dependent_files: list[str] = field(default_factory=list)
    dependent_tests: list[str] = field(default_factory=list)
    callers: dict[str, list[str]] = field(default_factory=dict)
    depth: int = 0

    def to_dict(self) -> dict:
        return {
            "changed": self.changed,
            "dependent_files": self.dependent_files,
            "dependent_tests": self.dependent_tests,
            "callers": self.callers,
            "depth": self.depth,
            "blast_radius": len(self.dependent_files),
        }


class CodeGraph:
    def __init__(self, index: RepoIndex) -> None:
        self.index = index
        self.imported_by: dict[str, set[str]] = {}
        self.imports: dict[str, set[str]] = {}
        self.called_by: dict[str, set[str]] = {}
        self._build()

    # -- construction --------------------------------------------------
    def _build(self) -> None:
        for path, facts in self.index.files.items():
            self.imports.setdefault(path, set())
            for module in facts.imports:
                for target in self._resolve_module(module, path):
                    if target != path:
                        self.imports[path].add(target)
                        self.imported_by.setdefault(target, set()).add(path)

        by_name: dict[str, list[str]] = {}
        for symbol in self.index.symbols.values():
            by_name.setdefault(symbol.name, []).append(symbol.id)
        for symbol in self.index.symbols.values():
            for call in symbol.calls:
                callee = call.split(".")[-1]
                for target_id in by_name.get(callee, [])[:4]:
                    if target_id != symbol.id:
                        self.called_by.setdefault(target_id, set()).add(symbol.id)

    def _resolve_module(self, module: str, from_path: str) -> list[str]:
        module = module.strip()
        if not module:
            return []
        found: list[str] = []
        if module.startswith("."):
            base_dir = posixpath.dirname(from_path)
            rel = module.lstrip(".")
            dotted = posixpath.normpath(posixpath.join(base_dir, rel.replace(".", "/"))) if rel else base_dir
            found.extend(self.index.files_in_module(dotted))
            return found
        # absolute import: try as-is, then progressively drop the leading package
        parts = module.split(".")
        for cut in range(len(parts)):
            candidate = ".".join(parts[cut:])
            found.extend(self.index.files_in_module(candidate))
            if found:
                break
        return found

    # -- queries -------------------------------------------------------
    def dependents_of(self, path: str, depth: int = 2) -> list[str]:
        """Transitive reverse-import closure, bounded by ``depth``."""
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(path, 0)])
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            for dependent in sorted(self.imported_by.get(current, ())):
                if dependent in seen or dependent == path:
                    continue
                seen.add(dependent)
                queue.append((dependent, level + 1))
        return sorted(seen)

    def callers_of(self, symbol_id: str) -> list[str]:
        return sorted(self.called_by.get(symbol_id, ()))

    def impact(self, paths: list[str], depth: int = 2) -> ImpactReport:
        changed = sorted({p for p in paths if p})
        dependents: set[str] = set()
        for path in changed:
            dependents.update(self.dependents_of(path, depth=depth))
        dependents -= set(changed)
        tests = sorted(p for p in dependents if is_test_path(p))
        tests += sorted(
            p
            for p, facts in self.index.files.items()
            if p not in changed and is_test_path(p) and self._mentions_any(p, changed)
        )
        callers: dict[str, list[str]] = {}
        for path in changed:
            for symbol in self.index.symbols.values():
                if symbol.path != path:
                    continue
                callers[symbol.name] = self.callers_of(symbol.id)[:8]
        return ImpactReport(
            changed=changed,
            dependent_files=sorted(dependents),
            dependent_tests=sorted(set(tests)),
            callers={k: v for k, v in callers.items() if v},
            depth=depth,
        )

    def _mentions_any(self, path: str, targets: list[str]) -> bool:
        facts = self.index.files.get(path)
        if not facts:
            return False
        stems = {posixpath.basename(t).rsplit(".", 1)[0] for t in targets}
        names: set[str] = set()
        for target in targets:
            for symbol in self.index.symbols.values():
                if symbol.path == target:
                    names.add(symbol.name)
        hit_names = {n for n in names if n}
        text_tokens = tokens(" ".join(facts.imports + facts.imported_names))
        if stems & text_tokens:
            return True
        for symbol in facts.symbols:
            if hit_names & set(symbol.calls):
                return True
        return False

    def tests_for(self, path: str) -> list[str]:
        stem = posixpath.basename(path).rsplit(".", 1)[0]
        candidates = []
        for candidate, facts in self.index.files.items():
            if not is_test_path(candidate):
                continue
            if stem in candidate or stem in " ".join(facts.imports) or stem in " ".join(facts.imported_names):
                candidates.append(candidate)
        return sorted(candidates)

    def resolve_symbol_id(self, path: str, name: str) -> str:
        needle = name.split(".")[-1]
        for symbol_id, symbol in self.index.symbols.items():
            if symbol.path == path and symbol.name == needle:
                return symbol_id
        return ""

    def module_summary(self) -> dict:
        return {
            "files": len(self.index.files),
            "import_edges": sum(len(v) for v in self.imports.values()),
            "call_edges": sum(len(v) for v in self.called_by.values()),
            "hubs": [
                {"path": path, "imported_by": len(deps)}
                for path, deps in sorted(self.imported_by.items(), key=lambda kv: -len(kv[1]))[:10]
            ],
        }
