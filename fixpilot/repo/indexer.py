"""Repository indexer: walk a checkout and build a queryable structural map.

The index is what lets FixPilot ground a bug report in *evidence*: which file
defines the symbol in the traceback, who imports it, where the tests live, and
how to run them.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import Settings
from ..store import read_json, write_json
from ..util import is_probably_binary, rel_path, short_hash, tokens
from .symbols import FileFacts, Symbol, adapter_for, is_test_path, language_for

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".fixpilot", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", "coverage", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".nuxt", ".cache", "vendor", ".idea", ".vscode", "site-packages", ".terraform",
    ".gradle", ".tox", "bower_components", ".turbo", ".svelte-kit", "Pods", ".dart_tool",
}

DEPENDENCY_FILES = {
    "requirements.txt": "pip",
    "requirements-dev.txt": "pip",
    "pyproject.toml": "python-project",
    "setup.py": "setuptools",
    "setup.cfg": "setuptools",
    "Pipfile": "pipenv",
    "poetry.lock": "poetry",
    "package.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "go.mod": "go",
    "Cargo.toml": "cargo",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "Gemfile": "bundler",
    "composer.json": "composer",
}

CONVENTION_FILES = {
    "CONTRIBUTING.md", "README.md", "Makefile", "makefile", "justfile", "Justfile",
    "tox.ini", "pytest.ini", "conftest.py", ".eslintrc", ".eslintrc.js", ".eslintrc.json",
    "ruff.toml", ".pre-commit-config.yaml", "noxfile.py",
}


@dataclass(slots=True)
class RepoStats:
    files: int = 0
    source_files: int = 0
    test_files: int = 0
    lines: int = 0
    symbols: int = 0
    languages: dict[str, int] = field(default_factory=dict)
    indexed_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "source_files": self.source_files,
            "test_files": self.test_files,
            "lines": self.lines,
            "symbols": self.symbols,
            "languages": dict(sorted(self.languages.items(), key=lambda kv: -kv[1])),
            "indexed_ms": self.indexed_ms,
            "truncated": self.truncated,
        }


@dataclass(slots=True)
class RepoIndex:
    root: Path
    fingerprint: str = ""
    files: dict[str, FileFacts] = field(default_factory=dict)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    test_command: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    stats: RepoStats = field(default_factory=RepoStats)
    is_git: bool = False
    git_head: str = ""
    git_branch: str = ""
    _content_cache: dict[str, list[str]] = field(default_factory=dict, repr=False)

    # -- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "fingerprint": self.fingerprint,
            "files": {path: facts.to_dict() for path, facts in self.files.items()},
            "dependencies": self.dependencies,
            "test_command": self.test_command,
            "build_commands": self.build_commands,
            "conventions": self.conventions,
            "stats": self.stats.to_dict(),
            "is_git": self.is_git,
            "git_head": self.git_head,
            "git_branch": self.git_branch,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RepoIndex":
        index = cls(root=Path(payload.get("root", ".")))
        index.fingerprint = payload.get("fingerprint", "")
        index.dependencies = payload.get("dependencies", {})
        index.test_command = payload.get("test_command", [])
        index.build_commands = payload.get("build_commands", [])
        index.conventions = payload.get("conventions", [])
        index.is_git = payload.get("is_git", False)
        index.git_head = payload.get("git_head", "")
        index.git_branch = payload.get("git_branch", "")
        stats = payload.get("stats", {})
        index.stats = RepoStats(
            files=stats.get("files", 0),
            source_files=stats.get("source_files", 0),
            test_files=stats.get("test_files", 0),
            lines=stats.get("lines", 0),
            symbols=stats.get("symbols", 0),
            languages=stats.get("languages", {}),
            indexed_ms=stats.get("indexed_ms", 0),
            truncated=stats.get("truncated", False),
        )
        for path, raw in (payload.get("files") or {}).items():
            facts = FileFacts(
                path=path,
                language=raw.get("language", "text"),
                lines=raw.get("lines", 0),
                size=raw.get("size", 0),
                sha=raw.get("sha", ""),
                imports=list(raw.get("imports", [])),
                imported_names=list(raw.get("imported_names", [])),
                risk_signals=list(raw.get("risk_signals", [])),
                has_main=raw.get("has_main", False),
            )
            for symbol in raw.get("symbols", []):
                facts.symbols.append(
                    Symbol(
                        id=symbol.get("id", ""),
                        name=symbol.get("name", ""),
                        kind=symbol.get("kind", "function"),
                        path=symbol.get("path", path),
                        lineno=int(symbol.get("lineno", 1)),
                        end_lineno=int(symbol.get("end_lineno", symbol.get("lineno", 1))),
                        signature=symbol.get("signature", ""),
                        doc=symbol.get("doc", ""),
                        decorators=list(symbol.get("decorators", [])),
                        parent=symbol.get("parent"),
                        calls=list(symbol.get("calls", [])),
                        is_test=bool(symbol.get("is_test", False)),
                        is_async=bool(symbol.get("is_async", False)),
                        params=list(symbol.get("params", [])),
                    )
                )
            index.files[path] = facts
            for symbol in facts.symbols:
                index.symbols[symbol.id] = symbol
        return index

    def save(self, path: Path) -> None:
        write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path, root: Path) -> "RepoIndex | None":
        payload = read_json(path)
        if not payload:
            return None
        index = cls.from_dict(payload)
        index.root = root
        return index

    # -- queries -------------------------------------------------------
    def resolve_path(self, raw: str) -> str:
        """Map a path from a log line or a phone command onto an indexed file."""
        if not raw:
            return ""
        candidate = str(raw).replace("\\", "/")
        if candidate in self.files:
            return candidate
        stripped = candidate.lstrip("./")
        for prefix in ("file://",):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
        parts = [part for part in stripped.split("/") if part not in {"", "."}]
        for size in range(len(parts), 0, -1):
            suffix = "/".join(parts[-size:])
            if suffix in self.files:
                return suffix
        if parts:
            for path in self.files:
                if path.endswith(parts[-1]):
                    return path
        return ""

    def source_lines(self, path: str) -> list[str]:
        """Cached file contents (index paths are repo-relative POSIX paths)."""
        if path not in self._content_cache:
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._content_cache[path] = text.splitlines()
        return self._content_cache[path]

    def context(self, path: str, lineno: int, before: int = 6, after: int = 6) -> dict:
        lines = self.source_lines(path)
        if not lines:
            return {"path": path, "lineno": lineno, "text": "", "start": lineno, "end": lineno}
        idx = max(0, min(len(lines) - 1, lineno - 1))
        start = max(0, idx - before)
        end = min(len(lines), idx + after + 1)
        return {
            "path": path,
            "lineno": lineno,
            "start": start + 1,
            "end": end,
            "text": "\n".join(lines[start:end]),
            "lines": [{"n": start + i + 1, "text": line} for i, line in enumerate(lines[start:end])],
        }

    def line_at(self, path: str, lineno: int) -> str:
        lines = self.source_lines(path)
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1]
        return ""

    def find_symbols(self, name: str, exact: bool = True) -> list[Symbol]:
        if not name:
            return []
        needle = name.split(".")[-1]
        found = []
        for symbol in self.symbols.values():
            if (symbol.name == needle) if exact else (needle.lower() in symbol.name.lower()):
                found.append(symbol)
        found.sort(key=lambda s: (s.kind != "function", s.path, s.lineno))
        return found

    def search_symbols(self, query: str, limit: int = 25) -> list[Symbol]:
        """Fuzzy symbol search used by the phone UI and the router."""
        query_tokens = tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, Symbol]] = []
        for symbol in self.symbols.values():
            name_tokens = tokens(symbol.name)
            if not name_tokens:
                continue
            overlap = len(query_tokens & name_tokens)
            bonus = 0.5 if query.lower() in symbol.name.lower() else 0.0
            doc_overlap = len(query_tokens & tokens(symbol.doc)) * 0.3
            score = overlap + bonus + doc_overlap
            if score > 0:
                scored.append((score, symbol))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].lineno))
        return [symbol for _, symbol in scored[:limit]]

    def search_code(self, query: str, limit: int = 20) -> list[dict]:
        """Text search across indexed files, ranked by match density."""
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            return []
        results: list[tuple[float, dict]] = []
        for path, facts in self.files.items():
            if facts.language == "text":
                continue
            lines = self.source_lines(path)
            hits = [(i + 1, line) for i, line in enumerate(lines) if needle in line.lower()]
            if not hits:
                continue
            density = len(hits) / max(1, facts.lines)
            for lineno, line in hits[:5]:
                results.append(
                    (
                        density + (0.2 if is_test_path(path) else 0),
                        {"path": path, "lineno": lineno, "line": line.strip()[:300], "language": facts.language},
                    )
                )
        results.sort(key=lambda item: -item[0])
        return [payload for _, payload in results[:limit]]

    def files_in_module(self, dotted: str) -> list[str]:
        base = dotted.replace(".", "/")
        candidates = {f"{base}.py", f"{base}/__init__.py", f"{base}.js", f"{base}.ts", f"{base}.tsx"}
        return [path for path in candidates if path in self.files]

    def summary(self) -> dict:
        return {
            "root": str(self.root),
            "stats": self.stats.to_dict(),
            "is_git": self.is_git,
            "git_head": self.git_head,
            "git_branch": self.git_branch,
            "test_command": self.test_command,
            "build_commands": self.build_commands,
            "dependencies": self.dependencies,
            "conventions": self.conventions,
            "top_files": self._top_files(),
        }

    def _top_files(self, limit: int = 12) -> list[dict]:
        ranked = sorted(
            self.files.values(),
            key=lambda f: (-(len(f.symbols) + f.lines / 50), f.path),
        )
        return [
            {"path": f.path, "language": f.language, "lines": f.lines, "symbols": len(f.symbols), "tests": is_test_path(f.path)}
            for f in ranked[:limit]
        ]


# --------------------------------------------------------------------------
# Indexer
# --------------------------------------------------------------------------


class Indexer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.repo_root

    # -- public --------------------------------------------------------
    def build(self, force: bool = False) -> RepoIndex:
        import time

        started = time.perf_counter()
        fingerprint = self._fingerprint()
        cache_path = self.settings.memory_dir / f"index-{short_hash(str(self.root))}.json"
        if not force:
            cached = RepoIndex.load(cache_path, self.root)
            if cached is not None and cached.fingerprint == fingerprint and cached.files:
                return cached

        index = RepoIndex(root=self.root, fingerprint=fingerprint)
        index.is_git, index.git_head, index.git_branch = self._git_state()
        limits = self.settings.repo_limits
        truncated = False

        for path in self.iter_source_files():
            if len(index.files) >= limits.max_files:
                truncated = True
                break
            rel = rel_path(path, self.root)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            language = language_for(rel)
            if size > limits.max_file_bytes:
                index.files[rel] = FileFacts(path=rel, language=language, lines=0, size=size, sha="", risk_signals=[
                    {"tag": "skipped-large", "note": f"{size} bytes exceeds limit", "lineno": 0, "text": ""}
                ])
                continue
            if language in {"text", "markdown", "json", "yaml", "toml", "html", "css", "sql", "dockerfile", "makefile"}:
                text = self._read(path)
                facts = FileFacts(
                    path=rel,
                    language=language,
                    lines=len(text.splitlines()),
                    size=size,
                    sha=short_hash(text, length=16),
                )
                if path.name in CONVENTION_FILES:
                    index.conventions.append(rel)
                if path.name in DEPENDENCY_FILES:
                    self._read_dependencies(path, text, index.dependencies)
                index.files[rel] = facts
                index.stats.files += 1
                index.stats.lines += facts.lines
                index.stats.languages[language] = index.stats.languages.get(language, 0) + 1
                continue

            text = self._read(path)
            facts = adapter_for(language).extract(rel, text)
            facts.sha = short_hash(text, length=16)
            index.files[rel] = facts
            index.stats.files += 1
            index.stats.source_files += 1
            index.stats.lines += facts.lines
            index.stats.symbols += len(facts.symbols)
            index.stats.languages[language] = index.stats.languages.get(language, 0) + 1
            if is_test_path(rel):
                index.stats.test_files += 1
            for symbol in facts.symbols:
                index.symbols[symbol.id] = symbol

        index.stats.truncated = truncated
        index.test_command = self.detect_test_command(index)
        index.build_commands = self.detect_build_commands(index)
        index.stats.indexed_ms = int((time.perf_counter() - started) * 1000)
        try:
            index.save(cache_path)
        except OSError:
            pass
        return index

    def iter_source_files(self) -> Iterable[Path]:
        """Deterministic, ignore-aware walk of the repository."""
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            parts = set(path.relative_to(self.root).parts[:-1])
            if parts & SKIP_DIRS:
                continue
            if path.name.startswith(".") and path.suffix not in {".py", ".js", ".ts"}:
                if path.name not in CONVENTION_FILES and path.name not in DEPENDENCY_FILES:
                    continue
            if path.suffix in {".pyc", ".pyo", ".so", ".o", ".a", ".dll", ".exe", ".class", ".jar", ".zip", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".mov", ".lock"}:
                continue
            if path.name.endswith(".min.js") or path.name.endswith(".min.css"):
                continue
            if is_probably_binary(path):
                continue
            yield path

    # -- helpers -------------------------------------------------------
    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _read_dependencies(self, path: Path, text: str, into: dict[str, list[str]]) -> None:
        name = path.name
        if name.startswith("requirements"):
            into.setdefault("pip", []).extend(
                [line.split("==")[0].split(">=")[0].strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
            )
        elif name == "package.json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return
            into.setdefault("npm", []).extend(sorted((payload.get("dependencies") or {}).keys()))
            into.setdefault("npm-dev", []).extend(sorted((payload.get("devDependencies") or {}).keys()))
            scripts = payload.get("scripts") or {}
            if scripts:
                into.setdefault("npm-scripts", []).extend([f"{k}: {v}" for k, v in sorted(scripts.items())])
        elif name == "pyproject.toml":
            into.setdefault("python-project", []).extend(
                [line.strip().strip('",') for line in text.splitlines() if re.match(r"^\s*[\"']?[a-zA-Z0-9_.\-]+[\"']?\s*[><=~]", line)]
            )
        elif name == "go.mod":
            found = re.findall(r"^\s*([\w./\-]+)\s+v[\w.\-]+", text, re.M)
            into.setdefault("go", []).extend(found)
        elif name in {"pom.xml", "build.gradle"}:
            into.setdefault(name, []).extend(re.findall(r"<artifactId>([^<]+)</artifactId>", text))

    def _git_state(self) -> tuple[bool, str, str]:
        if not (self.root / ".git").exists():
            return False, "", ""
        head = self._git(["rev-parse", "HEAD"])
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        return bool(head.strip()), head.strip(), branch.strip()

    def _git(self, args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return proc.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def _fingerprint(self) -> str:
        """Cheap change detector: HEAD + file count + newest mtime."""
        head = self._git(["rev-parse", "HEAD"]).strip()
        newest = 0.0
        count = 0
        for path in self.iter_source_files():
            count += 1
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
            if count > 5000:
                break
        return short_hash(str(self.root), head, str(count), f"{newest:.0f}", length=20)

    # -- command detection --------------------------------------------
    def detect_test_command(self, index: RepoIndex) -> list[str]:
        python_tests = [path for path, facts in index.files.items() if is_test_path(path) and facts.language == "python"]
        if python_tests:
            declared = {
                name.lower().split("[")[0].split(">")[0].split("=")[0].strip()
                for values in index.dependencies.values()
                for name in values
            }
            has_pytest_dep = any("pytest" in name for name in declared)
            has_pytest_cfg = any(
                path.rsplit("/", 1)[-1] in {"pytest.ini", "tox.ini", "conftest.py", "setup.cfg"} for path in index.files
            )
            if not has_pytest_cfg:
                has_pytest_cfg = any(
                    path.endswith("pyproject.toml") and "pytest" in "\n".join(index.source_lines(path))
                    for path in index.files
                )
            # Only reach for pytest when the project actually declares or configures
            # it — otherwise the stdlib runner is the one that will exist.
            if has_pytest_dep or has_pytest_cfg:
                return ["python3", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
            return ["python3", "-m", "unittest", "discover", "-v"]
        if "package.json" in index.files:
            scripts = index.dependencies.get("npm-scripts", [])
            if any(s.startswith("test:") or s.startswith("test ") or s.startswith("test:") for s in scripts):
                for script in scripts:
                    if script.startswith("test:") or script.startswith("test "):
                        name = script.split(":", 1)[0] if ":" in script else script.split(" ")[0]
                        return ["npm", "run", name]
            if any(s.startswith("test ") or s == "test" for s in scripts) or "test" in " ".join(scripts):
                return ["npm", "test", "--silent"]
        if "go.mod" in index.files:
            return ["go", "test", "./..."]
        if "Cargo.toml" in index.files:
            return ["cargo", "test", "--quiet"]
        if "pom.xml" in index.files:
            return ["mvn", "-q", "-DskipTests=false", "test"]
        if "Makefile" in index.files or "makefile" in index.files:
            return ["make", "test"]
        return []

    def detect_build_commands(self, index: RepoIndex) -> list[str]:
        commands: list[str] = []
        if "package.json" in index.files:
            if any("build" in s for s in index.dependencies.get("npm-scripts", [])):
                commands.append("npm run build")
            if any("lint" in s for s in index.dependencies.get("npm-scripts", [])):
                commands.append("npm run lint")
        if "pyproject.toml" in index.files:
            commands.append("python3 -m compileall -q .")
        if "Makefile" in index.files:
            commands.append("make build")
        return commands


def compile_check(files: Iterable[Path]) -> list[str]:
    """Return a list of syntax errors across the given Python files."""
    import ast

    problems: list[str] = []
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"{path}:{exc.lineno}: {exc.msg}")
        except OSError:
            continue
    return problems
