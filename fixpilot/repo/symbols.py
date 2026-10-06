"""Language adapters that turn source text into structural facts.

Python uses the stdlib :mod:`ast` module (exact).  Everything else uses
deliberately conservative regex extractors — good enough to locate symbols,
imports, and test files without shipping a parser per language, and never
wrong in a way that could corrupt a patch (patches are always anchored to
real file content and re-verified after writing).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Symbol:
    """A named, addressable piece of code."""

    id: str
    name: str
    kind: str  # function | method | class | variable | component
    path: str
    lineno: int
    end_lineno: int
    signature: str = ""
    doc: str = ""
    decorators: list[str] = field(default_factory=list)
    parent: str | None = None
    calls: list[str] = field(default_factory=list)
    is_test: bool = False
    is_async: bool = False
    params: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "lineno": self.lineno,
            "end_lineno": self.end_lineno,
            "signature": self.signature,
            "doc": self.doc,
            "decorators": self.decorators,
            "parent": self.parent,
            "calls": self.calls,
            "is_test": self.is_test,
            "is_async": self.is_async,
            "params": self.params,
        }


@dataclass(slots=True)
class FileFacts:
    path: str
    language: str
    lines: int
    size: int
    sha: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    imported_names: list[str] = field(default_factory=list)
    risk_signals: list[dict] = field(default_factory=list)
    has_main: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "lines": self.lines,
            "size": self.size,
            "sha": self.sha,
            "imports": self.imports,
            "imported_names": self.imported_names,
            "risk_signals": self.risk_signals,
            "has_main": self.has_main,
            "symbols": [s.to_dict() for s in self.symbols],
        }


LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
}

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)(/|$)"
    r"|(^|/)(test_|conftest|.*\.(test|spec)\.)"
    r"|_test\.(go|py|rb|js|ts|kt)$"
    r"|(^|/)Test[A-Z]\w*\.(java|kt)$",
    re.I,
)


def language_for(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith("dockerfile"):
        return "dockerfile"
    if lowered.endswith("makefile"):
        return "makefile"
    dot = lowered.rfind(".")
    if dot < 0:
        return "text"
    return LANGUAGES.get(lowered[dot:], "text")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


# --------------------------------------------------------------------------
# Risk signals (cheap static smells — they feed root-cause ranking)
# --------------------------------------------------------------------------

RISK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"except\s*:\s*$", "bare-except", "bare except swallows every error"),
    (r"except\s+Exception\s*:\s*(pass|return\s+None)\s*$", "swallowed-exception", "exception is swallowed"),
    (r"def\s+\w+\([^)]*=\s*(\[\]|\{\})\s*[,)]", "mutable-default", "mutable default argument shared between calls"),
    (r"\b(eval|exec)\s*\(", "dynamic-exec", "dynamic code execution"),
    (r"shell\s*=\s*True", "shell-true", "shell=True command injection surface"),
    (r"subprocess\.\w+\(", "subprocess", "external process launched"),
    (r"(?<![\w.])open\s*\([^)]*\)\s*$", "open-without-with", "file opened without context manager"),
    (r"TODO|FIXME|HACK|XXX", "todo-marker", "unfinished code marker"),
    (r"\bawait\b", "async", "asynchronous call"),
    (r"time\.sleep\(", "sleep", "sleep in code path (timing sensitive)"),
    (r"\.get\([^)]*\)\s*[-+*/]", "none-arithmetic", "arithmetic on a .get() result that may be None"),
    (r"\[\s*\d+\s*:\s*\]|\[\s*:\s*-?\d+\s*\]", "slice", "slice indexing (off-by-one prone)"),
    (r"range\s*\(\s*len\s*\(", "range-len", "range(len(...)) indexing"),
    (r"==\s*None|!=\s*None", "none-eq", "comparison to None with =="),
    (r"\bfloat\s*\(", "float-parse", "string-to-float conversion"),
    (r"\bint\s*\(\s*[a-zA-Z_]", "int-parse", "string-to-int conversion"),
    (r"//\s*2|/\s*2\b", "halving", "halving arithmetic"),
    (r"asyncio\.gather|Promise\.all", "concurrency", "concurrent fan-out"),
)

_RISK_COMPILED = tuple((re.compile(pattern, re.M), tag, note) for pattern, tag, note in RISK_PATTERNS)


def scan_risks(source: str) -> list[dict]:
    signals: list[dict] = []
    for pattern, tag, note in _RISK_COMPILED:
        for match in pattern.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            signals.append({"tag": tag, "note": note, "lineno": lineno, "text": match.group(0).strip()[:120]})
    return signals


# --------------------------------------------------------------------------
# Python adapter (exact, via ast)
# --------------------------------------------------------------------------


class PythonAdapter:
    language = "python"

    def extract(self, path: str, source: str) -> FileFacts:
        lines = source.splitlines()
        facts = FileFacts(
            path=path,
            language="python",
            lines=len(lines),
            size=len(source.encode("utf-8", "replace")),
            sha="",
            risk_signals=scan_risks(source),
        )
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            facts.risk_signals.append(
                {"tag": "syntax-error", "note": f"syntax error: {exc.msg}", "lineno": exc.lineno or 0, "text": ""}
            )
            return facts

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    facts.imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = "." * node.level + module
                facts.imports.append(module)
                for alias in node.names:
                    facts.imported_names.append(alias.name)

        def visit_body(body: Iterable[ast.stmt], parent: str | None, class_name: str | None) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = node.name
                    full = f"{parent}.{name}" if parent else name
                    calls = sorted(
                        {
                            self._call_name(call.func)
                            for call in ast.walk(node)
                            if isinstance(call, ast.Call) and self._call_name(call.func)
                        }
                    )
                    args = node.args
                    params = [a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)]
                    defaults = [ast.unparse(d)[:80] for d in list(args.defaults) + [d for d in args.kw_defaults if d]]
                    facts.symbols.append(
                        Symbol(
                            id=f"{path}::{full}",
                            name=name,
                            kind="method" if class_name else "function",
                            path=path,
                            lineno=node.lineno,
                            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                            signature=self._signature(node),
                            doc=(ast.get_docstring(node) or "").split("\n")[0][:200],
                            decorators=[self._decorator(d) for d in node.decorator_list],
                            parent=parent,
                            calls=calls,
                            is_test=name.startswith("test_") or is_test_path(path),
                            is_async=isinstance(node, ast.AsyncFunctionDef),
                            params=params,
                            defaults=defaults,
                        )
                    )
                    visit_body(node.body, full, class_name)
                elif isinstance(node, ast.ClassDef):
                    full = f"{parent}.{node.name}" if parent else node.name
                    facts.symbols.append(
                        Symbol(
                            id=f"{path}::{full}",
                            name=node.name,
                            kind="class",
                            path=path,
                            lineno=node.lineno,
                            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                            signature=f"class {node.name}",
                            doc=(ast.get_docstring(node) or "").split("\n")[0][:200],
                            decorators=[self._decorator(d) for d in node.decorator_list],
                            parent=parent,
                            calls=[],
                        )
                    )
                    visit_body(node.body, full, node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent is None:
                    for target in self._assign_targets(node):
                        facts.symbols.append(
                            Symbol(
                                id=f"{path}::{target}",
                                name=target,
                                kind="variable",
                                path=path,
                                lineno=node.lineno,
                                end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
                                signature=f"{target} = ...",
                            )
                        )

        visit_body(tree.body, None, None)
        facts.has_main = bool(re.search(r"if\s+__name__\s*==\s*[\"']__main__[\"']", source))
        return facts

    @staticmethod
    def _assign_targets(node: ast.stmt) -> list[str]:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if target is None and isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        return [target.id] if isinstance(target, ast.Name) else []

    @staticmethod
    def _call_name(func: ast.expr) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            inner = PythonAdapter._call_name(func.value)
            return f"{inner}.{func.attr}" if inner else func.attr
        return ""

    @staticmethod
    def _decorator(node: ast.expr) -> str:
        try:
            return ast.unparse(node)[:120]
        except Exception:  # pragma: no cover - unparse is stable on 3.9+
            return ""

    @staticmethod
    def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        try:
            args = ast.unparse(node.args)[:300]
        except Exception:  # pragma: no cover
            args = "..."  # type: ignore[assignment]
        ret = ""
        if node.returns is not None:
            try:
                ret = f" -> {ast.unparse(node.returns)[:80]}"
            except Exception:  # pragma: no cover
                ret = ""
        return f"{prefix} {node.name}({args}){ret}"


# --------------------------------------------------------------------------
# Regex adapters for other languages
# --------------------------------------------------------------------------

_JS_FUNCTION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", re.M
)
_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>", re.M
)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?", re.M)
_JS_METHOD = re.compile(
    r"^\s*(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{", re.M
)
_JS_IMPORT = re.compile(r"^\s*import\s+(?:([\w${},\s*]+)\s+from\s+)?[\"']([^\"']+)[\"']", re.M)
_JS_REQUIRE = re.compile(r"require\(\s*[\"']([^\"']+)[\"']\s*\)")
_JS_EXPORT = re.compile(r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|class|async function)\s+([A-Za-z_$][\w$]*)", re.M)

_GO_FUNC = re.compile(r"^func\s+(?:\(([^)]*)\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)\)", re.M)
_GO_IMPORT = re.compile(r"^\s*(?:import\s+)?[\"']([^\"']+)[\"']", re.M)

_JAVA_TYPE = re.compile(r"^\s*(?:public|private|protected|abstract|final|static|\s)*\s*(class|interface|enum|record)\s+(\w+)", re.M)
_JAVA_METHOD = re.compile(
    r"^\s*(?:public|private|protected|static|final|synchronized|abstract|\s)*\s*(?:[\w<>\[\],.?]+\s+)?(\w+)\s*\(([^)]*)\)\s*(?:throws [\w,\s.]+)?\{", re.M
)
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+);", re.M)

_GENERIC_FUNC = re.compile(r"^\s*(?:def|func|fn|sub)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", re.M)


class RegexAdapter:
    """Best-effort structural extraction for non-Python languages."""

    def __init__(self, language: str) -> None:
        self.language = language

    def extract(self, path: str, source: str) -> FileFacts:
        lines = source.splitlines()
        facts = FileFacts(
            path=path,
            language=self.language,
            lines=len(lines),
            size=len(source.encode("utf-8", "replace")),
            sha="",
            risk_signals=scan_risks(source),
        )
        if self.language in {"javascript", "typescript"}:
            self._js(path, source, facts)
        elif self.language == "go":
            self._go(path, source, facts)
        elif self.language in {"java", "kotlin", "csharp"}:
            self._java(path, source, facts)
        else:
            self._generic(path, source, facts)
        facts.has_main = bool(re.search(r"__main__|func\s+main\s*\(", source))
        return facts

    @staticmethod
    def _end_lineno(source: str, start: int) -> int:
        """Brace/indent matching for the block starting at ``start`` (1-based)."""
        lines = source.splitlines()
        if start > len(lines):
            return start
        depth = 0
        seen_open = False
        for offset, line in enumerate(lines[start - 1 : start + 400], start=start):
            depth += line.count("{") - line.count("}")
            if "{" in line:
                seen_open = True
            if seen_open and depth <= 0:
                return offset
        return min(len(lines), start)

    def _add(self, facts: FileFacts, path: str, name: str, kind: str, lineno: int, end: int, signature: str, params: str = "") -> None:
        facts.symbols.append(
            Symbol(
                id=f"{path}::{name}",
                name=name,
                kind=kind,
                path=path,
                lineno=lineno,
                end_lineno=max(end, lineno),
                signature=signature[:300],
                params=[p.strip().split(":")[0].strip() for p in params.split(",") if p.strip()],
                is_test=name.startswith("test") or is_test_path(path),
            )
        )

    def _js(self, path: str, source: str, facts: FileFacts) -> None:
        facts.imports = [m.group(2) for m in _JS_IMPORT.finditer(source)] + _JS_REQUIRE.findall(source)
        for match in _JS_IMPORT.finditer(source):
            if match.group(1):
                facts.imported_names.extend(
                    [piece.strip().split(" as ")[-1].strip() for piece in match.group(1).split(",") if piece.strip()]
                )
        for match in _JS_CLASS.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, match.group(1), "class", lineno, self._end_lineno(source, lineno), f"class {match.group(1)}")
        for match in _JS_FUNCTION.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, match.group(1), "function", lineno, self._end_lineno(source, lineno), match.group(0).strip(), match.group(2))
        for match in _JS_ARROW.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            params = match.group(2) or match.group(3) or ""
            self._add(facts, path, match.group(1), "function", lineno, self._end_lineno(source, lineno), match.group(0).strip(), params)
        for match in _JS_METHOD.finditer(source):
            token = match.group(1)
            if token in {"if", "for", "while", "switch", "catch", "function", "return", "else", "do", "try"}:
                continue
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, token, "method", lineno, self._end_lineno(source, lineno), match.group(0).strip(), match.group(2))

    def _go(self, path: str, source: str, facts: FileFacts) -> None:
        facts.imports = _GO_IMPORT.findall(source)
        for match in _GO_FUNC.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            receiver = match.group(1) or ""
            self._add(facts, path, match.group(2), "method" if receiver else "function", lineno, self._end_lineno(source, lineno), match.group(0).strip(), match.group(3))

    def _java(self, path: str, source: str, facts: FileFacts) -> None:
        facts.imports = _JAVA_IMPORT.findall(source)
        for match in _JAVA_TYPE.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, match.group(2), "class", lineno, self._end_lineno(source, lineno), match.group(0).strip())
        for match in _JAVA_METHOD.finditer(source):
            token = match.group(1)
            if token in {"if", "for", "while", "switch", "catch", "return", "new", "try"}:
                continue
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, token, "method", lineno, self._end_lineno(source, lineno), match.group(0).strip(), match.group(2))

    def _generic(self, path: str, source: str, facts: FileFacts) -> None:
        for match in _GENERIC_FUNC.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            self._add(facts, path, match.group(1), "function", lineno, self._end_lineno(source, lineno), match.group(0).strip(), match.group(2))


def adapter_for(language: str) -> PythonAdapter | RegexAdapter:
    if language == "python":
        return PythonAdapter()
    return RegexAdapter(language)
