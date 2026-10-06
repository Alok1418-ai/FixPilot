"""Skills that localise a bug: tracebacks, log signatures, static smells, UI captures."""

from __future__ import annotations

import re

from .base import Evidence, Hypothesis, Skill, SkillContext, SkillResult
from ..models.media import LogSignal

# --------------------------------------------------------------------------
# Traceback parsing helpers (kept local so the skill stays self-contained)
# --------------------------------------------------------------------------

TRACEBACK_LINE = re.compile(r"^Traceback \(most recent call last\)")
PY_FRAME_LINE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
PY_ERROR_LINE = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit|Fault|Timeout|Failure))\b(?::\s*(?P<msg>.*))?$"
)

# --------------------------------------------------------------------------
# Exception taxonomy: error type -> likely cause + fix strategy
# --------------------------------------------------------------------------

EXCEPTION_PLAYBOOK: dict[str, dict[str, str]] = {
    "AttributeError": {
        "cause": "code dereferences an attribute on a value that is None (or the wrong type)",
        "category": "null-safety",
        "strategy": "guard-none-attribute",
        "explanation": "The traceback ends in an attribute access that returned None upstream — most often a lookup with no default, an unset config value, or an early return without a value.",
        "verification": "rerun the failing call with the reported input and assert the attribute access is guarded",
    },
    "TypeError": {
        "cause": "a value of the wrong type reaches this call (often None or str/int mix-ups)",
        "category": "input-validation",
        "strategy": "coerce-and-validate",
        "explanation": "TypeErrors at this frame usually mean the caller passed an unexpected shape: a None where a number was expected, or a string where a container was expected.",
        "verification": "call the function with the reported argument types and assert a clear error or correct coercion",
    },
    "KeyError": {
        "cause": "a dictionary lookup uses a key that is not present for this input",
        "category": "null-safety",
        "strategy": "safe-key-access",
        "explanation": "The failing key is absent rather than empty — either the producer of the dict skips the field for some inputs, or the key name drifted.",
        "verification": "replay the input that misses the key and assert the lookup degrades gracefully",
    },
    "IndexError": {
        "cause": "an index or slice reaches past the end of the sequence",
        "category": "boundary",
        "strategy": "bounds-check",
        "explanation": "Off-by-one on an empty or short sequence — common when the code assumes at least N elements.",
        "verification": "run the function with an empty and a single-element input",
    },
    "ZeroDivisionError": {
        "cause": "a divisor derived from data is zero for this input",
        "category": "boundary",
        "strategy": "zero-guard",
        "explanation": "The divisor comes from user or aggregate data and has no zero guard.",
        "verification": "call with a zero divisor and assert the documented behaviour",
    },
    "ValueError": {
        "cause": "a parse or validation step rejects the runtime value",
        "category": "input-validation",
        "strategy": "validate-input",
        "explanation": "Conversion helpers (int/float/json/date) are being handed a value they cannot parse, typically an empty string or a placeholder.",
        "verification": "feed the malformed value and assert the new guard handles it",
    },
    "FileNotFoundError": {
        "cause": "a path is computed from configuration and does not exist at runtime",
        "category": "config",
        "strategy": "path-existence-check",
        "explanation": "The path is usually relative to the working directory or an unset environment variable.",
        "verification": "run with the path missing and assert a clear, actionable error",
    },
    "PermissionError": {
        "cause": "the process lacks write/read rights on the target path",
        "category": "resource",
        "strategy": "permission-preflight",
        "explanation": "Usually an artifact directory owned by another user, or a container running as a non-root user.",
        "verification": "assert the failure is reported as an actionable configuration error",
    },
    "ImportError": {
        "cause": "an optional or version-mismatched dependency is imported",
        "category": "dependency",
        "strategy": "declared-dependency-guard",
        "explanation": "The import works locally but not in CI/production, which points at a dependency that is undeclared or pinned differently.",
        "verification": "confirm the dependency is declared in the manifest and import is optional-safe",
    },
    "ModuleNotFoundError": {
        "cause": "the module imported here is not installed (or the package layout changed)",
        "category": "dependency",
        "strategy": "declared-dependency-guard",
        "explanation": "Either a missing manifest entry or a stale lockfile; the fix belongs in the dependency declaration, not the import.",
        "verification": "verify the dependency appears in the manifest and the import path is correct",
    },
    "ConnectionError": {
        "cause": "an outbound call failed — service down, wrong host, or no network in the sandbox",
        "category": "resource",
        "strategy": "retry-with-backoff",
        "explanation": "Network calls need an explicit failure mode; without one, a transient blip becomes a crash.",
        "verification": "simulate a refused connection and assert the retry/error path",
    },
    "TimeoutError": {
        "cause": "an operation exceeds its deadline (usually a network or lock wait)",
        "category": "resource",
        "strategy": "timeout-and-retry",
        "explanation": "The timeout is either too aggressive or not retried; both are common in CI.",
        "verification": "run with an artificially slow dependency and assert the timeout path",
    },
    "JSONDecodeError": {
        "cause": "a payload expected to be JSON is empty or truncated",
        "category": "input-validation",
        "strategy": "validate-payload",
        "explanation": "Typical sources: an empty response body, an HTML error page, or a partially written file.",
        "verification": "feed an empty and a non-JSON payload and assert graceful handling",
    },
    "UnicodeDecodeError": {
        "cause": "text is read without declaring an encoding that matches the data",
        "category": "input-validation",
        "strategy": "explicit-encoding",
        "explanation": "Reading with the platform default encoding breaks on non-UTF-8 bytes.",
        "verification": "read the offending bytes with the new encoding and assert success",
    },
    "RecursionError": {
        "cause": "a recursive structure has no base case for this input",
        "category": "boundary",
        "strategy": "base-case-guard",
        "explanation": "Cyclic input (self-referencing objects, symlinks) recurses forever.",
        "verification": "call with the cyclic input and assert termination",
    },
    "AssertionError": {
        "cause": "an invariant the code asserted no longer holds for this input",
        "category": "logic",
        "strategy": "reconcile-invariant",
        "explanation": "The assertion encodes an assumption that changed — either the caller changed or the data contract did.",
        "verification": "run the asserting test and assert the fixed invariant",
    },
    "OperationalError": {
        "cause": "the database rejected the statement (schema drift, constraint, or connection)",
        "category": "state",
        "strategy": "schema-reconcile",
        "explanation": "Schema drift between migrations and code is the usual suspect.",
        "verification": "run the migration/test suite against a clean schema",
    },
}


def _is_test_file(path: str) -> bool:
    from ..repo.symbols import is_test_path

    return is_test_path(path)


def _traceback_blocks(text: str) -> list[list[tuple[LogSignal, str]]]:
    """Split a log into traceback blocks: ``[(frame_signal, error_type), ...]``."""
    blocks: list[list[tuple[LogSignal, str]]] = []
    current: list[tuple[LogSignal, str]] = []
    for line in (text or "").splitlines():
        if TRACEBACK_LINE.search(line) or PY_FRAME_LINE.search(line):
            if TRACEBACK_LINE.search(line) and current:
                blocks.append(current)
                current = []
            match = PY_FRAME_LINE.search(line)
            if match:
                current.append(
                    (
                        LogSignal(
                            kind="frame",
                            path=match.group("file"),
                            lineno=int(match.group("line")),
                            function=(match.group("func") or "").strip(),
                            language="python",
                            raw=line,
                        ),
                        "",
                    )
                )
            continue
        error_match = PY_ERROR_LINE.match(line.strip())
        if error_match and current:
            current[-1] = (current[-1][0], error_match.group("type"))
        elif current and line.strip() and not line.startswith(" "):
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _suspect_from_test_frame(ctx: SkillContext, path: str, lineno: int) -> tuple[str, int, str] | None:
    """Find the application symbol a failing test line calls into."""
    lines = ctx.index.source_lines(path)
    if not lines:
        return None
    start = max(0, lineno - 6)
    window = "\n".join(lines[start : min(len(lines), lineno + 1)])
    candidates = re.findall(r"([A-Za-z_]\w*)\.([A-Za-z_]\w+)\s*\(", window) + [
        (None, name) for name in re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", window)
    ]
    for module_name, name in candidates:
        if name in {"assertEqual", "assertTrue", "assertFalse", "assertRaises", "assertIn", "assertIsNone", "assertAlmostEqual", "assertGreater", "assertLess", "patch", "raises", "len", "int", "str", "float", "print", "range"}:
            continue
        for symbol in ctx.index.find_symbols(name):
            if _is_test_file(symbol.path):
                continue
            if module_name and module_name not in symbol.path and module_name not in " ".join(
                ctx.index.files.get(path).imported_names if ctx.index.files.get(path) else []
            ):
                continue
            return symbol.path, symbol.lineno, symbol.name
    return None


def _primary_error(errors: list[LogSignal], block_frames: list[tuple[LogSignal, str]]) -> LogSignal | None:
    """Prefer the error that belongs to the first application traceback."""
    block_types = {error_type for _, error_type in block_frames if error_type}
    for error in errors:
        if error.value in block_types:
            return error
    return errors[0] if errors else None


class TracebackLocalizer(Skill):
    name = "traceback_localizer"
    title = "Traceback localisation"
    description = "Maps stack frames onto indexed symbols and turns the deepest frame into a grounded hypothesis."
    priority = 95
    cost = "fast"
    trigger_signals = ("frame",)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        frames = ctx.signal_frames()
        errors = [s for s in ctx.signals if s.kind == "error"]
        if not frames and not errors:
            return result

        blocks = _traceback_blocks(ctx.text or "")
        resolved: list[dict] = []
        for position, frame in enumerate(frames):
            path = ctx.resolve_path(frame.path)
            if not path:
                continue
            symbol = ctx.symbol_at(path, frame.lineno)
            context = ctx.index.context(path, frame.lineno, before=5, after=5)
            blame = ctx.history.evidence_for_line(path, frame.lineno) if ctx.history.enabled else {}
            depth_weight = (position + 1) / len(frames)
            is_test = _is_test_file(path)
            confidence = (0.5 if is_test else 0.62) + (0.28 if not is_test else 0.1) * depth_weight
            evidence = ctx.evidence(
                kind="source",
                claim=f"frame {position + 1}/{len(frames)}: {frame.function or '(module)'} at {path}:{frame.lineno}"
                + (" [test harness frame]" if is_test else ""),
                detail=blame.get("summary", ""),
                path=path,
                lineno=frame.lineno,
                snippet=context.get("text", ""),
                confidence=confidence,
                source="stack trace",
                skill=self.name,
            )
            resolved.append(
                {"frame": frame, "path": path, "symbol": symbol, "evidence": evidence, "blame": blame, "is_test": is_test}
            )
            if blame:
                result.evidence.append(
                    ctx.evidence(
                        kind="history",
                        claim=blame.get("summary", f"history unavailable for {path}:{frame.lineno}"),
                        path=path,
                        lineno=frame.lineno,
                        snippet="\n".join(
                            f"{c['date']} {c['sha']} {c['subject']}" for c in blame.get("file_commits", [])[:3]
                        ),
                        confidence=0.6 if blame.get("blame") else 0.35,
                        source="git blame",
                        skill=self.name,
                    )
                )

        if not resolved:
            primary_error = errors[0] if errors else None
            if primary_error:
                result.notes.append("traceback frames did not map to indexed files — the log may be from another checkout")
                fallback = ctx.evidence(
                    kind="log",
                    claim=f"exception {primary_error.value} raised: {primary_error.message or 'no message'}",
                    snippet=primary_error.raw,
                    confidence=0.5,
                    source="log",
                    skill=self.name,
                )
                result.hypotheses.append(
                    Hypothesis(
                        cause=f"{primary_error.value} raised but no repository frame matched the log",
                        category="exception",
                        explanation="The stack trace does not line up with this checkout, so the cause is inferred from the exception alone.",
                        confidence=0.4,
                        evidence_ids=[fallback.id],
                        strategy="manual-review",
                        falsifier="run the failing command against this checkout and compare the traceback",
                        skill=self.name,
                    )
                )
            return result

        # Prefer the deepest *application* frame: a test harness frame is where the
        # failure was noticed, not where it was caused.
        application = [item for item in resolved if not item["is_test"]]
        primary_pool = application or resolved
        primary_block = blocks[0] if blocks else []
        block_paths = {ctx.resolve_path(frame.path) for frame, _ in primary_block}
        block_paths.discard("")
        preferred = [item for item in primary_pool if item["path"] in block_paths]
        deepest = (preferred or primary_pool)[-1]
        if application and resolved[-1]["is_test"]:
            result.notes.append(
                f"the deepest frame is the test harness ({resolved[-1]['path']}:{resolved[-1]['frame'].lineno}); "
                f"FixPilot attributes the cause to the application frame {deepest['path']}:{deepest['frame'].lineno}"
            )
        error = _primary_error(errors, primary_block)
        error_type = (error.value if error else "").split(".")[-1]
        playbook = EXCEPTION_PLAYBOOK.get(error_type, None)
        symbol_label = deepest["symbol"].name if deepest["symbol"] else "(module level)"
        cause = (
            f"{error_type or 'failure'} in {symbol_label} at {deepest['path']}:{deepest['frame'].lineno}"
        )
        explanation = ""
        category = "exception"
        strategy = "targeted-guard"
        if playbook:
            category = playbook["category"]
            strategy = playbook["strategy"]
            explanation = playbook["explanation"]
        if error and error.message:
            explanation = f"{explanation}\nReported message: {error.message}".strip()

        local_lines = ctx.index.source_lines(deepest["path"])
        suspicious_text = "\n".join(
            local_lines[max(0, deepest["frame"].lineno - 12) : deepest["frame"].lineno + 8]
        )
        result.evidence.append(
            ctx.evidence(
                kind="traceback",
                claim=f"deepest failing frame is {symbol_label} ({deepest['path']}:{deepest['frame'].lineno})",
                detail=error.message if error and error.message else "",
                path=deepest["path"],
                lineno=deepest["frame"].lineno,
                snippet=suspicious_text,
                confidence=0.88,
                source="stack trace",
                skill=self.name,
            )
        )
        hypothesis_confidence = 0.72 if playbook else 0.55
        result.hypotheses.append(
            Hypothesis(
                cause=cause,
                category=category,
                explanation=explanation or "The deepest frame is where execution stopped; the cause is usually one line above in the same function.",
                confidence=hypothesis_confidence,
                evidence_ids=[e.id for e in result.evidence],
                file=deepest["path"],
                lineno=deepest["frame"].lineno,
                symbol=deepest["symbol"].name if deepest["symbol"] else "",
                strategy=strategy,
                fix_plan=f"Inspect {deepest['path']} around line {deepest['frame'].lineno} and add the missing {category.replace('-', ' ')} handling.",
                verification_plan=playbook.get("verification", "reproduce the failing call and assert the fixed behaviour") if playbook else "reproduce the failing call",
                falsifier="if the deepest frame is correct, the cause is one frame earlier — walk the traceback upwards",
                skill=self.name,
            )
        )
        # If every frame is a test frame, the suspect is whatever the test called.
        suspect = None
        if deepest["is_test"]:
            suspect = _suspect_from_test_frame(ctx, deepest["path"], deepest["frame"].lineno)
            if suspect:
                suspect_file, suspect_line, suspect_name = suspect
                suspect_tests = ctx.graph.tests_for(suspect_file)
                suspect_evidence = ctx.evidence(
                    kind="source",
                    claim=(
                        f"the failing test exercises `{suspect_name}` in {suspect_file}:{suspect_line} — "
                        "this is the code under test"
                    ),
                    detail="The traceback only contains test frames, so the failing assertion names the suspect directly.",
                    path=suspect_file,
                    lineno=suspect_line,
                    snippet=ctx.index.context(suspect_file, suspect_line).get("text", ""),
                    confidence=0.72,
                    source="test frame call site",
                    skill=self.name,
                )
                result.evidence.append(suspect_evidence)
                result.hypotheses.append(
                    Hypothesis(
                        cause=(
                            f"{symbol_label} fails because the code under test, `{suspect_name}` "
                            f"in {suspect_file}:{suspect_line}, does not satisfy its contract"
                        ),
                        category=category if category != "logic" else "input-validation",
                        explanation=(
                            "The reported failure has no application frame — the exception never escapes the module "
                            "under test, which itself is a strong signal (swallowed errors, wrong return type, or a "
                            "missing guard). The assertion in the test describes the expected contract."
                        ),
                        confidence=0.6,
                        evidence_ids=[suspect_evidence.id],
                        file=suspect_file,
                        lineno=suspect_line,
                        symbol=suspect_name,
                        strategy="narrow-exception",
                        fix_plan=f"Inspect {suspect_file}:{suspect_line} (`{suspect_name}`) and make the contract in the failing test hold.",
                        verification_plan=f"run {', '.join(suspect_tests[:2]) or 'the failing test'} after the change",
                        falsifier="if the test itself asserts the wrong contract, fix the test instead — check the docstring",
                        skill=self.name,
                    )
                )
        result.outputs = {
            "primary_file": deepest["path"],
            "primary_line": deepest["frame"].lineno,
            "primary_symbol": deepest["symbol"].name if deepest["symbol"] else "",
            "frames": [
                {
                    "path": item["path"],
                    "lineno": item["frame"].lineno,
                    "function": item["frame"].function,
                    "symbol": item["symbol"].name if item["symbol"] else "",
                }
                for item in resolved
            ],
            "error_type": error_type,
            "error_message": error.message if error else "",
        }
        return result


# --------------------------------------------------------------------------
# Log signatures
# --------------------------------------------------------------------------

LOG_SIGNATURES: tuple[dict[str, str], ...] = (
    {
        "id": "connection-refused",
        "pattern": r"connection refused|ECONNREFUSED|failed to connect|connection reset by peer",
        "cause": "a required service is not reachable from the environment running the code",
        "category": "resource",
        "strategy": "retry-with-backoff",
        "explanation": "Either the service address is wrong for this environment or the call happens before the dependency is ready.",
        "verification": "start with the dependency unreachable and assert a clear, retried failure",
    },
    {
        "id": "timeout",
        "pattern": r"\btimeout(ed)?\b|deadline exceeded|ETIMEDOUT|Read timed out",
        "cause": "an I/O or lock operation exceeds its deadline under load",
        "category": "resource",
        "strategy": "timeout-and-retry",
        "explanation": "Timeouts in CI usually mean an unbounded wait or a default that is tuned for a developer laptop.",
        "verification": "inject latency and assert the timeout is respected and retried",
    },
    {
        "id": "none-type",
        "pattern": r"NoneType|None has no attribute|object has no attribute|undefined is not an object",
        "cause": "a value that is assumed present is None at this point in the flow",
        "category": "null-safety",
        "strategy": "guard-none-attribute",
        "explanation": "The producer of the value can legitimately return None (lookup miss, unset config, empty response).",
        "verification": "call the path with the missing value and assert the guarded behaviour",
    },
    {
        "id": "permission-denied",
        "pattern": r"permission denied|EACCES|access is denied",
        "cause": "the process lacks rights on a path it must write",
        "category": "resource",
        "strategy": "permission-preflight",
        "explanation": "Common in containers where the working directory is owned by root.",
        "verification": "assert the code fails with an actionable message instead of a raw traceback",
    },
    {
        "id": "disk-full",
        "pattern": r"no space left on device|disk quota exceeded|ENOSPC",
        "cause": "the volume holding artifacts is full",
        "category": "resource",
        "strategy": "preflight-disk-space",
        "explanation": "Undeleted build artifacts or unbounded log growth.",
        "verification": "assert a preflight check reports the condition before writing",
    },
    {
        "id": "rate-limit",
        "pattern": r"\b429\b|rate limit|too many requests|quota exceeded",
        "cause": "an external API is throttling the caller",
        "category": "resource",
        "strategy": "respect-retry-after",
        "explanation": "The client ignores Retry-After and hammers the API until it fails.",
        "verification": "simulate a 429 with Retry-After and assert the backoff",
    },
    {
        "id": "oom",
        "pattern": r"out of memory|OOMKilled|MemoryError|cannot allocate memory|JavaScript heap out of memory",
        "cause": "an unbounded data structure or buffer grows until the process dies",
        "category": "resource",
        "strategy": "bound-memory",
        "explanation": "Look for loading an entire dataset into memory, or an accumulating cache without eviction.",
        "verification": "run with a bounded input and assert memory stays flat",
    },
    {
        "id": "encoding",
        "pattern": r"UnicodeDecodeError|invalid byte sequence|codec can't decode|Illegal character",
        "cause": "text is read or written with the wrong encoding",
        "category": "input-validation",
        "strategy": "explicit-encoding",
        "explanation": "The platform default encoding differs between the dev machine and CI.",
        "verification": "read the offending bytes with the explicit encoding",
    },
    {
        "id": "migration",
        "pattern": r"no such table|no such column|relation .* does not exist|column .* does not exist|migration",
        "cause": "schema and code are out of sync (missing or reordered migration)",
        "category": "state",
        "strategy": "schema-reconcile",
        "explanation": "The code expects the migrated schema while the database is at an older revision.",
        "verification": "run migrations from scratch and then the failing query",
    },
    {
        "id": "dependency-version",
        "pattern": r"No module named|Cannot find module|ModuleNotFoundError|ImportError|undefined symbol|version.*(incompatible|mismatch)|has no attribute .* \(most likely due to a circular import\)",
        "cause": "a dependency is missing, undeclared, or pinned to an incompatible version",
        "category": "dependency",
        "strategy": "declared-dependency-guard",
        "explanation": "The environment that failed does not match the environment that was tested.",
        "verification": "install from the manifest in a clean environment and re-import",
    },
    {
        "id": "deadlock",
        "pattern": r"deadlock|database is locked|resource temporarily unavailable|EAGAIN",
        "cause": "two operations hold locks in opposite order, or a lock is never released",
        "category": "concurrency",
        "strategy": "serialise-lock-order",
        "explanation": "Concurrent writers with inconsistent lock ordering, often surfaced only under parallel test runs.",
        "verification": "run the concurrent test repeatedly with the new lock ordering",
    },
    {
        "id": "cors",
        "pattern": r"CORS|Access-Control-Allow-Origin|blocked by CORS policy|preflight",
        "cause": "the API does not allow the origin the app is served from",
        "category": "config",
        "strategy": "explicit-cors-config",
        "explanation": "Allowed origins are hard-coded for localhost or a single domain.",
        "verification": "assert the configured origin list includes the failing origin",
    },
    {
        "id": "flaky-timing",
        "pattern": r"flake|flaky|intermittent|sometimes fails|works locally|passes on retry",
        "cause": "the failure is timing or ordering dependent rather than a deterministic logic error",
        "category": "concurrency",
        "strategy": "stabilise-timing",
        "explanation": "Sleeps, polling without conditions, and shared mutable fixtures are the usual causes.",
        "verification": "run the test 5 times in a row and assert stability",
    },
)


class LogPatternAnalyzer(Skill):
    name = "log_pattern_analyzer"
    title = "Log signature analysis"
    description = "Matches logs against known operational failure signatures and proposes the matching strategy."
    priority = 80
    cost = "fast"
    trigger_signals = ("frame", "panic", "level", "assertion", "file_ref")

    # Signals rarely carry a literal "log" kind, so match on text too.
    triggers = (r"\b(error|exception|failed|failure|timeout|refused|denied|panic|fatal|traceback)\b",)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        text = ctx.text or ""
        if not text:
            return result
        matched: list[dict] = []
        for signature in LOG_SIGNATURES:
            match = re.search(signature["pattern"], text, re.I)
            if not match:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            matched.append({**signature, "match": match.group(0), "line": line_no})
        for signature in matched[:6]:
            evidence = ctx.evidence(
                kind="log",
                claim=f"log signature '{signature['id']}' matched: {signature['match'][:80]}",
                detail=signature["explanation"],
                lineno=signature["line"],
                confidence=0.75 if signature["id"] not in {"timeout", "flaky-timing"} else 0.6,
                source="log text",
                skill=self.name,
            )
            target_file = ""
            target_line = 0
            for frame in ctx.signal_frames():
                resolved = ctx.resolve_path(frame.path)
                if resolved:
                    target_file, target_line = resolved, frame.lineno
                    break
            result.hypotheses.append(
                Hypothesis(
                    cause=signature["cause"],
                    category=signature["category"],
                    explanation=signature["explanation"],
                    confidence=0.6,
                    evidence_ids=[evidence.id],
                    file=target_file,
                    lineno=target_line,
                    strategy=signature["strategy"],
                    fix_plan=f"Apply strategy '{signature['strategy']}' at the code path that produced this log line.",
                    verification_plan=signature["verification"],
                    falsifier="reproduce the log line locally; if it cannot be reproduced the signature may be incidental",
                    skill=self.name,
                )
            )
        result.outputs = {"matched_signatures": [item["id"] for item in matched]}
        return result


# --------------------------------------------------------------------------
# Static smells
# --------------------------------------------------------------------------

SMELL_PLAYBOOK: dict[str, dict[str, str]] = {
    "bare-except": {
        "cause": "a bare `except:` hides the real error and can swallow interrupts",
        "category": "exception",
        "strategy": "narrow-exception",
        "explanation": "The code catches everything, so the original failure surfaced somewhere else or not at all.",
    },
    "swallowed-exception": {
        "cause": "an exception is caught and discarded, so a failure continues silently",
        "category": "exception",
        "strategy": "log-and-reraise",
        "explanation": "Silent suppression turns a small failure into a confusing downstream crash.",
    },
    "mutable-default": {
        "cause": "a mutable default argument is shared across calls, leaking state between invocations",
        "category": "state",
        "strategy": "none-default-sentinel",
        "explanation": "The default list/dict is created once at function definition time.",
    },
    "none-arithmetic": {
        "cause": "arithmetic is performed on a `.get()` result that can be None",
        "category": "null-safety",
        "strategy": "guard-none-attribute",
        "explanation": "A missing key yields None, and the next operator raises TypeError.",
    },
    "int-parse": {
        "cause": "a string is converted to a number without validating its shape",
        "category": "input-validation",
        "strategy": "validate-input",
        "explanation": "Empty strings, placeholders and locale formats all raise ValueError here.",
    },
    "float-parse": {
        "cause": "a float is parsed without handling locale/format edge cases",
        "category": "input-validation",
        "strategy": "validate-input",
        "explanation": "Comma decimal separators and empty strings are common sources of ValueError.",
    },
    "range-len": {
        "cause": "index-based iteration over a sequence invites off-by-one errors",
        "category": "boundary",
        "strategy": "iterate-directly",
        "explanation": "Iterating with range(len(...)) hides intent and misfires on mutated sequences.",
    },
    "slice": {
        "cause": "slice arithmetic is a common home for off-by-one errors",
        "category": "boundary",
        "strategy": "bounds-check",
        "explanation": "Check the intended inclusive/exclusive boundaries against the data contract.",
    },
    "sleep": {
        "cause": "a fixed sleep is used to wait for an asynchronous condition",
        "category": "concurrency",
        "strategy": "poll-with-condition",
        "explanation": "Sleeps are either too short (flaky) or too long (slow) and are the top flakiness cause.",
    },
    "shell-true": {
        "cause": "subprocess is invoked with shell=True, which mixes input with command syntax",
        "category": "unknown",
        "strategy": "argv-list-exec",
        "explanation": "Arguments interpolated into a shell string become command injection.",
    },
    "dynamic-exec": {
        "cause": "eval/exec is used to run dynamic code",
        "category": "unknown",
        "strategy": "replace-eval",
        "explanation": "Dynamic execution hides control flow and is a security risk.",
    },
    "syntax-error": {
        "cause": "the file does not parse, so nothing below this point can run",
        "category": "exception",
        "strategy": "fix-syntax",
        "explanation": "Syntax errors are deterministic: fix the parse error before reasoning about behaviour.",
    },
}


class StaticSmellScanner(Skill):
    name = "static_smell_scanner"
    title = "Static smell scan"
    description = "Scans the implicated files (and their hot paths) for known defect patterns."
    priority = 70
    cost = "fast"
    always = True

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        targets = self._targets(ctx)
        if not targets:
            return result
        result.notes.append(f"scanned {len(targets)} file(s) for defect patterns")
        for path in targets[:8]:
            facts = ctx.index.files.get(path)
            if facts is None:
                continue
            for signal in facts.risk_signals[:12]:
                playbook = SMELL_PLAYBOOK.get(signal["tag"])
                if not playbook:
                    continue
                lineno = int(signal.get("lineno") or 0)
                context = ctx.index.context(path, max(1, lineno), before=3, after=3)
                evidence = ctx.evidence(
                    kind="pattern",
                    claim=f"{playbook['cause']} ({path}:{lineno})",
                    detail=f"pattern '{signal['tag']}': {signal.get('text', '')[:120]}",
                    path=path,
                    lineno=lineno,
                    snippet=context.get("text", ""),
                    confidence=0.55,
                    source="static scan",
                    skill=self.name,
                )
                result.hypotheses.append(
                    Hypothesis(
                        cause=f"{playbook['cause']} in {path}:{lineno}",
                        category=playbook["category"],
                        explanation=playbook["explanation"],
                        confidence=0.45,
                        evidence_ids=[evidence.id],
                        file=path,
                        lineno=lineno,
                        symbol=ctx.symbol_at(path, max(1, lineno)).name if ctx.symbol_at(path, max(1, lineno)) else "",
                        strategy=playbook["strategy"],
                        fix_plan=f"Apply strategy '{playbook['strategy']}' at {path}:{lineno}.",
                        verification_plan="run the surrounding tests after the change",
                        falsifier="the pattern may be intentional — confirm with a failing test",
                        skill=self.name,
                    )
                )
            if any(s.get("tag") == "syntax-error" for s in facts.risk_signals):
                result.notes.append(f"{path} currently has a syntax error; behaviour reasoning is unreliable until it is fixed")
        return result

    def _targets(self, ctx: SkillContext) -> list[str]:
        scored: dict[str, float] = {}

        def bump(path: str, weight: float) -> None:
            if not path or path not in ctx.index.files:
                return
            scored[path] = scored.get(path, 0.0) + weight

        for frame in ctx.signal_frames():
            bump(ctx.resolve_path(frame.path), 1.0 + 0.1 * len(ctx.signal_frames()))
        for signal in ctx.signals:
            if signal.kind in {"file_ref", "test_failure"}:
                resolved = ctx.resolve_path(signal.path)
                bump(resolved, 0.7)
                if signal.kind == "test_failure" and resolved:
                    for symbol in ctx.index.symbols.values():
                        if symbol.path == resolved and symbol.is_test:
                            bump(symbol.path, 0.2)
        for identifier in ctx.scratch.get("mentioned_identifiers", []):
            for symbol in ctx.index.find_symbols(identifier.split(".")[-1]):
                bump(symbol.path, 0.8)
        if not scored:
            for item in ctx.memory.load().key_files[:5]:
                bump(item.get("path", ""), 0.3)
        return [path for path, _ in sorted(scored.items(), key=lambda kv: -kv[1])]


class UiScreenshotSkill(Skill):
    name = "ui_screenshot_triage"
    title = "UI / screenshot triage"
    description = "Handles visual bug reports: locates the component, styles or endpoint behind the screen capture."
    priority = 60
    cost = "normal"
    triggers = (r"\b(button|modal|layout|css|style|overflow|align|responsive|viewport|render|dark mode|font|spacing|click|tap|screen)\b",)
    trigger_signals = ("file_ref",)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        attachments = ctx.ingested.attachments
        if not attachments and "screen" not in (ctx.text or "").lower():
            return result
        for attachment in attachments:
            hint = attachment.get("vision_hint", "unclassified_screenshot")
            evidence = ctx.evidence(
                kind="screenshot",
                claim=f"visual report attached ({attachment.get('filename', 'image')}, {hint})",
                detail="FixPilot will route this to a vision-capable local model before reasoning about it.",
                confidence=0.5,
                source="attachment",
                skill=self.name,
            )
            result.evidence.append(evidence)
            result.outputs.setdefault("attachments", []).append(attachment.get("filename", ""))
        frontend_files = [
            path
            for path, facts in ctx.index.files.items()
            if facts.language in {"javascript", "typescript", "css", "html"}
        ]
        result.outputs["frontend_files"] = len(frontend_files)
        keywords = [word for word in re.findall(r"[A-Za-z][\w-]{2,}", ctx.text or "") if len(word) > 3][:6]
        located = []
        for keyword in keywords:
            hits = ctx.index.search_code(keyword, limit=3)
            located.extend(hit for hit in hits if hit["path"] in frontend_files)
        if located:
            top = located[0]
            evidence = ctx.evidence(
                kind="source",
                claim=f"likely UI code behind the report: {top['path']}:{top['lineno']}",
                snippet=top["line"],
                path=top["path"],
                lineno=top["lineno"],
                confidence=0.45,
                source="keyword search over front-end files",
                skill=self.name,
            )
            result.hypotheses.append(
                Hypothesis(
                    cause=f"UI defect in {top['path']}:{top['lineno']} related to the reported visual problem",
                    category="ui",
                    explanation="FixPilot matched the words in the report against front-end source; visual issues usually live in styling, conditional rendering, or a payload shape mismatch.",
                    confidence=0.4,
                    evidence_ids=[evidence.id],
                    file=top["path"],
                    lineno=top["lineno"],
                    strategy="inspect-render-path",
                    fix_plan="Trace the component's data source and rendering conditions; check whether the payload shape changed.",
                    verification_plan="run the front-end test/build and re-render the affected screen",
                    falsifier="if the screenshot is from a different app version, the location will not match",
                    skill=self.name,
                )
            )
        else:
            result.notes.append("no front-end match for the screenshot keywords — a vision model pass is needed to read the image")
        return result
