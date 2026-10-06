"""Narration: turning machine state into something a developer can read on a phone.

Every step of the loop has a human-facing artefact:

* ``I found a bug``        -> :func:`explain_understanding`
* ``I understand why``     -> :func:`explain_cause`
* ``here is what I'll do``  -> :func:`explain_patch`
* ``I verified it``        -> :func:`explain_verification`
* the whole story          -> :func:`session_report` (exportable markdown)
* spoken summary           -> :func:`spoken_summary` (for text-to-speech)
"""

from __future__ import annotations

from ..memory.sessions import Session
from ..util import excerpt, human_bytes
from .verifier import PARTIAL, VERIFIED


def _bullets(items: list[str], marker: str = "-") -> str:
    return "\n".join(f"{marker} {item}" for item in items)


def _repo_line(session: Session) -> str:
    """`- **Repository:** path (branch @ head)` without dangling punctuation."""
    root = session.repo.get("root", "") or "unknown"
    branch = session.repo.get("branch", "") or ""
    head = (session.repo.get("head", "") or "")[:10]
    rail = " @ ".join(part for part in (branch, head) if part)
    return f"- **Repository:** {root}" + (f" ({rail})" if rail else "")


def explain_understanding(understanding: dict) -> str:
    lines = [f"**What I read:** {understanding.get('summary', '')}"]
    if understanding.get("error_types"):
        lines.append(f"**Error signal:** {', '.join(understanding['error_types'])}")
    if understanding.get("affected_files"):
        lines.append(f"**Files in the trace:** {', '.join(understanding['affected_files'][:4])}")
    if understanding.get("mentioned_identifiers"):
        lines.append(f"**You named:** {', '.join(understanding['mentioned_identifiers'][:5])}")
    if understanding.get("needs_clarification"):
        lines.append("**I still need:** " + "; ".join(understanding["needs_clarification"]))
    return "\n\n".join(lines)


def explain_cause(investigation) -> str:
    top = investigation.top()
    if top is None:
        return (
            "I could not ground a root cause in this repository. "
            "The most useful next step is a stack trace, the failing command, or the name of the function involved."
        )
    lines = [
        f"**Root cause:** {top.cause}",
        f"**Confidence:** {int(top.ranked_score * 100)}% ({top.category.replace('-', ' ')})",
    ]
    if top.explanation:
        lines.append(top.explanation)
    if investigation.evidence:
        lines.append("**Evidence I used:**")
        lines.append(
            _bullets(
                [
                    f"{e.claim}" + (f" — `{e.path}:{e.lineno}`" if e.path else "")
                    for e in sorted(investigation.evidence, key=lambda e: -e.confidence)[:5]
                ]
            )
        )
    alternatives = [h for h in investigation.hypotheses[1:4]]
    if alternatives:
        lines.append("**Other candidates I ranked lower:**")
        lines.append(
            _bullets([f"{h.cause} ({int(h.ranked_score * 100)}%)" for h in alternatives])
        )
    if top.falsifier:
        lines.append(f"**How this could be wrong:** {top.falsifier}")
    return "\n\n".join(lines)


def explain_patch(plan) -> str:
    if plan.parse_failed or not plan.files:
        return (
            "**No patch proposed.**\n\n"
            f"{plan.rationale}\n\n"
            "FixPilot deliberately stops rather than guessing at a change it cannot verify. "
            "You can ask me to explain the cause, or narrow the report and try again."
        )
    lines = [f"**Proposed change:** {plan.summary}"]
    lines.append(f"**Files:** {', '.join(plan.files)}  ·  **Diff:** {plan.patch_stats.get('additions', 0)} added / {plan.patch_stats.get('deletions', 0)} removed")
    if plan.rationale:
        lines.append(plan.rationale)
    if plan.candidates:
        lines.append("**Why this shape of fix:**")
        lines.append(_bullets([f"`{c.strategy}` — {c.reason}" for c in plan.candidates[:3]]))
    risk = plan.risk or {}
    lines.append(f"**Risk:** {risk.get('level', 'unknown')}")
    if risk.get("notes"):
        lines.append(_bullets([f"{note}" for note in risk["notes"]]))
    if plan.verification_plan:
        lines.append("**How I will verify it:**")
        lines.append(_bullets(plan.verification_plan))
    if risk.get("behaviour_change"):
        lines.append(
            "> This candidate changes behaviour for an edge case rather than only improving error reporting. "
            "If the tests disagree, I will try a fail-fast candidate instead."
        )
    return "\n\n".join(lines)


def explain_verification(verification: dict) -> str:
    status = verification.get("status", "unknown")
    lines = [f"**Verification: {status}** (confidence {int(float(verification.get('confidence', 0)) * 100)}%)"]
    tests = verification.get("tests") or {}
    if tests:
        lines.append(
            f"Tests: {tests.get('passed', 0)} passed · {tests.get('failed', 0)} failed · "
            f"{tests.get('errors', 0)} errors · {tests.get('skipped', 0)} skipped ({tests.get('framework', 'unknown')})"
        )
    syntax = verification.get("syntax") or {}
    if syntax:
        bad = [path for path, result in syntax.items() if not result.get("ok")]
        lines.append(f"Syntax: {'all changed files parse' if not bad else 'BROKEN in ' + ', '.join(bad)}")
    if verification.get("reasons"):
        lines.append(_bullets(verification["reasons"]))
    if tests.get("failures"):
        lines.append("**Failing tests:**")
        lines.append(_bullets([f"{f['name']} — {excerpt(f['message'], 100)}" for f in tests["failures"][:4]]))
    if verification.get("next_action"):
        lines.append(f"**Next:** {verification['next_action']}")
    return "\n\n".join(lines)


def explain_rollback(session: Session) -> str:
    files = session.rollback.get("files", [])
    if not files:
        return "Nothing to roll back — no change is applied."
    return (
        f"**Rolled back {len(files)} file(s):** {', '.join(files)}\n\n"
        "Your working tree is exactly as it was before the patch was applied. "
        "The session keeps the diff and the test output so nothing is lost."
    )


def session_report(session: Session) -> str:
    """Full markdown report for the session — the artefact you keep or paste in a PR."""
    d = session.to_dict()
    lines = [
        f"# FixPilot report — {session.title}",
        "",
        f"- **Session:** `{session.id}`",
        f"- **Status:** {d['status_label']}",
        f"- **Channel:** {session.channel}",
        _repo_line(session),
        f"- **Iterations:** {session.iterations}",
        f"- **Created:** {session.created_at}",
        "",
        "## 1. Report",
        "",
        "```",
        excerpt(session.input.get("text", ""), 1500),
        "```",
        "",
        "## 2. What FixPilot understood",
        "",
        explain_understanding(session.understanding),
        "",
        "## 3. Root cause",
        "",
        _cause_markdown(session),
        "",
        "## 4. Evidence",
        "",
    ]
    for evidence in session.evidence[:14]:
        where = f" · `{evidence.get('path')}:{evidence.get('lineno')}`" if evidence.get("path") else ""
        lines.append(f"- **[{evidence.get('kind')}]** {evidence.get('claim')}{where} _(conf {evidence.get('confidence')})_")
        if evidence.get("detail"):
            lines.append(f"  - {excerpt(evidence['detail'], 240)}")
    if not session.evidence:
        lines.append("- (no evidence recorded)")
    lines += ["", "## 5. Patch", ""]
    if session.patch.get("text"):
        lines.append(f"Summary: {session.patch.get('summary', '')}")
        lines.append("")
        lines.append("```diff")
        lines.append(session.patch["text"][:6000])
        lines.append("```")
    else:
        lines.append(session.patch.get("summary", "No patch was produced."))
    lines += ["", "## 6. Approval", ""]
    approval = session.approval or {}
    lines.append(
        f"- Decision: **{approval.get('decision', 'not requested')}**"
        + (f" by {approval.get('actor', 'user')}" if approval.get("actor") else "")
        + (f" at {approval.get('at')}" if approval.get("at") else "")
    )
    if approval.get("note"):
        lines.append(f"- Note: {approval['note']}")
    lines += ["", "## 7. Verification", ""]
    lines.append(explain_verification(session.verification) if session.verification else "Not verified yet.")
    if session.tests:
        lines += ["", "### Test runs", ""]
        for run in session.tests[-4:]:
            lines.append(
                f"- `{run.get('command', '')}` → exit {run.get('exit_code')} in {run.get('duration_ms', 0)}ms: {run.get('summary', '')}"
            )
    if session.rollback.get("files"):
        lines += ["", "## 8. Rollback", "", explain_rollback(session)]
    lines += ["", "---", "", "_Generated by FixPilot — every command above ran inside the local security sandbox._"]
    return "\n".join(lines)


def _cause_markdown(session: Session) -> str:
    top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
    if not top:
        return "No hypothesis was produced."
    out = [
        f"**{top.get('cause', '')}** — confidence {int(float(top.get('score') or top.get('confidence') or 0) * 100)}%",
        "",
        top.get("explanation", ""),
    ]
    if top.get("falsifier"):
        out += ["", f"_Falsifier: {top['falsifier']}_"]
    if len(session.hypotheses) > 1:
        out += ["", "Alternatives considered:"]
        out += [f"- {h.get('cause')} ({int(float(h.get('score') or 0) * 100)}%)" for h in session.hypotheses[1:4]]
    return "\n".join(out)


def spoken_summary(session: Session) -> str:
    """Short, speech-friendly status for the phone's voice interface."""
    status = session.status
    if status == "verified":
        tests = (session.verification or {}).get("tests") or {}
        return (
            f"Done. I found the root cause, applied a {session.patch.get('stats', {}).get('additions', 0)}-line patch to "
            f"{len(session.patch.get('files', []))} file, and the tests pass"
            + (f" — {tests.get('passed', 0)} passed, none failing." if tests else ".")
        )
    if status == "awaiting_approval":
        return (
            f"I have a fix ready for your review. {session.patch.get('summary', '')} "
            f"It touches {', '.join(session.patch.get('files', [])[:2])}. Approve it and I will run the tests."
        )
    if status == "rolled_back":
        return "The patch did not pass verification, so I rolled it back. Your code is unchanged."
    if status == "failed":
        return "I could not verify a fix for this one. The evidence and the failing output are in the session."
    if status in {"investigating", "hypothesis"}:
        top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
        return f"Still investigating. Most likely cause: {top.get('cause', 'not determined yet')}."
    return f"Session is in state: {status.replace('_', ' ')}."


def risk_badge(risk: dict) -> str:
    level = (risk or {}).get("level", "unknown")
    return {"low": "🟢 low risk", "medium": "🟡 medium risk", "high": "🔴 high risk"}.get(level, "⚪ unknown risk")


def file_size_note(path: str, size: int) -> str:
    return f"{path} ({human_bytes(size)})"
