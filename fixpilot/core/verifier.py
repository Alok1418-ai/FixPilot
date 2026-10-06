"""Verification: syntax checks, test execution, reproduction runs, and a verdict.

"Verified" is a claim FixPilot has to earn.  The verifier refuses to mark a patch
verified unless something actually executed: the project's tests, or a generated
reproduction when the project has no tests.  A patch whose tests fail is never
reported as fixed — it goes back through refinement, and if refinement is
exhausted the change is rolled back automatically.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..repo.indexer import RepoIndex
from ..repo.symbols import is_test_path
from ..security.executor import ExecResult, SandboxExecutor
from ..util import excerpt

VERIFIED = "verified"
PARTIAL = "partial"
FAILED = "failed"
ERROR = "error"

PYTEST_FRAME = re.compile(r"^(?P<node>[\w./\\\-]+\.py::[\w\[\]\.\-]+)\s+(?P<outcome>FAILED|ERROR|PASSED|XFAIL|XPASS)", re.M)
PYTEST_SUMMARY = re.compile(r"(?P<counts>(?:\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings|deselected|selected)[,\s]*)+)", re.I)
UNITTEST_SUMMARY = re.compile(r"^(?:OK|FAILED)\s*(?:\((?P<detail>[^)]*)\))?", re.M)
UNITTEST_RAN = re.compile(r"^Ran (?P<count>\d+) tests? in (?P<duration>[\d.]+)s", re.M)
JEST_SUMMARY = re.compile(r"Tests:\s*(?P<body>[^\n]+)")
GO_FAIL = re.compile(r"^--- FAIL: (?P<name>\S+)", re.M)
GO_OK = re.compile(r"^ok\s+\S+", re.M)
NODE_FAIL = re.compile(r"^\s*(?:\d+\)\s*)?(?P<name>[\w\s.\-]+)$", re.M)
GENERIC_PASS = re.compile(r"\b(?P<count>\d+)\s+(?:tests?\s+)?pass(?:ed|ing)\b", re.I)
GENERIC_FAIL = re.compile(r"\b(?P<count>\d+)\s+(?:tests?\s+)?fail(?:ed|ing|ures?)\b", re.I)
ASSERTION_DETAIL = re.compile(r"^(?:E\s+|AssertionError:?\s*)?(?P<message>.{0,400})$", re.M)


@dataclass(slots=True)
class TestFailure:
    name: str = ""
    message: str = ""
    path: str = ""
    lineno: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "message": self.message[:400], "path": self.path, "lineno": self.lineno}


@dataclass(slots=True)
class TestReport:
    framework: str = "unknown"
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    duration_s: float = 0.0
    failures: list[TestFailure] = field(default_factory=list)
    summary_line: str = ""
    parse_confidence: float = 0.3
    raw_excerpt: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errors == 0 and self.total > 0

    def to_dict(self) -> dict:
        return {
            "framework": self.framework,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "total": self.total,
            "duration_s": round(self.duration_s, 3),
            "failures": [f.to_dict() for f in self.failures],
            "summary": self.summary_line,
            "parse_confidence": round(self.parse_confidence, 2),
        }

    def render(self) -> str:
        if self.total == 0:
            return f"{self.framework}: no tests executed"
        return f"{self.framework}: {self.passed} passed, {self.failed} failed, {self.errors} errors, {self.skipped} skipped ({self.total} total)"


def parse_test_output(text: str) -> TestReport:
    """Best-effort structured parse of common test runners."""
    report = TestReport()
    if not text:
        return report
    report.raw_excerpt = text[-4000:]
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)

    # --- pytest -------------------------------------------------------
    pytest_nodes = list(PYTEST_FRAME.finditer(plain))
    pytest_summary = re.search(r"^=+ (?P<body>[^=\n]*(?:passed|failed|error)[^=\n]*) =+$", plain, re.M)
    if pytest_nodes or pytest_summary or "=== FAILURES ===" in plain:
        report.framework = "pytest"
        report.parse_confidence = 0.9
        for match in pytest_nodes:
            outcome = match.group("outcome")
            if outcome == "PASSED":
                report.passed += 1
            elif outcome in {"FAILED", "ERROR"}:
                if outcome == "FAILED":
                    report.failed += 1
                else:
                    report.errors += 1
                node = match.group("node")
                path, _, name = node.partition("::")
                report.failures.append(TestFailure(name=node, path=path.replace("\\", "/"), message=name))
        if pytest_summary:
            body = pytest_summary.group("body")
            report.summary_line = body.strip()
            for count, word in re.findall(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)", body, re.I):
                word = word.lower()
                value = int(count)
                if word == "passed":
                    report.passed = max(report.passed, value)
                elif word == "failed":
                    report.failed = max(report.failed, value)
                elif word.startswith("error"):
                    report.errors = max(report.errors, value)
                elif word == "skipped":
                    report.skipped = max(report.skipped, value)
            for match in re.finditer(r"^_{5,}\s*(?P<name>\S+)\s*_{5,}$", plain, re.M):
                name = match.group("name").strip()
                if name and not any(f.name.endswith(name) for f in report.failures):
                    path, _, test_name = name.partition("::")
                    report.failures.append(TestFailure(name=name, path=path.replace("\\", "/"), message=test_name))
                    report.failed = max(report.failed, len(report.failures))

    # --- unittest -----------------------------------------------------
    unittest_ran = UNITTEST_RAN.search(plain)
    unittest_ok = re.search(r"^OK(?: \((?P<detail>[^)]*)\))?\s*$", plain, re.M)
    unittest_fail = re.search(r"^FAILED \((?P<detail>[^)]*)\)", plain, re.M)
    if report.framework == "unknown" and (unittest_ran or unittest_ok or unittest_fail):
        report.framework = "unittest"
        report.parse_confidence = 0.85
        if unittest_ran:
            report.total = int(unittest_ran.group("count"))
            report.duration_s = float(unittest_ran.group("duration"))
        if unittest_ok:
            report.passed = report.total or 0
            report.skipped = _detail_int(unittest_ok.group("detail"), "skipped")
            report.summary_line = "OK"
        if unittest_fail:
            detail = unittest_fail.group("detail") or ""
            report.failed = _detail_int(detail, "failures")
            report.errors = _detail_int(detail, "errors")
            report.skipped = _detail_int(detail, "skipped")
            report.passed = max(0, report.total - report.failed - report.errors - report.skipped)
            report.summary_line = f"FAILED ({detail})"

    # --- jest / vitest -------------------------------------------------
    jest = JEST_SUMMARY.search(plain)
    if jest and report.framework == "unknown":
        report.framework = "jest"
        report.parse_confidence = 0.85
        body = jest.group("body")
        report.summary_line = body.strip()
        for count, word in re.findall(r"(\d+)\s+(passed|failed|skipped|todo|total)", body, re.I):
            value = int(count)
            word = word.lower()
            if word == "passed":
                report.passed = value
            elif word == "failed":
                report.failed = value
            elif word == "skipped":
                report.skipped = value
            elif word == "total":
                report.total = value
        for match in re.finditer(r"^\s*●\s+(?P<name>.+)$", plain, re.M):
            name = match.group("name").strip()
            if not name.startswith(("Test suite", "Cannot")):
                report.failures.append(TestFailure(name=name))

    # --- go test -------------------------------------------------------
    if report.framework == "unknown" and (GO_FAIL.search(plain) or GO_OK.search(plain)):
        report.framework = "go test"
        report.parse_confidence = 0.8
        for match in GO_FAIL.finditer(plain):
            report.failed += 1
            report.failures.append(TestFailure(name=match.group("name").strip()))
        report.passed = len(GO_OK.findall(plain))

    # --- generic -------------------------------------------------------
    if report.framework == "unknown":
        generic_fail = GENERIC_FAIL.search(plain)
        generic_pass = GENERIC_PASS.search(plain)
        if generic_fail or generic_pass:
            report.framework = "generic"
            report.parse_confidence = 0.45
            report.failed = int(generic_fail.group("count")) if generic_fail else 0
            report.passed = int(generic_pass.group("count")) if generic_pass else 0
            report.summary_line = excerpt(generic_fail.group(0) if generic_fail else generic_pass.group(0), 120)
    # Final sanity net: if the runner's own verdict line says FAILED, the report
    # must never look clean. A false "verified" is the worst failure this tool
    # can produce, so the runner's verdict always outranks a partial parse.
    verdict = (report.summary_line or "").strip().upper()
    if verdict.startswith(("FAILED", "FAIL", "ERROR")) and report.failed == 0 and report.errors == 0:
        report.errors = 1
    if verdict.startswith(("FAILED", "FAIL")) and report.failed == 0 and report.errors == 1 and report.passed == 0:
        report.failed = 1
    if report.total == 0:
        report.total = report.passed + report.failed + report.errors + report.skipped
    return report


MISSING_RUNNER_MARKERS = (
    "no module named pytest",
    "no module named 'pytest'",
    "command not found",
    "not recognized as an internal or external command",
    "cannot find module 'jest'",
    "could not determine executable to run",
    "executable file not found",
)


def _runner_missing(output: str) -> bool:
    return any(marker in output for marker in MISSING_RUNNER_MARKERS)


def _runner_fallback(index: RepoIndex) -> list[str]:
    """A stdlib-only command that still exercises the tests, when one exists."""
    has_python_tests = any(is_test_path(path) and facts.language == "python" for path, facts in index.files.items())
    if has_python_tests:
        return ["python3", "-m", "unittest", "discover", "-v"]
    return []


def build_test_command(base: list[str], targets: list[str], test_files: list[str]) -> list[str]:
    """Narrow the project's test command to the tests that matter for this change."""
    if not base:
        return []
    if base[:3] == ["python3", "-m", "pytest"]:
        selected = targets or test_files[:6]
        return [*base, *selected] if selected else base
    if base[:3] == ["python3", "-m", "unittest"]:
        modules = [_module_name(target.split("::")[0]) for target in targets] or [
            _module_name(path) for path in test_files[:6]
        ]
        modules = [module for module in dict.fromkeys(modules) if module]
        if modules:
            return ["python3", "-m", "unittest", "-v", *modules]
        return base
    if base[:2] == ["go", "test"] and test_files:
        packages = sorted({f"./{'/'.join(path.split('/')[:-1])}/..." if "/" in path else "./..." for path in test_files})
        return ["go", "test", *packages[:4]]
    if base[0] in {"npx", "npm", "yarn", "pnpm"} and test_files and base[:2] in (["npx", "jest"], ["npx", "vitest"]):
        return [*base, *test_files[:4]]
    return base


def _module_name(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    if cleaned.endswith(".py"):
        cleaned = cleaned[:-3]
    if cleaned.endswith("/__init__"):
        cleaned = cleaned[: -len("/__init__")]
    return cleaned.replace("/", ".").strip(".")


def _detail_int(detail: str | None, word: str) -> int:
    """Read a count written either as ``3 failures`` or as ``failures=3``."""
    if not detail:
        return 0
    for pattern in (rf"(\d+)\s+{word}", rf"{word}\s*=\s*(\d+)"):
        match = re.search(pattern, detail, re.I)
        if match:
            return int(match.group(1))
    return 0


def syntax_check(paths: list[str], root: Path) -> dict:
    """Parse-check every changed file that we can verify cheaply."""
    results: dict[str, dict] = {}
    for path in paths:
        target = root / path
        if not target.exists():
            results[path] = {"ok": False, "error": "file missing after patch"}
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results[path] = {"ok": False, "error": f"unreadable: {exc}"}
            continue
        if path.endswith(".py"):
            try:
                ast.parse(text, filename=path)
                results[path] = {"ok": True}
            except SyntaxError as exc:
                results[path] = {"ok": False, "error": f"line {exc.lineno}: {exc.msg}"}
        elif path.endswith((".json",)):
            import json

            try:
                json.loads(text)
                results[path] = {"ok": True}
            except json.JSONDecodeError as exc:
                results[path] = {"ok": False, "error": f"line {exc.lineno}: {exc.msg}"}
        else:
            results[path] = {"ok": True, "note": "no cheap parser for this language"}
    return results


@dataclass(slots=True)
class VerificationOutcome:
    status: str = ERROR
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    test_report: TestReport | None = None
    syntax: dict = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    coverage_hint: str = ""
    next_action: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "tests": self.test_report.to_dict() if self.test_report else None,
            "syntax": self.syntax,
            "runs": self.runs,
            "coverage_hint": self.coverage_hint,
            "next_action": self.next_action,
        }


class Verifier:
    def __init__(self, settings: Settings, executor: SandboxExecutor) -> None:
        self.settings = settings
        self.executor = executor

    # -- syntax --------------------------------------------------------
    def check_syntax(self, paths: list[str]) -> dict:
        return syntax_check(paths, self.settings.repo_root)

    # -- tests ---------------------------------------------------------
    def run_tests(
        self,
        index: RepoIndex,
        *,
        targets: list[str] | None = None,
        test_files: list[str] | None = None,
        timeout: int | None = None,
    ) -> tuple[TestReport, ExecResult]:
        base = list(index.test_command)
        if not base:
            return TestReport(framework="none", summary_line="no test runner configured"), ExecResult(
                error="no test command detected", command="(none)"
            )
        targeted = build_test_command(base, targets or [], test_files or [])
        result = self.executor.run(
            targeted,
            cwd=self.settings.repo_root,
            timeout=timeout or self.settings.sandbox.timeout_seconds,
            purpose="verify patch by running the relevant tests",
            approved=True,
        )
        report = parse_test_output(result.combined(60_000))
        if result.timed_out:
            report.summary_line = (report.summary_line + " [timeout]").strip()
        # The declared runner may simply not be installed (very common on CI
        # images and fresh clones). Try the stdlib runner before giving up.
        combined_output = (result.stdout + result.stderr).lower()
        if report.total == 0 and _runner_missing(combined_output):
            fallback_command = _runner_fallback(index)
            if fallback_command:
                fallback = self.executor.run(
                    fallback_command,
                    cwd=self.settings.repo_root,
                    timeout=timeout or self.settings.sandbox.timeout_seconds,
                    purpose="verify patch: declared runner unavailable, using the stdlib runner",
                    approved=True,
                )
                fallback_report = parse_test_output(fallback.combined(60_000))
                fallback_report.summary_line = (
                    fallback_report.summary_line + " [declared runner unavailable; stdlib runner used]"
                ).strip()
                if fallback_report.total > 0:
                    return fallback_report, fallback
        # A targeted run that collects nothing tells us the selection missed —
        # fall back to the full suite rather than claiming a clean verification.
        if targeted != base and (report.total == 0 or "no tests ran" in combined_output):
            fallback = self.executor.run(
                base,
                cwd=self.settings.repo_root,
                timeout=timeout or self.settings.sandbox.timeout_seconds,
                purpose="verify patch by running the full suite",
                approved=True,
            )
            fallback_report = parse_test_output(fallback.combined(60_000))
            fallback_report.summary_line = (fallback_report.summary_line + " [targeted run matched nothing; full suite used]").strip()
            return fallback_report, fallback
        return report, result

    def run_reproduction(self, script: str, timeout: int = 60) -> ExecResult:
        return self.executor.run(
            ["python3", script],
            cwd=self.settings.repo_root,
            timeout=timeout,
            purpose="run the generated reproduction harness",
        )

    # -- verdict -------------------------------------------------------
    def verify(
        self,
        index: RepoIndex,
        *,
        changed_files: list[str],
        test_targets: list[str] | None = None,
        test_files: list[str] | None = None,
        repro_script: str = "",
        run_tests: bool = True,
    ) -> VerificationOutcome:
        outcome = VerificationOutcome()
        outcome.syntax = self.check_syntax(changed_files)
        broken = [path for path, result in outcome.syntax.items() if not result.get("ok")]
        if broken:
            outcome.status = FAILED
            outcome.confidence = 0.05
            outcome.reasons.append(f"changed file(s) no longer parse: {', '.join(broken)}")
            outcome.next_action = "fix the syntax error introduced by the patch"
            return outcome

        if run_tests:
            report, exec_result = self.run_tests(index, targets=test_targets, test_files=test_files)
            outcome.test_report = report
            outcome.runs.append(
                {
                    "kind": "tests",
                    "command": exec_result.command,
                    "exit_code": exec_result.exit_code,
                    "duration_ms": exec_result.duration_ms,
                    "timed_out": exec_result.timed_out,
                    "summary": report.render(),
                    "stdout_tail": exec_result.stdout[-2500:],
                    "stderr_tail": exec_result.stderr[-2500:],
                }
            )
            covered = self._coverage_hint(index, changed_files)
            outcome.coverage_hint = covered
            runner_exit_ok = exec_result.exit_code == 0
            if report.total > 0 and report.ok and not runner_exit_ok:
                # Defensive: a clean parse over a non-zero exit is a parse bug,
                # not a passing suite.
                outcome.status = FAILED
                outcome.confidence = 0.1
                outcome.reasons.append(
                    f"the test runner exited with code {exec_result.exit_code} even though the output parsed cleanly — "
                    "treating this run as failed"
                )
                outcome.next_action = "inspect the raw command output"
                return outcome
            if report.total > 0 and report.ok and runner_exit_ok:
                outcome.status = VERIFIED
                outcome.confidence = 0.92 if covered else 0.78
                outcome.reasons.append(f"test suite passes ({report.render()})")
                if covered:
                    outcome.reasons.append("the tests that exercise the changed code were included in the run")
                else:
                    outcome.reasons.append("no test file references the changed module — passing tests are weaker evidence")
            elif report.total > 0 and not report.ok:
                outcome.status = FAILED
                outcome.confidence = 0.1
                outcome.reasons.append(f"tests failed after the patch ({report.render()})")
                for failure in report.failures[:3]:
                    outcome.reasons.append(f"failing: {failure.name} {excerpt(failure.message, 120)}")
                outcome.next_action = "refine the patch using the failure output"
                return outcome
            elif exec_result.timed_out:
                outcome.status = ERROR
                outcome.confidence = 0.0
                outcome.reasons.append("the test command exceeded its time budget")
                outcome.next_action = "narrow the test selection or raise the timeout"
                return outcome
            else:
                outcome.reasons.append("test command produced no parseable results")

        # Fall back to the reproduction harness when tests are unavailable.
        if outcome.status != VERIFIED:
            if repro_script:
                repro_result = self.run_reproduction(repro_script)
                outcome.runs.append(
                    {
                        "kind": "reproduction",
                        "command": repro_result.command,
                        "exit_code": repro_result.exit_code,
                        "duration_ms": repro_result.duration_ms,
                        "summary": "reproduction harness executed",
                        "stdout_tail": repro_result.stdout[-2500:],
                    }
                )
                if repro_result.ok:
                    outcome.status = PARTIAL
                    outcome.confidence = 0.55
                    outcome.reasons.append("no test suite available: verified by reproduction harness, syntax and import checks only")
                    outcome.next_action = "add a regression test capturing this failure"
                else:
                    outcome.status = FAILED
                    outcome.confidence = 0.15
                    outcome.reasons.append("the reproduction harness still fails after the patch")
                    outcome.next_action = "refine the patch; the failure is still reproducible"
                return outcome
            if not index.test_command:
                outcome.status = PARTIAL
                outcome.confidence = 0.45
                outcome.reasons.append("changed files parse cleanly, but this project has no test runner to execute")
                outcome.next_action = "add the missing test command or verify manually on the phone"
                return outcome
            outcome.status = ERROR
            outcome.confidence = 0.2
            outcome.reasons.append("verification could not be completed")
            outcome.next_action = "inspect the raw command output for details"
        return outcome

    @staticmethod
    def _coverage_hint(index: RepoIndex, changed_files: list[str]) -> bool:
        """Do any test files reference the changed modules?"""
        stems = {Path(path).stem for path in changed_files}
        for path, facts in index.files.items():
            if not is_test_path(path):
                continue
            tokens = set(facts.imported_names) | {imp.split("/")[-1].split(".")[-1] for imp in facts.imports}
            if stems & tokens:
                return True
            text = " ".join(facts.imports) + " " + path
            if any(stem in text for stem in stems):
                return True
        return False
