"""Git-history forensics and flakiness triage."""

from __future__ import annotations

import re
from collections import Counter

from .base import Hypothesis, Skill, SkillContext, SkillResult
from ..util import excerpt


class GitForensics(Skill):
    name = "git_forensics"
    title = "Git history forensics"
    description = "Correlates the failure with blame, churn hotspots, revert smells and recent edits to implicated lines."
    priority = 75
    cost = "normal"
    always = True

    def applies(self, ctx: SkillContext) -> bool:
        return ctx.history.enabled

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        if not ctx.history.enabled:
            return result

        implicated: list[tuple[str, int]] = []
        for frame in ctx.signal_frames():
            path = ctx.resolve_path(frame.path)
            if path:
                implicated.append((path, frame.lineno))
        for signal in ctx.signals:
            if signal.kind in {"file_ref", "test_failure"}:
                path = ctx.resolve_path(signal.path)
                if path:
                    implicated.append((path, signal.lineno))
        if not implicated:
            for identifier in ctx.scratch.get("mentioned_identifiers", []):
                for symbol in ctx.index.find_symbols(identifier.split(".")[-1])[:2]:
                    implicated.append((symbol.path, symbol.lineno))

        if not implicated:
            result.notes.append("no implicated file to investigate in history")
            return result

        recent_commits = ctx.history.log(limit=40)
        file_churn = Counter()
        for commit in recent_commits:
            for path in ctx.history.changed_files(commit.sha)[:40]:
                file_churn[path] += 1

        for path, lineno in implicated[:4]:
            blame = ctx.history.evidence_for_line(path, lineno)
            if blame.get("blame"):
                blame_info = blame["blame"]
                evidence = ctx.evidence(
                    kind="history",
                    claim=(
                        f"{path}:{lineno} was last changed {blame_info['date']} by {blame_info['author']} "
                        f"in {blame_info['sha']} — “{excerpt(blame_info['summary'], 90)}”"
                    ),
                    detail="Recent edits to the exact failing line are the strongest regression signal available without running the code.",
                    path=path,
                    lineno=lineno,
                    confidence=0.62,
                    source="git blame",
                    skill=self.name,
                )
                result.evidence.append(evidence)
                result.hypotheses.append(
                    Hypothesis(
                        cause=f"regression introduced by commit {blame_info['sha']} touching {path}:{lineno}",
                        category="regression",
                        explanation=(
                            f"The failing line was last modified {blame_info['date']} in “{excerpt(blame_info['summary'], 80)}”. "
                            "If the failure started around then, the change is the most likely cause."
                        ),
                        confidence=0.55,
                        evidence_ids=[evidence.id],
                        file=path,
                        lineno=lineno,
                        strategy="restore-intent",
                        fix_plan=f"Compare the current line against {blame_info['sha']}^ and make the intended behaviour explicit instead of reverting blindly.",
                        verification_plan="run the tests that cover this section and confirm they passed before that commit",
                        falsifier=f"if the failure predates {blame_info['date']}, this is not a regression",
                        skill=self.name,
                    )
                )
            commits = ctx.history.commits_touching(path, limit=4)
            if commits:
                churn = file_churn.get(path, 0)
                confidence = 0.35 + min(0.25, churn / 40)
                evidence = ctx.evidence(
                    kind="history",
                    claim=f"{path} changed in {churn} of the last {len(recent_commits)} commits",
                    snippet="\n".join(f"{c.date} {c.sha[:8]} {c.subject}" for c in commits),
                    path=path,
                    confidence=confidence,
                    source="git log",
                    skill=self.name,
                )
                result.evidence.append(evidence)
                result.outputs.setdefault("churn", {})[path] = churn

        if recent_commits:
            result.outputs["head"] = recent_commits[0].sha[:10]
            result.outputs["recent_subjects"] = [c.subject[:80] for c in recent_commits[:5]]

        regressions = ctx.history.recent_regressions(limit=4)
        if regressions:
            evidence = ctx.evidence(
                kind="history",
                claim=f"{len(regressions)} recent commit(s) mention reverts/hotfixes — the area may be unstable",
                snippet="\n".join(f"{c.date} {c.sha[:8]} {c.subject}" for c in regressions),
                confidence=0.4,
                source="git log regex",
                skill=self.name,
            )
            result.evidence.append(evidence)
            result.notes.append("history contains revert/hotfix language; treat nearby code as recently churned")

        # Pickaxe: find when a constant or identifier from the failure first appeared.
        for token in self._interesting_tokens(ctx)[:3]:
            commits = ctx.history.pickaxe(token, limit=3)
            if commits:
                result.outputs.setdefault("pickaxe", {})[token] = [
                    {"sha": c.sha[:8], "date": c.date, "subject": excerpt(c.subject, 70)} for c in commits
                ]
        return result

    @staticmethod
    def _interesting_tokens(ctx: SkillContext) -> list[str]:
        found: list[str] = []
        for match in re.finditer(r"[\"']([A-Za-z_][\w.\-/]{3,40})[\"']", ctx.text or ""):
            found.append(match.group(1))
        for signal in ctx.signals:
            if signal.kind == "error" and signal.message:
                for match in re.finditer(r"['\"]([\w .\-/]{3,40})['\"]", signal.message):
                    found.append(match.group(1))
        return list(dict.fromkeys(found))


class FlakyDetector(Skill):
    name = "flaky_detector"
    title = "Flakiness triage"
    description = "Separates deterministic defects from timing/ordering instability and proposes a stabilisation plan."
    priority = 55
    cost = "fast"
    STRONG = re.compile(r"\b(flaky|flake|intermittent|sometimes fails|nondeterministic|non-deterministic|race condition|works locally|passes on retry|random order)\b", re.I)

    def applies(self, ctx: SkillContext) -> bool:
        text = ctx.text or ""
        if self.STRONG.search(text):
            return True
        # Two independent weak markers, one of which must be a timing construct.
        weak_timing = re.search(r"time\.sleep|setTimeout|Thread\.sleep|\bsleep\(", text)
        weak_other = re.search(r"\b(thread|async|promise|await|concurrent|parallel|lock|mutex|retry|deadlock)\b", text, re.I)
        return bool(weak_timing and weak_other)

    def run(self, ctx: SkillContext) -> SkillResult:
        result = SkillResult(skill=self.name)
        text = (ctx.text or "").lower()
        markers = {
            "sleep": bool(re.search(r"time\.sleep|setTimeout|Thread\.sleep|sleep\(", ctx.text or "")),
            "concurrency": bool(re.search(r"thread|async|promise|await|concurrent|parallel|lock|mutex", ctx.text or "", re.I)),
            "retry-passes": bool(re.search(r"passes on retry|flaky|intermittent", text)),
            "explicit-flake-word": bool(self.STRONG.search(text)),
        }
        test_signals = [s for s in ctx.signals if s.kind in {"test_failure", "assertion"}]
        if not markers["explicit-flake-word"] and not (markers["concurrency"] and (markers["sleep"] or markers["retry-passes"])):
            return result
        evidence = ctx.evidence(
            kind="pattern",
            claim="failure shows signs of timing/ordering sensitivity: " + ", ".join(k for k, v in markers.items() if v),
            detail="Flaky failures are not fixed by changing logic; they need deterministic synchronisation or isolation.",
            confidence=0.5,
            source="heuristics over the report",
            skill=self.name,
        )
        result.evidence.append(evidence)
        result.hypotheses.append(
            Hypothesis(
                cause="shared state or unsynchronised timing makes this test order-dependent",
                category="concurrency",
                explanation=(
                    "When a failure appears intermittently and depends on ordering, the root cause is usually a shared fixture, "
                    "a fixed sleep, or a background task that outlives its test."
                ),
                confidence=0.45,
                evidence_ids=[evidence.id],
                file=test_signals[0].path if test_signals and test_signals[0].path else "",
                strategy="stabilise-timing",
                fix_plan="Make the wait condition explicit (poll for the state, don't sleep), isolate shared fixtures per test, and reset module-level caches.",
                verification_plan="run the affected test 5 times in a loop and require 5/5 passes",
                falsifier="if the test fails reproducibly with the same input, treat it as a deterministic bug instead",
                skill=self.name,
            )
        )
        result.outputs["markers"] = {k: v for k, v in markers.items() if v}
        return result
