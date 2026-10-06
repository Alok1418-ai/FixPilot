"""Deterministic fix strategies.

This is FixPilot's offline brain: a library of small, *provably grounded*
transforms.  Each one takes the exact source text around the failing line, makes
a minimal, explainable edit, and refuses to emit anything that does not parse.
Multiple candidates can be produced for the same bug (for example "guard with an
explicit error" versus "degrade to a neutral value") — the verifier then runs the
tests and keeps whichever one actually holds.

A strategy that cannot ground itself in real source text produces **no**
candidate.  Producing no patch is a valid, honest outcome; inventing one is not.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..repo.githistory import GitHistory
from ..repo.indexer import RepoIndex
from ..util import excerpt

NUMBER_HINT = re.compile(r"\b(int|float|decimal|total|count|amount|price|size|len|sum|score|balance|qty|quantity|num)\b", re.I)
STRING_HINT = re.compile(r"\b(str|name|label|title|message|path|text|slug|id)\b", re.I)
LIST_HINT = re.compile(r"\b(list|items|rows|values|entries|tags|results|collection)\b", re.I)
DICT_HINT = re.compile(r"\b(dict|map|config|payload|metadata|options|settings|data)\b", re.I)


@dataclass(slots=True)
class FixCandidate:
    strategy: str
    path: str
    old_content: str
    new_content: str
    reason: str
    confidence: float = 0.5
    lineno: int = 0
    symbol: str = ""
    behaviour_change: bool = False
    requires_review: bool = True
    verification_hint: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.old_content != self.new_content

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "path": self.path,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "lineno": self.lineno,
            "symbol": self.symbol,
            "behaviour_change": self.behaviour_change,
            "verification_hint": self.verification_hint,
            "notes": self.notes,
            "changed": self.changed,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _line_index(source: str, lineno: int) -> int:
    return max(0, min(len(source.splitlines()) - 1, lineno - 1))


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


USAGE_DEFAULT_RULES: tuple[tuple[str, str, str], ...] = (
    # (regex over "{name}", default, why) — first match wins, ordered by certainty
    (r"\b{name}\s*\[\s*['\"]", "{}", "the value is subscripted with a string key, so a mapping default is safe"),
    (r"\b{name}\s*\.\s*(?:get|items|keys|values|setdefault|update)\s*\(", "{}", "the value is used as a mapping"),
    (r"\b{name}\s*\[\s*\d", "[]", "the value is subscripted with an index, so a sequence default is safe"),
    (r"\b{name}\s*\.\s*(?:append|extend|insert|remove|index|count|sort)\s*\(", "[]", "the value is used as a list"),
    (r"\blen\s*\(\s*{name}\s*\)|for\s+\w+\s*(?:,\s*\w+\s*)?in\s+{name}\b", "[]", "the value is measured or iterated as a collection"),
    (r"{name}\s*[-+*/%]|[-+*/%]\s*{name}\b|float\s*\(\s*{name}|int\s*\(\s*{name}|round\s*\(\s*{name}|sum\s*\(\s*{name}", "0", "the value takes part in arithmetic"),
    (r"\b{name}\s*\.\s*(?:strip|lower|upper|split|replace|startswith|endswith|join)\s*\(", '""', "the value is used as text"),
    (r"\b{name}\s*\+|(?:\+\s*{name})", '""', "the value is concatenated"),
)


def _infer_default(usage_text: str, name: str = "") -> tuple[str, str]:
    """Guess a neutral default from how the value is *used* afterwards.

    Returns ``("None", ...)`` when nothing certain can be inferred, which callers
    treat as "do not invent a change".
    """
    if name:
        for pattern, default, why in USAGE_DEFAULT_RULES:
            if re.search(pattern.format(name=re.escape(name)), usage_text):
                return default, why
    # Fall back to type-shaped naming hints.
    if NUMBER_HINT.search(usage_text):
        return "0", "numeric context"
    if LIST_HINT.search(usage_text):
        return "[]", "collection context"
    if DICT_HINT.search(usage_text):
        return "{}", "mapping context"
    if STRING_HINT.search(usage_text):
        return '""', "text context"
    return "None", "no type hint available"


def _usage_after(lines: list[str], start: int, name: str, window: int = 8) -> str:
    segment = "\n".join(lines[start : start + window])
    return segment


def _parses(source: str, language: str) -> bool:
    if language != "python":
        return True
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def _candidate(
    *,
    strategy: str,
    path: str,
    old: str,
    new: str,
    reason: str,
    confidence: float,
    lineno: int,
    symbol: str = "",
    behaviour_change: bool = False,
    verification_hint: str = "",
    notes: Iterable[str] = (),
    language: str = "python",
) -> list[FixCandidate]:
    if old == new or not _parses(new, language):
        return []
    return [
        FixCandidate(
            strategy=strategy,
            path=path,
            old_content=old,
            new_content=new,
            reason=reason,
            confidence=confidence,
            lineno=lineno,
            symbol=symbol,
            behaviour_change=behaviour_change,
            verification_hint=verification_hint,
            notes=list(notes),
        )
    ]


def _window(source: str, lineno: int, before: int = 0, after: int = 0) -> tuple[list[str], int, int]:
    """Return ``(window_lines, absolute_start, absolute_end)``.

    ``window_lines[offset]`` corresponds to absolute line ``start + offset`` (0-based),
    which is what :func:`_replace_lines` expects.
    """
    lines = source.split("\n")
    idx = max(0, min(len(lines) - 1, lineno - 1))
    start = max(0, idx - before)
    end = min(len(lines), idx + after + 1)
    return lines[start:end], start, end


def _order(window_len: int, start: int, lineno: int):
    """Window offsets ordered by distance from the reported line.

    Tracebacks routinely point a line or two away from the real defect (a
    statement wrapped over several lines, or a CI checkout at a slightly older
    revision).  Scanning outward keeps the fix anchored to the *reported* line
    while still tolerating that drift.
    """
    anchor = max(0, min(window_len - 1, (lineno - 1) - start))
    ordered: list[int] = []
    for delta in range(0, window_len):
        for candidate in (anchor + delta, anchor - delta):
            if 0 <= candidate < window_len and candidate not in ordered:
                ordered.append(candidate)
    return ordered


def _nearest(window_len: int, start: int, lineno: int) -> int:
    return _order(window_len, start, lineno)[0]


def _replace_lines(source: str, start: int, end: int, replacement: list[str]) -> str:
    lines = source.split("\n")
    return "\n".join(lines[:start] + replacement + lines[end:])


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

Transform = Callable[[str, int, str, str, RepoIndex, GitHistory], list[FixCandidate]]


def _strategy_dict_get_default(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """`.get(key)` used as if the key always exists -> supply the neutral default."""
    lines, start, end = _window(source, lineno, before=4, after=6)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        line = lines[offset]
        if ".get(" not in line:
            continue
        match = re.search(r"\.get\(\s*([^,()]+?)\s*\)", line)
        if not match:
            continue
        key = match.group(1).strip()
        target_match = re.match(r"\s*([A-Za-z_]\w*)\s*=", line)
        target = target_match.group(1) if target_match else ""
        usage = _usage_after(lines, offset + 1, target)
        default, why = _infer_default(usage + "\n" + line, target or symbol)
        if default == "None":
            continue
        new_line = line[: match.start()] + f".get({key}, {default})" + line[match.end() :]
        replaced = _replace_lines(source, start + offset, start + offset + 1, [new_line])
        candidates.extend(
            _candidate(
                strategy="safe-key-access",
                path=path,
                old=source,
                new=replaced,
                reason=(
                    f"`{excerpt(line.strip(), 90)}` reads `{key}` as if it is always present. Using the {why} "
                    f"default `{default}` keeps the failing path alive instead of raising."
                ),
                confidence=0.62,
                lineno=start + offset + 1,
                symbol=symbol,
                behaviour_change=True,
                verification_hint="call the failing path with the key missing and assert the new behaviour",
            )
        )
        break
    return candidates


def _strategy_none_guard(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Attribute access on a value that can be None -> explicit guard before the line."""
    lines, start, end = _window(source, lineno, before=2, after=2)
    if not lines:
        return []
    found = None
    for offset in _order(len(lines), start, lineno):
        candidate_line = lines[offset]
        indent = _indent(candidate_line)
        if not candidate_line.strip() or candidate_line.strip().startswith(
            ("#", "return", "raise", "if", "for", "while", "with", "try")
        ):
            continue
        match = re.match(r"(?P<lead>\s*(?:[\w\[\]'\".]+(?:\s*=\s*)?)?)(?P<expr>[\w]+)(?P<attr>\.\w+)", candidate_line)
        expr = match.group("expr") if match else ""
        if not expr or expr in {"self", "cls"}:
            continue
        found = (offset, candidate_line, indent, expr)
        break
    if found is None:
        return []
    target, line, indent, expr = found
    guard = [
        f"{indent}if {expr} is None:  # FixPilot: guard added after a NoneType failure",
        f'{indent}    raise ValueError("{symbol or "value"} was expected but got None")',
    ]
    absolute = start + target
    replaced = _replace_lines(source, absolute, absolute + 1, guard + [line])
    return _candidate(
        strategy="guard-none-attribute",
        path=path,
        old=source,
        new=replaced,
        reason=(
            f"`{expr}` is dereferenced on line {start + 1} but can be None. Failing loudly with context "
            f"turns an opaque AttributeError into an actionable error."
        ),
        confidence=0.55,
        lineno=start + 1,
        symbol=symbol,
        behaviour_change=False,
        verification_hint="assert the guarded error message is raised when the value is missing",
        notes=["this keeps a failure rather than hiding it — choose a neutral default instead if the caller can tolerate absence"],
    )


def _strategy_subscript_default(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """``D[key]`` that can miss -> ``D.get(key, <neutral default>)``.

    Grounded by how the retrieved value is used: a value that is later subscripted
    with a string key, or used as a mapping, gets a ``{}`` default; arithmetic gets
    ``0``; sequence usage gets ``[]``; and if nothing certain can be inferred, no
    candidate is produced at all.
    """
    lines, start, end = _window(source, lineno, before=4, after=6)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        line = lines[offset]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "import ", "from ")):
            continue
        if re.match(r"^\s*[\w\.]+\s*\[[^\]]+\]\s*=[^=]", line):
            continue  # writes into a mapping, not a lookup that can miss
        # A lookup that can miss: a bare identifier subscripted by a literal or a variable.
        match = re.search(r"(?<![\w\.])([A-Za-z_]\w*)\[\s*([A-Za-z_]\w*|'[^']+'|\"[^\"]+\")\s*\]", line)
        if not match:
            continue
        mapping, key = match.group(1), match.group(2)
        if mapping in {"self", "cls", "os", "sys", "re", "json"}:
            continue
        target_match = re.match(r"\s*([A-Za-z_]\w*)\s*=", line)
        target = target_match.group(1) if target_match else ""
        usage = _usage_after(lines, offset + 1, target) if target else ""
        default, why = _infer_default(usage + "\n" + line, target)
        if default == "None":
            continue
        new_line = line[: match.start()] + f"{mapping}.get({key}, {default})" + line[match.end() :]
        candidates.extend(
            _candidate(
                strategy="safe-key-access",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + offset + 1, [new_line]),
                reason=(
                    f"`{excerpt(stripped, 90)}` looks `{key}` up in `{mapping}` as if it is always present. "
                    f"Because {why}, falling back to `{default}` keeps the failing path alive."
                ),
                confidence=0.68,
                lineno=start + offset + 1,
                symbol=symbol,
                behaviour_change=True,
                verification_hint=f"call the failing path without `{key}` and assert the new fallback behaviour",
            )
        )
        break
    return candidates


def _strategy_zero_guard(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Division whose divisor can be zero -> explicit error, plus a neutral variant."""
    lines, start, end = _window(source, lineno, before=2, after=2)
    if not lines:
        return []
    operand = r"(?:[\w\.\[\]'\"]+\([^()]*\)|[\w\.\[\]'\"]+)"
    found = None
    for offset in _order(len(lines), start, lineno):
        candidate_line = lines[offset]
        candidate_match = re.search(rf"({operand})\s*/\s*({operand})", candidate_line)
        if candidate_match:
            found = (offset, candidate_line, candidate_match)
            break
    if found is None:
        return []
    target, line, match = found
    numerator, divisor = match.group(1), match.group(2)
    indent = _indent(line)
    guarded = [
        f"{indent}if not {divisor}:  # FixPilot: zero guard",
        f'{indent}    raise ValueError("{divisor} must not be zero (got " + repr({divisor}) + ")")',
        line,
    ]
    absolute = start + target
    candidates = _candidate(
        strategy="zero-guard",
        path=path,
        old=source,
        new=_replace_lines(source, absolute, absolute + 1, guarded),
        reason=f"`{divisor}` is unguarded in a division on line {absolute + 1}; a zero value raises ZeroDivisionError.",
        confidence=0.6,
        lineno=absolute + 1,
        symbol=symbol,
        behaviour_change=False,
        verification_hint="call with a zero divisor and assert the explicit error",
    )
    if re.fullmatch(r"(?:len\s*\(\s*[A-Za-z_][\w\.]*\s*\)|[A-Za-z_][\w\.]*)", divisor):
        neutral_line = line.replace(match.group(0), f"({match.group(0)} if {divisor} else 0)")
        candidates.extend(
            _candidate(
                strategy="zero-guard-neutral",
                path=path,
                old=source,
                new=_replace_lines(source, absolute, absolute + 1, [neutral_line]),
                reason=f"Alternate candidate: treat a zero `{divisor}` as a zero result instead of failing.",
                confidence=0.45,
                lineno=absolute + 1,
                symbol=symbol,
                behaviour_change=True,
                verification_hint="assert the zero-divisor case returns 0 (behaviour change — review before shipping)",
            )
        )
    return candidates


def _strategy_bounds_check(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Index access -> bounds-checked access."""
    lines, start, end = _window(source, lineno, before=2, after=2)
    if not lines:
        return []
    found = None
    for offset in _order(len(lines), start, lineno):
        candidate_line = lines[offset]
        candidate_match = re.search(r"([\w\.]+)\[\s*([\w\.\-+ ]+?)\s*\](?!\s*=)", candidate_line)
        if candidate_match:
            found = (offset, candidate_line, candidate_match)
            break
    if found is None:
        return []
    target, line, match = found
    sequence, index_expr = match.group(1), match.group(2)
    default, _ = _infer_default(symbol + " " + line)
    replacement = f"({sequence}[{index_expr}] if -len({sequence}) <= {index_expr} < len({sequence}) else {default})"
    new_line = line[: match.start()] + replacement + line[match.end() :]
    absolute = start + target
    return _candidate(
        strategy="bounds-check",
        path=path,
        old=source,
        new=_replace_lines(source, absolute, absolute + 1, [new_line]),
        reason=f"`{sequence}[{index_expr}]` uses an index derived from data on line {absolute + 1}; out-of-range input raises IndexError.",
        confidence=0.5,
        lineno=absolute + 1,
        symbol=symbol,
        behaviour_change=True,
        verification_hint="call with an empty and an oversized index and assert the fallback",
    )


def _strategy_validate_input(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """int()/float()/json parse without guarding empty or malformed input."""
    lines, start, end = _window(source, lineno, before=7, after=2)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        line = lines[offset]
        for cast in ("int", "float"):
            match = re.search(rf"(?<![\w.]){cast}\(\s*([\w\.\[\]'\"]+)\s*\)", line)
            if not match:
                continue
            value = match.group(1)
            guard_indent = _indent(line)
            new_line = line[: match.start()] + f"{cast}({value} or 0)" + line[match.end() :]
            candidates.extend(
                _candidate(
                    strategy="validate-input",
                    path=path,
                    old=source,
                    new=_replace_lines(source, start + offset, start + offset + 1, [new_line]),
                    reason=(
                        f"`{cast}({value})` on line {start + offset + 1} raises ValueError for empty or non-numeric input "
                        f"(the common case in report: {excerpt(reason, 70)}). Defaulting falsy input to 0 keeps the flow intact."
                    ),
                    confidence=0.58,
                    lineno=start + offset + 1,
                    symbol=symbol,
                    behaviour_change=True,
                    verification_hint="feed an empty string and assert the default is used",
                )
            )
            break
        if candidates:
            break
    return candidates


def _strategy_narrow_exception(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """bare `except:` -> narrow clause, with a re-raise variant."""
    lines, start, end = _window(source, lineno, before=6, after=4)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        stripped = lines[offset].strip()
        if not re.match(r"except\s*:", stripped):
            continue
        indent = _indent(lines[offset])
        narrowed = [f"{indent}except Exception:"]
        body_is_pass = offset + 1 < len(lines) and lines[offset + 1].strip() == "pass"
        candidates.extend(
            _candidate(
                strategy="narrow-exception",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + offset + 1, narrowed),
                reason=(
                    f"`except:` on line {start + offset + 1} swallows every error, including KeyboardInterrupt and SystemExit. "
                    "Narrowing it keeps the original failure visible."
                ),
                confidence=0.66,
                lineno=start + offset + 1,
                symbol=symbol,
                behaviour_change=False,
                verification_hint="run the surrounding tests; the original error should now surface if it was being hidden",
            )
        )
        if body_is_pass:
            rethrow = [
                f"{indent}except Exception:  # FixPilot: was a silent bare except",
                f"{indent}    raise",
            ]
            candidates.extend(
                _candidate(
                    strategy="log-and-reraise",
                    path=path,
                    old=source,
                    new=_replace_lines(source, start + offset, start + offset + 2, rethrow),
                    reason=(
                        "The bare except body is just `pass`, so a real failure disappears here. Re-raising surfaces it "
                        "at the true source instead of downstream."
                    ),
                    confidence=0.55,
                    lineno=start + offset + 1,
                    symbol=symbol,
                    behaviour_change=True,
                    verification_hint="assert the previously hidden error is now raised",
                )
            )
        break
    return candidates


def _strategy_mutable_default(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Mutable default argument -> None sentinel."""
    lines, start, end = _window(source, lineno, before=40, after=4)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        match = re.search(r"def\s+(\w+)\s*\((.*)\)\s*(?:->.*)?:", lines[offset])
        if not match:
            continue
        signature = match.group(2)
        mutable = re.findall(r"(\w+)\s*=\s*(\[\]|\{\})", signature)
        if not mutable:
            continue
        new_signature = re.sub(r"=\s*(\[\]|\{\})", "=None", signature)
        new_line = lines[offset][: match.start(2)] + new_signature + lines[offset][match.end(2) :]
        body_indent = _indent(lines[offset]) + "    "
        initialisers = []
        for param_name, kind in mutable:
            initialisers.append(f"{body_indent}if {param_name} is None:")
            initialisers.append(f"{body_indent}    {param_name} = {'[]' if kind == '[]' else '{}'}")
        if not initialisers:
            continue
        replacement = [new_line, *initialisers]
        candidates.extend(
            _candidate(
                strategy="none-default-sentinel",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + offset + 1, replacement),
                reason=(
                    f"`{match.group(1)}` uses a mutable default argument, which is created once at import time and shared by every call. "
                    "The None sentinel creates a fresh container per call."
                ),
                confidence=0.75,
                lineno=start + offset + 1,
                symbol=match.group(1),
                behaviour_change=False,
                verification_hint="call the function twice and assert state does not leak between calls",
            )
        )
        break
    return candidates


def _strategy_explicit_encoding(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Text I/O without an explicit encoding."""
    lines, start, end = _window(source, lineno, before=6, after=3)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        line = lines[offset]
        if "encoding=" in line:
            continue
        new_line = line
        if re.search(r"\bopen\((?![^)]*encoding=)", line):
            new_line = re.sub(r"\bopen\(([^)]*?)\)", r"open(\1, encoding=\"utf-8\")", line, count=1)
        elif re.search(r"\.read_text\(\s*\)", line):
            new_line = re.sub(r"\.read_text\(\s*\)", '.read_text(encoding="utf-8")', line, count=1)
        elif re.search(r"\.write_text\(([^)]*?)\)", line) and "encoding=" not in line:
            new_line = re.sub(r"\.write_text\(([^)]*?)\)", r'.write_text(\1, encoding="utf-8")', line, count=1)
        if new_line == line:
            continue
        candidates.extend(
            _candidate(
                strategy="explicit-encoding",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + offset + 1, [new_line]),
                reason=(
                    f"Line {start + offset + 1} reads/writes text using the platform default encoding, which differs between "
                    "a developer laptop and CI. Pinning UTF-8 (or a declared codec) removes the ambiguity."
                ),
                confidence=0.7,
                lineno=start + offset + 1,
                symbol=symbol,
                verification_hint="read a non-ASCII fixture and assert it decodes correctly",
            )
        )
        break
    return candidates


def _strategy_iterate_directly(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """range(len(...)) indexing -> direct iteration."""
    lines, start, end = _window(source, lineno, before=6, after=10)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        match = re.match(r"(\s*)for\s+(\w+)\s+in\s+range\(\s*len\(\s*([\w\.]+)\s*\)\s*\)\s*:", lines[offset])
        if not match:
            continue
        indent, var, sequence = match.group(1), match.group(2), match.group(3)
        body_end = offset + 1
        while body_end < len(lines) and (lines[body_end].startswith(indent + " ") or not lines[body_end].strip()):
            body_end += 1
        body = lines[offset + 1 : body_end]
        new_body = [line.replace(f"{sequence}[{var}]", var) for line in body]
        new_loop = [f"{indent}for {var} in {sequence}:"]
        block = new_loop + new_body
        candidates.extend(
            _candidate(
                strategy="iterate-directly",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + body_end, block),
                reason=(
                    f"`for {var} in range(len({sequence}))` on line {start + offset + 1} iterates by index, which is where the "
                    "boundary error lives. Direct iteration removes the index arithmetic entirely."
                ),
                confidence=0.55,
                lineno=start + offset + 1,
                symbol=symbol,
                behaviour_change=False,
                verification_hint="run the loop over an empty and a single-element collection",
            )
        )
        break
    return candidates


def _strategy_dependency_guard(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Optional import -> guarded import with an actionable message."""
    lines, start, end = _window(source, lineno, before=25, after=6)
    candidates: list[FixCandidate] = []
    for offset in _order(len(lines), start, lineno):
        line = lines[offset]
        match = re.match(r"(\s*)(?:from\s+([\w\.]+)\s+import\s+([\w,\s]+)|import\s+([\w\.]+))", line)
        if not match:
            continue
        module = match.group(2) or match.group(4)
        indent = match.group(1)
        imported = (match.group(3) or module).split(",")[0].strip()
        if module.startswith(".") or not module:
            continue
        guarded = [
            f"{indent}try:  # FixPilot: dependency made explicit",
            f"{indent}    {line.strip()}",
            f"{indent}except ImportError as exc:  # pragma: no cover - environment guard",
            f'{indent}    raise ImportError(',
            f'{indent}        "FixPilot: {module} is required for this module but is not installed. "',
            f'{indent}        "Add it to your dependency manifest (see requirements/pyproject) and reinstall."',
            f"{indent}    ) from exc",
        ]
        candidates.extend(
            _candidate(
                strategy="declared-dependency-guard",
                path=path,
                old=source,
                new=_replace_lines(source, start + offset, start + offset + 1, guarded),
                reason=(
                    f"`{module}` is imported on line {start + offset + 1} but was missing at runtime. Failing with an actionable "
                    "message makes the missing dependency obvious instead of a bare ModuleNotFoundError."
                ),
                confidence=0.45,
                lineno=start + offset + 1,
                symbol=symbol,
                behaviour_change=False,
                verification_hint="remove the package from the environment and assert the actionable ImportError",
                notes=["the real fix may be declaring the dependency rather than guarding the import"],
            )
        )
        break
    return candidates


def _strategy_restore_intent(path: str, source: str, lineno: int, symbol: str, reason: str, index: RepoIndex, history: GitHistory) -> list[FixCandidate]:
    """Use git history: propose restoring the pre-change version of the failing line."""
    if not history.enabled:
        return []
    blame = history.blame_line(path, lineno)
    if blame is None:
        return []
    previous = history.run(["show", f"{blame.sha}^:{path}"])
    if not previous.strip():
        return []
    old_lines = previous.split("\n")
    current_lines = source.split("\n")
    if lineno - 1 >= len(current_lines) or lineno - 1 >= len(old_lines):
        return []
    candidates: list[FixCandidate] = []
    for candidate_line in (lineno, lineno - 1, lineno + 1):
        idx = candidate_line - 1
        if idx < 0 or idx >= len(old_lines) or idx >= len(current_lines):
            continue
        if old_lines[idx] == current_lines[idx] or not old_lines[idx].strip():
            continue
        new_lines = list(current_lines)
        new_lines[idx] = old_lines[idx]
        candidates.extend(
            _candidate(
                strategy="restore-intent",
                path=path,
                old=source,
                new="\n".join(new_lines),
                reason=(
                    f"Line {candidate_line} was changed in {blame.sha[:8]} (“{excerpt(blame.summary, 70)}”). The previous "
                    f"revision had `{excerpt(old_lines[idx].strip(), 80)}`. Restoring that line is the smallest change that "
                    "returns the code to its last known-good intent."
                ),
                confidence=0.5,
                lineno=candidate_line,
                symbol=symbol,
                behaviour_change=True,
                verification_hint="re-run the test that covers this line; if it passes, the regression is reverted",
                notes=["review whether the original change was deliberate before keeping this"],
            )
        )
        break
    return candidates


#: Strategy name -> one or more transforms (several shapes of the same idea).
STRATEGIES: dict[str, tuple[Transform, ...]] = {
    "safe-key-access": (_strategy_subscript_default, _strategy_dict_get_default),
    "guard-none-attribute": (_strategy_none_guard,),
    "zero-guard": (_strategy_zero_guard,),
    "zero-guard-neutral": (_strategy_zero_guard,),
    "bounds-check": (_strategy_bounds_check,),
    "validate-input": (_strategy_validate_input,),
    "narrow-exception": (_strategy_narrow_exception,),
    "log-and-reraise": (_strategy_narrow_exception,),
    "none-default-sentinel": (_strategy_mutable_default,),
    "explicit-encoding": (_strategy_explicit_encoding,),
    "iterate-directly": (_strategy_iterate_directly,),
    "declared-dependency-guard": (_strategy_dependency_guard,),
    "restore-intent": (_strategy_restore_intent,),
}

#: Strategies whose entire purpose is to fail loudly rather than continue.
FAIL_FAST_STRATEGIES = {"guard-none-attribute", "zero-guard", "narrow-exception"}

STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "safe-key-access": "supply the neutral default for a lookup that assumed the key was present",
    "guard-none-attribute": "fail with an actionable error instead of an opaque AttributeError",
    "zero-guard": "guard a divisor that can be zero",
    "zero-guard-neutral": "treat a zero divisor as a zero result",
    "bounds-check": "bounds-check an index derived from data",
    "validate-input": "default unparseable input instead of raising ValueError",
    "narrow-exception": "narrow a bare except so real failures stay visible",
    "log-and-reraise": "stop swallowing a caught exception silently",
    "none-default-sentinel": "replace a mutable default argument with a per-call container",
    "explicit-encoding": "pin the text encoding instead of relying on the platform default",
    "iterate-directly": "remove index arithmetic by iterating the collection directly",
    "declared-dependency-guard": "turn a missing dependency into an actionable error",
    "restore-intent": "restore the last known-good version of the failing line",
    "stabilise-timing": "replace fixed sleeps with condition polling",
    "manual-review": "no safe automated transform was found",
}


def generate_candidates(
    *,
    strategy: str,
    path: str,
    lineno: int,
    symbol: str,
    reason: str,
    index: RepoIndex,
    history: GitHistory,
    extra_strategies: Iterable[str] = (),
) -> list[FixCandidate]:
    """Run one or more strategies against a file and return grounded candidates."""
    facts = index.files.get(path)
    if facts is None:
        return []
    source = "\n".join(index.source_lines(path))
    if not source.strip():
        return []
    tried = [strategy, *extra_strategies] if strategy else list(extra_strategies)
    candidates: list[FixCandidate] = []
    for name in dict.fromkeys(tried):
        transforms = STRATEGIES.get(name)
        if not transforms:
            continue
        for transform in transforms:
            try:
                candidates.extend(transform(path, source, lineno, symbol, reason, index, history))
            except Exception as exc:  # pragma: no cover - a transform must never break the run
                candidates.append(
                    FixCandidate(
                        strategy=name,
                        path=path,
                        old_content=source,
                        new_content=source,
                        reason=f"strategy {name} failed safely: {type(exc).__name__}: {exc}",
                        confidence=0.0,
                        lineno=lineno,
                        symbol=symbol,
                    )
                )
    # De-duplicate: two strategies can legitimately describe the same edit.
    unique: dict[tuple[str, str], FixCandidate] = {}
    for candidate in candidates:
        if not candidate.changed:
            continue
        key = (candidate.path, candidate.new_content)
        existing = unique.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda c: -c.confidence)


def strategies_for_category(category: str) -> list[str]:
    """Category-level fallbacks when the top hypothesis names no strategy."""
    mapping = {
        "null-safety": ["safe-key-access", "guard-none-attribute"],
        "boundary": ["bounds-check", "iterate-directly", "zero-guard"],
        "input-validation": ["validate-input"],
        "exception": ["narrow-exception", "log-and-reraise"],
        "state": ["none-default-sentinel"],
        "regression": ["restore-intent"],
        "dependency": ["declared-dependency-guard"],
        "resource": ["explicit-encoding"],
        "concurrency": [],
        "config": ["explicit-encoding"],
        "ui": [],
        "test": [],
        "logic": [],
        "unknown": [],
    }
    return mapping.get(category, [])


def describe_strategies() -> list[dict]:
    return [
        {
            "name": name,
            "description": STRATEGY_DESCRIPTIONS.get(name, ""),
            "fail_fast": name in FAIL_FAST_STRATEGIES,
            "shapes": len(STRATEGIES[name]),
        }
        for name in sorted(STRATEGIES)
    ]
