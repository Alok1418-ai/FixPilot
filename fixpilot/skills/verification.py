"""Skills that plan and build verification: test discovery and reproduction."""

from __future__ import annotations

import re
from pathlib import Path

from .base import Hypothesis, Skill, SkillContext, SkillResult
from ..util import atomic_write_text, excerpt

TEST_FRAMEWORK_HINTS: tuple[tuple[str, str], ...] = (
    ("pytest.ini", "pytest"),
    ("conftest.py", "pytest"),
    ("pyproject.toml", "pytest"),
    ("tox.ini", "pytest"),
    ("package.json", "npm"),
    ("go.mod", "go"),
    ("Cargo.toml", "cargo"),
    ("pom.xml", "maven"),
)


class TestLocator(Skill):
    name = "test_locator"
    title = "Test discovery"
    description = "Finds the tests that cover the implicated code and builds the exact verification command."
    priority = 85
    cost = "fast"
    always = True

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        base_command = ctx.index.test_command or self._fallback_command(ctx)
        targets: list[str] = []
        candidate_files: list[str] = []

        for frame in ctx.signal_frames():
            path = ctx.resolve_path(frame.path)
            if path:
                candidate_files.append(path)
        for signal in ctx.signals:
            if signal.kind == "test_failure" and signal.path:
                resolved = ctx.resolve_path(signal.path)
                if resolved:
                    candidate_files.append(resolved)
        if not candidate_files:
            for identifier in ctx.scratch.get("mentioned_identifiers", []):
                for symbol in ctx.index.find_symbols(identifier.split(".")[-1])[:2]:
                    candidate_files.append(symbol.path)

        test_files: list[str] = []
        for path in dict.fromkeys(candidate_files):
            if path in ctx.index.files and _is_test(path):
                test_files.append(path)
            test_files.extend(ctx.graph.tests_for(path))

        for identifier in ctx.scratch.get("mentioned_identifiers", []):
            name = identifier.split(".")[-1]
            for symbol in ctx.index.symbols.values():
                if symbol.is_test and name.lower() in symbol.name.lower():
                    test_files.append(symbol.path)
                    targets.append(f"{symbol.path}::{symbol.name}")

        for path, lineno in [(f.path, f.lineno) for f in ctx.signal_frames()]:
            resolved = ctx.resolve_path(path)
            symbol = ctx.symbol_at(resolved, lineno) if resolved else None
            if symbol is not None:
                for test_symbol in ctx.index.symbols.values():
                    if test_symbol.is_test and symbol.name and symbol.name in " ".join(_called_names(test_symbol)):
                        targets.append(f"{test_symbol.path}::{test_symbol.name}")

        test_files = sorted(set(test_files))
        targets = list(dict.fromkeys(targets))[:6]

        if test_files:
            evidence = ctx.evidence(
                kind="test",
                claim=f"found {len(test_files)} relevant test file(s): {', '.join(test_files[:4])}",
                detail="Targeted verification is faster and less noisy than the full suite.",
                confidence=0.8,
                source="index + import graph",
                skill=self.name,
            )
            result.evidence.append(evidence)
            result.notes.append(f"targeted verification available for {len(test_files)} file(s)")
        else:
            evidence = ctx.evidence(
                kind="test",
                claim="no test file covers the implicated code",
                detail="Verification will fall back to a generated reproduction plus syntax/import checks.",
                confidence=0.7,
                source="index",
                skill=self.name,
            )
            result.evidence.append(evidence)

        if not base_command:
            result.notes.append("no test runner detected in this repository")

        result.outputs = {
            "base_command": base_command,
            "test_files": test_files[:20],
            "test_targets": targets,
            "has_tests": bool(test_files),
            "verification_plan": self._plan(base_command, targets, test_files),
        }
        return result

    @staticmethod
    def _fallback_command(ctx: SkillContext) -> list[str]:
        if any(f.language == "python" and _is_test(p) for p, f in ctx.index.files.items()):
            return ["python3", "-m", "unittest", "discover", "-v"]
        if "package.json" in ctx.index.files:
            return ["npm", "test", "--silent"]
        return []

    @staticmethod
    def _plan(base_command: list[str], targets: list[str], test_files: list[str]) -> list[str]:
        if not base_command:
            return []
        commands: list[str] = []
        if targets and base_command[:3] == ["python3", "-m", "pytest"]:
            commands.append(" ".join([*base_command, *targets]))
        elif test_files and base_command[:3] == ["python3", "-m", "pytest"]:
            commands.append(" ".join([*base_command, *test_files[:4]]))
        else:
            commands.append(" ".join(base_command))
        return commands


def _is_test(path: str) -> bool:
    from ..repo.symbols import is_test_path

    return is_test_path(path)


def _called_names(symbol) -> list[str]:
    return list(symbol.calls) + [symbol.name]


class ReproBuilder(Skill):
    name = "repro_builder"
    title = "Reproduction builder"
    description = "Creates an executable reproduction (or documented repro) so the fix can be verified without a test suite."
    priority = 65
    cost = "normal"
    trigger_failures = ("no-tests", "verification-failed")

    def applies(self, ctx: SkillContext) -> bool:
        return True

    def score(self, ctx: SkillContext) -> float:
        base = 0.3
        if not ctx.index.test_command and not ctx.index.stats.test_files:
            base += 0.4
        if any(s.kind == "frame" for s in ctx.signals):
            base += 0.2
        if "reproduce" in (ctx.text or "").lower() or "repro" in (ctx.text or "").lower():
            base += 0.15
        return min(1.0, base)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        frames = ctx.signal_frames()
        target = None
        for frame in reversed(frames):
            path = ctx.resolve_path(frame.path)
            if path and path in ctx.index.files and ctx.index.files[path].language == "python":
                target = (path, frame.lineno, frame.function)
                break
        command_from_log = self._command_from_log(ctx.text or "")
        if target is None and not command_from_log:
            result.notes.append("no reproduction could be derived automatically — the report has no resolvable frame or command")
            return result

        repro_dir = ctx.settings.data_dir / "repro"
        repro_dir.mkdir(parents=True, exist_ok=True)
        session_slug = ctx.session.id.replace("/", "-")
        lines: list[str] = [
            '"""Auto-generated reproduction for session {}."""'.format(ctx.session.id),
            "",
            "import sys",
            "",
            f"REPORT = {ctx.text[:1200]!r}",
            "",
        ]
        if command_from_log:
            lines += [
                "COMMAND_FROM_REPORT = " + repr(command_from_log),
                "",
                "",
                "def main() -> int:",
                "    print('Original failing command reported by the developer:')",
                "    print('   ', COMMAND_FROM_REPORT)",
                "    print('Re-run it inside the workspace to reproduce the failure:')",
                "    print('   ' + COMMAND_FROM_REPORT)",
                "    return 0",
            ]
        elif target is not None:
            path, lineno, function = target
            module = _module_path(path)
            lines += [
                f"TARGET = {path!r}",
                f"TARGET_LINE = {lineno}",
                f"TARGET_FUNCTION = {function!r}",
                "",
                "",
                "def show_context() -> int:",
                "    try:",
                f"        source = open({path!r}, encoding='utf-8').read().splitlines()",
                "    except OSError as exc:",
                "        print('cannot open target:', exc)",
                "        return 2",
                "    start = max(0, TARGET_LINE - 6)",
                "    end = min(len(source), TARGET_LINE + 5)",
                "    for number in range(start, end):",
                "        marker = '>>' if number + 1 == TARGET_LINE else '  '",
                "        print(f'{marker} {number + 1:4d} {source[number]}')",
                "    return 0",
                "",
                "",
                "def import_target() -> int:",
                f"    sys.path.insert(0, {str(ctx.settings.repo_root)!r})",
                "    try:",
                f"        __import__({module!r})",
                "    except Exception as exc:  # noqa: BLE001 - reproduction harness",
                "        print(f'import failed: {type(exc).__name__}: {exc}')",
                "        return 1",
                "    print('module imported cleanly')",
                "    return 0",
                "",
                "",
                "def main() -> int:",
                "    print('--- reported failure ---')",
                "    print(REPORT)",
                "    print('--- target context ---')",
                "    show_context()",
                "    print('--- import check ---')",
                "    code = import_target()",
                "    print('Reproduction harness finished (import status %d).' % code)",
                "    print('Next: turn the reported input into an assertion and keep it as a regression test.')",
                "    return 0",
            ]
        lines += ["", "", "if __name__ == '__main__':", "    raise SystemExit(main())", ""]
        script_path = repro_dir / f"repro-{session_slug}.py"
        try:
            atomic_write_text(script_path, "\n".join(lines))
        except OSError as exc:
            result.notes.append(f"could not write reproduction script: {exc}")
            return result

        evidence = ctx.evidence(
            kind="reproduction",
            claim=f"generated reproduction harness at {script_path.name}",
            detail="Used for verification when the repository has no tests covering the failing path.",
            path=str(script_path),
            confidence=0.5,
            source="repro_builder",
            skill=self.name,
        )
        result.evidence.append(evidence)
        result.outputs = {
            "repro_script": str(script_path),
            "repro_command": ["python3", str(script_path)],
            "command_from_report": command_from_log,
            "target": target[0] if target else "",
        }
        return result

    @staticmethod
    def _command_from_log(text: str) -> str:
        match = re.search(r"^\s*\$\s+(.+)$", text, re.M)
        if match:
            return match.group(1).strip()[:200]
        match = re.search(r"command (?:not found|failed)[:\s]+(.+)", text, re.I)
        if match:
            return match.group(1).strip()[:200]
        return ""


def _module_path(path: str) -> str:
    module = path[:-3] if path.endswith(".py") else path
    module = module.replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


class ReproHint(Skill):
    """Adds a written reproduction hint to the session for human review."""

    name = "repro_hint"
    title = "Manual reproduction guidance"
    description = "When automation cannot reproduce the bug, states precisely what a human should try next."
    priority = 30
    cost = "fast"
    always = True

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        steps: list[str] = []
        if ctx.index.test_command:
            steps.append("Run: " + " ".join(ctx.index.test_command))
        frames = [(ctx.resolve_path(f.path), f.lineno, f.function) for f in ctx.signal_frames()]
        frames = [f for f in frames if f[0]]
        if frames:
            path, lineno, function = frames[-1]
            steps.append(f"Reproduce by calling `{function or 'the failing function'}` in {path} (line {lineno}) with the reported input.")
        identifiers = ctx.scratch.get("mentioned_identifiers", [])
        if identifiers:
            steps.append("Focus inspection on: " + ", ".join(f"`{i}`" for i in identifiers[:5]))
        if not steps:
            steps.append("Attach the full error log or a stack-trace screenshot to give FixPilot a concrete entry point.")
        result.outputs = {"steps": steps}
        result.notes.append("reproduction guidance: " + excerpt(" ".join(steps), 200))
        return result
