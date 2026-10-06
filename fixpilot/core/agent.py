"""The FixPilot agent: session orchestration from report to verified patch.

State machine (see :mod:`fixpilot.memory.sessions`)::

    intake -> investigating -> hypothesis -> patch_proposed -> awaiting_approval
           -> applying -> verifying -> verified
                         \\-> refining (up to N attempts) -> patch_proposed
                         \\-> failed -> rolled_back

Two principles shape this file:

1. **Nothing is written without a reviewable diff and an approval** (unless the
   operator explicitly disabled approval).
2. **Nothing stays written unless verification passes.**  If refinement runs out
   of attempts, the agent rolls the working tree back automatically.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import Settings
from ..memory.lessons import LessonStore
from ..memory.project import ProjectMemoryStore
from ..memory.sessions import (
    APPLYING,
    AWAITING_APPROVAL,
    CANCELLED,
    FAILED,
    HYPOTHESIS,
    INTAKE,
    INVESTIGATING,
    PATCH_PROPOSED,
    REFINING,
    REJECTED,
    ROLLED_BACK,
    Session,
    SessionStore,
    VERIFIED,
    VERIFYING,
)
from ..models.media import IngestedInput, InputAdapter, summary_of
from ..models.providers import build_providers
from ..models.router import ModelRouter
from ..repo.githistory import GitHistory
from ..repo.graph import CodeGraph
from ..repo.indexer import Indexer, RepoIndex
from ..security.executor import SandboxExecutor
from ..security.patch import (
    ApplyResult,
    FileProposal,
    ParsedPatch,
    apply_patch,
    diff_stats,
    parse_unified,
    proposals_to_patch,
    revert_patch,
    validate_patch,
)
from ..security.policy import CommandPolicy
from ..security.secrets import SecretScanner
from ..skills import SkillRegistry, get_registry
from ..store import AuditLog, append_jsonl, read_json, write_json
from ..util import excerpt, new_id, now_iso, rel_path
from .engine import FixPlanner, Investigation, Investigator
from .fixes import FixCandidate
from .narrator import (
    explain_cause,
    explain_patch,
    explain_understanding,
    explain_verification,
    session_report,
    spoken_summary,
)
from .verifier import PARTIAL, VERIFIED as V_VERIFIED, Verifier


class FixPilotAgent:
    """Everything the API, the CLI and the phone talk to."""

    def __init__(self, settings: Settings, registry: SkillRegistry | None = None) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.audit = AuditLog(settings.audit_log)
        self.policy = CommandPolicy(settings)
        self.executor = SandboxExecutor(settings, self.policy, self.audit)
        self.router = ModelRouter(settings, build_providers(settings), self.audit)
        self.sessions = SessionStore(settings)
        self.memory = ProjectMemoryStore(settings)
        self.lessons = LessonStore(settings)
        self.adapter = InputAdapter(settings, self.audit)
        self.verifier = Verifier(settings, self.executor)
        self.scanner = SecretScanner(block_writes=True)
        self.registry = registry or get_registry()
        self._index: RepoIndex | None = None
        self._graph: CodeGraph | None = None
        self._history: GitHistory | None = None

    # ------------------------------------------------------------------
    # Repo state
    # ------------------------------------------------------------------
    @property
    def index(self) -> RepoIndex:
        if self._index is None:
            self._index = Indexer(self.settings).build()
            self.memory.learn_from_index(self._index)
        return self._index

    @property
    def graph(self) -> CodeGraph:
        if self._graph is None:
            self._graph = CodeGraph(self.index)
        return self._graph

    @property
    def history(self) -> GitHistory:
        if self._history is None:
            self._history = GitHistory(self.settings.repo_root)
        return self._history

    def refresh_index(self, force: bool = True) -> dict:
        self._index = Indexer(self.settings).build(force=force)
        self._graph = CodeGraph(self._index)
        self.memory.learn_from_index(self._index)
        return self._index.summary()

    def overview(self) -> dict:
        index = self.index
        memory = self.memory.load()
        return {
            "product": {"name": "FixPilot", "version": _version(), "tagline": "Bug reports in. Verified patches out. From your phone."},
            "repo": {
                **index.summary(),
                "dirty": self.history.is_dirty() if index.is_git else False,
            },
            "git": self.history.describe(),
            "memory": memory.summary(),
            "lessons": self.lessons.summary(),
            "models": self.router.describe(),
            "skills": self.registry.summary(),
            "sandbox": self.executor.stats(),
            "settings": self.settings.public(),
        }

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def ingest(self, payload: dict) -> IngestedInput:
        """Normalise phone input (text / voice / log / screenshots) into one object."""
        parts: list[IngestedInput] = []
        text = (payload.get("text") or "").strip()
        transcript = (payload.get("transcript") or "").strip()
        channel = payload.get("channel") or ("voice" if transcript else "text")
        if text:
            parts.append(self.adapter.from_text(text, language=payload.get("language", "en")))
        if transcript:
            parts.append(
                self.adapter.from_voice(
                    transcript,
                    language=payload.get("language", "en"),
                    confidence=float(payload.get("confidence") or 0.8),
                )
            )
        for attachment in payload.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            parts.append(
                self.adapter.from_image(
                    filename=attachment.get("filename", "screenshot.png"),
                    data_url_or_base64=attachment.get("data") or attachment.get("dataUrl") or "",
                    ocr_text=attachment.get("ocr_text") or attachment.get("ocrText") or "",
                    user_note=payload.get("text", ""),
                    language=payload.get("language", "en"),
                )
            )
        if not parts:
            parts.append(self.adapter.from_text(payload.get("command") or "", language=payload.get("language", "en")))
        ingested = self.adapter.merge(parts, hint=payload.get("hint", "")) if len(parts) > 1 else parts[0]
        if channel and ingested.channel == "text" and channel in {"voice", "log", "image"}:
            ingested.channel = channel
        return ingested

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    def start(self, payload: dict) -> dict:
        ingested = self.ingest(payload)
        index = self.index
        session = self.sessions.create(
            title=_title_for(ingested),
            channel=ingested.channel,
            repo={
                "root": str(self.settings.repo_root),
                "branch": index.git_branch,
                "head": index.git_head,
                "dirty": self.history.is_dirty() if index.is_git else False,
                "files_indexed": index.stats.files,
                "languages": index.stats.languages,
            },
            input_payload=ingested.to_dict(),
        )
        self._emit(session, "input.received", {"channel": ingested.channel, "chars": len(ingested.text), "signals": len(ingested.signals)})
        investigator = Investigator(
            self.settings,
            index=index,
            graph=self.graph,
            history=self.history,
            executor=self.executor,
            router=self.router,
            memory=self.memory,
            lessons=self.lessons,
            registry=self.registry,
        )
        understanding = investigator.understand(ingested, session)
        self.sessions.update(session, understanding=understanding)
        self._emit(session, "input.understood", {"summary": understanding["summary"], "intent": understanding["intent"]})
        self.sessions.set_status(session, INVESTIGATING, note="evidence gathering")
        self._save_ingested(session.id, ingested)
        return session.to_dict()

    def investigate(self, session_id: str, *, cost_budget: str = "deep", propose: bool = True) -> dict:
        session = self._require(session_id)
        ingested = self._load_ingested(session_id) or self.adapter.from_text(session.input.get("text", ""))
        investigator = Investigator(
            self.settings,
            index=self.index,
            graph=self.graph,
            history=self.history,
            executor=self.executor,
            router=self.router,
            memory=self.memory,
            lessons=self.lessons,
            registry=self.registry,
        )
        investigation = investigator.investigate(
            session,
            ingested,
            cost_budget=cost_budget,
            emit=lambda kind, payload: self._emit(session, kind, payload),
        )
        self.sessions.update(
            session,
            evidence=investigation.evidence_dicts()[:40],
            hypotheses=investigation.hypothesis_dicts()[:8],
            lessons_used=investigation.lessons_used,
            plan=investigation.plan,
            metrics={"investigation_ms": investigation.duration_ms, "skills_run": investigation.skills_run},
        )
        self.sessions.set_status(session, HYPOTHESIS, note=f"{len(investigation.hypotheses)} hypotheses ranked")
        proposal: dict = {}
        if propose:
            proposal = self._propose(session, investigation)
        return {
            "session": session.to_dict(),
            "investigation": _investigation_dict(investigation),
            "plan": proposal.get("plan", {}),
            "awaiting_approval": proposal.get("awaiting_approval", False),
        }

    def _propose(self, session: Session, investigation: Investigation) -> dict:
        planner = FixPlanner(self.settings, index=self.index, graph=self.graph, registry=self.registry)
        plan = planner.plan(investigation, candidates=investigation.patch_candidates or None)
        self._save_candidates(session.id, plan.candidates)
        patch_payload = plan.to_dict()
        self.sessions.update(
            session,
            patch={
                "text": plan.patch_text,
                "summary": plan.summary,
                "rationale": plan.rationale,
                "files": plan.files,
                "stats": plan.patch_stats,
                "risk": plan.risk,
                "strategies": plan.strategies_tried,
                "verification_plan": plan.verification_plan,
                "test_files": plan.test_files,
                "test_targets": plan.test_targets,
                "requires_review": plan.requires_review,
            },
            iterations=0,
        )
        if plan.parse_failed or not plan.patch_text:
            # No candidate survived validation. Say so plainly instead of asking
            # the developer to approve an empty patch. (Note: this must not be
            # routed through patch_proposed, which cannot transition to failed.)
            self._emit(
                session,
                "patch.abandoned",
                {
                    "reason": plan.rationale or "no candidate patch survived validation",
                    "strategies": plan.strategies_tried,
                    "validation": plan.validation[:5],
                },
            )
            self.sessions.set_status(session, FAILED, note="no safe patch could be produced")
            self._record_lesson(session, investigation, outcome="failed", root_cause=plan.rationale)
            return {"plan": patch_payload, "awaiting_approval": False}
        try:
            (self.sessions.patch_path(session.id)).write_text(plan.patch_text, encoding="utf-8")
        except OSError:
            pass
        self.sessions.set_status(session, PATCH_PROPOSED, note=f"{len(plan.files)} file(s) proposed")
        self._emit(
            session,
            "patch.proposed",
            {
                "files": len(plan.files),
                "additions": plan.patch_stats.get("additions", 0),
                "deletions": plan.patch_stats.get("deletions", 0),
                "risk": plan.risk.get("level"),
                "strategies": plan.strategies_tried,
                "explanation": explain_patch(plan),
                "parse_failed": plan.parse_failed,
            },
        )
        if self.settings.require_approval:
            self.sessions.set_status(session, AWAITING_APPROVAL, note="developer approval required")
            self.sessions.update(session, approval={"required": True, "decision": "pending", "requested_at": now_iso()})
            self._emit(session, "approval.requested", {"files": plan.files, "risk": plan.risk.get("level"), "summary": plan.summary})
        else:
            self.sessions.update(session, approval={"required": False, "decision": "auto-approved", "at": now_iso(), "note": "approval gate disabled by configuration"})
            self._emit(session, "approval.granted", {"note": "auto-approved by configuration"})
        return {"plan": patch_payload, "awaiting_approval": bool(self.settings.require_approval)}

    def run(self, payload: dict, *, cost_budget: str = "deep") -> dict:
        """Full front half of the loop: ingest -> investigate -> propose."""
        started = time.perf_counter()
        session = Session.from_dict(self.start(payload))
        result = self.investigate(session.id, cost_budget=cost_budget, propose=True)
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result["session"] = self._require(session.id).to_dict()
        return result

    # ------------------------------------------------------------------
    # Approval / apply / verify
    # ------------------------------------------------------------------
    def approve(self, session_id: str, *, approved: bool, note: str = "", actor: str = "user") -> dict:
        session = self._require(session_id)
        if session.status not in {AWAITING_APPROVAL, PATCH_PROPOSED, REJECTED}:
            return {"ok": False, "error": f"session is {session.status}; nothing is awaiting approval"}
        session.approval = {
            "required": True,
            "decision": "approved" if approved else "rejected",
            "note": note,
            "actor": actor,
            "at": now_iso(),
        }
        self.sessions.save(session)
        if not approved:
            self.sessions.set_status(session, REJECTED, note=note or "developer rejected the patch")
            self._emit(session, "approval.rejected", {"note": note, "actor": actor}, actor=actor)
            return {"ok": True, "session": session.to_dict(), "applied": False}
        self._emit(session, "approval.granted", {"note": note, "actor": actor}, actor=actor)
        return {"ok": True, "session": session.to_dict(), "applied": False}

    def apply_and_verify(self, session_id: str, *, auto_rollback: bool = True) -> dict:
        """Apply the approved patch, run the tests, refine on failure, roll back if needed."""
        session = self._require(session_id)
        if session.status not in {AWAITING_APPROVAL, PATCH_PROPOSED, REJECTED, FAILED, REFINING}:
            return {
                "ok": False,
                "error": f"session is {session.status}; cannot apply",
                "session": session.to_dict(),
            }
        if self.settings.require_approval and session.approval.get("decision") not in {"approved", "auto-approved"}:
            return {"ok": False, "error": "patch is not approved", "session": session.to_dict()}

        candidates = self._load_candidates(session_id)
        if not candidates:
            self.sessions.set_status(session, FAILED, note="no candidates available to apply")
            return {"ok": False, "error": "no patch candidates were stored for this session", "session": session.to_dict()}

        self.sessions.set_status(session, APPLYING, note=f"applying candidate 1/{len(candidates)}")
        attempts: list[dict] = []
        max_attempts = max(1, min(len(candidates), self.settings.max_refine_iterations + 1))
        failure_context = ""
        baseline_backup: dict[str, str] = {}
        applied_files: list[str] = []
        last_verification: dict = {}

        for attempt in range(1, max_attempts + 1):
            candidate = candidates[attempt - 1]
            if attempt > 1:
                self.sessions.set_status(session, REFINING, note=f"attempt {attempt}: {candidate['strategy']}")
                self._emit(
                    session,
                    "patch.refined",
                    {
                        "attempt": attempt,
                        "strategy": candidate.get("strategy"),
                        "reason": candidate.get("reason"),
                        "previous_failure": excerpt(failure_context, 300),
                    },
                )
                session.iterations = attempt - 1
            proposals = [
                FileProposal(
                    path=candidate["path"],
                    old_content=candidate.get("old_content", ""),
                    new_content=candidate["new_content"],
                    reason=candidate.get("reason", ""),
                    strategy=candidate.get("strategy", ""),
                    confidence=float(candidate.get("confidence", 0.5)),
                )
            ]
            patch_text, parsed = proposals_to_patch(proposals)
            if not parsed.files:
                attempts.append({"attempt": attempt, "applied": False, "error": "candidate produced an empty diff"})
                continue
            violations = validate_patch(parsed, self.settings.repo_root, self.settings, allow_new_files=False)
            if violations:
                attempts.append({"attempt": attempt, "applied": False, "error": "; ".join(violations)})
                self._emit(session, "patch.rejected_by_policy", {"attempt": attempt, "violations": violations})
                continue

            backup_dir = self.sessions.backup_dir(session.id) / f"attempt-{attempt}"
            apply_result = apply_patch(
                self.settings.repo_root,
                parsed,
                self.settings,
                dry_run=False,
                backup_dir=backup_dir,
            )
            if not apply_result.ok:
                attempts.append({"attempt": attempt, "applied": False, "error": apply_result.error, "failed": apply_result.failed})
                failure_context = apply_result.error or ""
                continue

            baseline_backup = dict(apply_result.backup)
            applied_files = parsed.touched_paths()
            session.patch.update(
                {
                    "text": patch_text,
                    "applied_at": now_iso(),
                    "files": applied_files,
                    "stats": diff_stats(parsed),
                    "strategy_applied": candidate.get("strategy"),
                    "attempt": attempt,
                }
            )
            self.sessions.save(session)
            self._emit(
                session,
                "patch.applied",
                {
                    "files": applied_files,
                    "strategy": candidate.get("strategy"),
                    "attempt": attempt,
                    "stats": diff_stats(parsed),
                },
            )

            verification = self._verify(session, applied_files)
            last_verification = verification.to_dict()
            attempts.append(
                {
                    "attempt": attempt,
                    "applied": True,
                    "strategy": candidate.get("strategy"),
                    "verification": verification.status,
                    "files": applied_files,
                }
            )
            if verification.status == V_VERIFIED:
                self.sessions.set_status(session, VERIFIED, note="tests pass on the patched tree")
                self.sessions.update(session, verification=last_verification, iterations=attempt - 1)
                self._record_lesson(session, None, outcome="verified")
                return {
                    "ok": True,
                    "verified": True,
                    "session": session.to_dict(),
                    "attempts": attempts,
                    "explanation": explain_verification(last_verification),
                }

            failure_context = _failure_context(verification)
            if attempt < max_attempts:
                self._restore(baseline_backup)
                self.sessions.update(session, verification=last_verification)
                continue
            break

        # Exhausted attempts.
        self.sessions.update(session, verification=last_verification, iterations=max(0, len(attempts) - 1))
        if auto_rollback and baseline_backup:
            rollback = self._rollback_files(session, baseline_backup)
            self.sessions.set_status(session, ROLLED_BACK, note="verification failed and refinement was exhausted")
            self._emit(
                session,
                "rollback.completed",
                {"files": list(baseline_backup.keys()), "reason": "verification failed after refinement", **rollback},
            )
            self._record_lesson(session, None, outcome="rolled_back")
            return {
                "ok": False,
                "verified": False,
                "rolled_back": True,
                "session": session.to_dict(),
                "attempts": attempts,
                "explanation": explain_verification(last_verification)
                + "\n\nThe change has been rolled back — your working tree is unchanged.",
            }
        self.sessions.set_status(session, FAILED, note="verification failed and the patch was left in place")
        self._record_lesson(session, None, outcome="failed")
        return {
            "ok": False,
            "verified": False,
            "rolled_back": False,
            "session": session.to_dict(),
            "attempts": attempts,
            "explanation": explain_verification(last_verification),
        }

    def _verify(self, session: Session, changed_files: list[str]):
        self.sessions.set_status(session, VERIFYING, note=f"running tests for {', '.join(changed_files[:3])}")
        repro = ""
        stored = self._load_scratch(session.id)
        if stored:
            repro = stored.get("repro_script", "")
        outcome = self.verifier.verify(
            self.index,
            changed_files=changed_files,
            test_targets=_targets_from_session(session),
            test_files=[path for path in (session.patch.get("test_files") or []) if path],
            repro_script=repro,
        )
        payload = outcome.to_dict()
        session.tests = list(session.tests) + payload["runs"][-2:]
        self.sessions.update(session, tests=session.tests, verification=payload)
        for run in payload["runs"]:
            self._emit(
                session,
                "tests.completed",
                {
                    "kind": run.get("kind"),
                    "command": run.get("command"),
                    "exit_code": run.get("exit_code"),
                    "duration_ms": run.get("duration_ms"),
                    "summary": run.get("summary"),
                    "passed": (payload.get("tests") or {}).get("passed", 0),
                    "failed": (payload.get("tests") or {}).get("failed", 0),
                },
            )
        self._emit(
            session,
            "verification.completed",
            {"status": payload["status"], "confidence": payload["confidence"], "reasons": payload["reasons"]},
        )
        return outcome

    def _targets(self, session: Session) -> list[str]:
        return _targets_from_session(session)

    def refine(self, session_id: str, *, instruction: str = "") -> dict:
        """Ask for a new candidate patch using the failure output as evidence."""
        session = self._require(session_id)
        failure = _failure_context_from_session(session)
        candidates = self._load_candidates(session_id)
        previous = candidates[session.iterations] if session.iterations < len(candidates) else (candidates[-1] if candidates else None)
        refined = self._request_refined_patch(session, failure_text=failure, instruction=instruction, previous=previous)
        if refined:
            candidates.extend(refined)
            self._save_candidates(session_id, candidates)
        self.sessions.set_status(session, REFINING, note=f"{len(refined)} refined candidate(s) from failure output")
        self._emit(session, "patch.refined", {"attempt": session.iterations + 1, "candidates": len(refined), "reason": excerpt(failure, 240)})
        return {"ok": bool(refined), "candidates_added": len(refined), "session": session.to_dict()}

    def rollback(self, session_id: str, *, note: str = "manual rollback from the phone") -> dict:
        session = self._require(session_id)
        backups = self._latest_backup(session)
        if not backups:
            return {"ok": False, "error": "no backup snapshot is available for this session"}
        result = self._rollback_files(session, backups, note=note)
        self.sessions.set_status(session, ROLLED_BACK, note=note)
        self._emit(session, "rollback.completed", {"files": list(backups.keys()), "note": note, **result})
        return {"ok": True, "session": session.to_dict(), **result, "explanation": explain_verification(session.verification or {})}

    def cancel(self, session_id: str) -> dict:
        session = self._require(session_id)
        self.sessions.set_status(session, CANCELLED, note="cancelled from the phone")
        return {"ok": True, "session": session.to_dict()}

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def status(self, session_id: str) -> dict:
        session = self._require(session_id)
        return {
            "session": session.to_dict(),
            "timeline": self.sessions.timeline(session_id),
            "spoken": spoken_summary(session),
            "counts": {
                "evidence": len(session.evidence),
                "hypotheses": len(session.hypotheses),
                "test_runs": len(session.tests),
            },
        }

    def explain(self, session_id: str) -> dict:
        session = self._require(session_id)
        plan = session.patch
        return {
            "session_id": session_id,
            "status": session.status,
            "understanding": explain_understanding(session.understanding),
            "cause": _cause_markdown(session),
            "patch": _patch_markdown(session),
            "verification": explain_verification(session.verification) if session.verification else "Not verified yet.",
            "spoken": spoken_summary(session),
            "risk": plan.get("risk", {}),
        }

    def report(self, session_id: str) -> dict:
        session = self._require(session_id)
        markdown = session_report(session)
        path = self.sessions.dir_for(session_id) / "report.md"
        try:
            path.write_text(markdown, encoding="utf-8")
        except OSError:
            pass
        return {"session_id": session_id, "markdown": markdown, "path": str(path)}

    def replay(self, session_id: str, *, since_seq: int = 0) -> dict:
        return self.sessions.replay(session_id, since_seq=since_seq)

    def list_sessions(self, limit: int = 30) -> list[dict]:
        return self.sessions.list(limit=limit)

    def answer(self, question: str) -> dict:
        """Repository Q&A for the phone: grounded, with citations."""
        index = self.index
        question = (question or "").strip()
        lowered = question.lower()
        citations: list[dict] = []
        answer_parts: list[str] = []

        if not question:
            return {"answer": "Ask me anything about this repository — where something is defined, how to run it, or what changed recently.", "citations": []}

        if any(phrase in lowered for phrase in ("what is this project", "what does this project", "overview", "architecture", "what does this repo")):
            memory = self.memory.load()
            answer_parts.append(memory.brief(limit=8))
            if index.stats.languages:
                answer_parts.append(f"{index.stats.files} files indexed, {index.stats.symbols} symbols, {index.stats.test_files} test files.")
            citations.extend({"path": item["path"], "lineno": 1, "snippet": ""} for item in memory.key_files[:3])

        if any(phrase in lowered for phrase in ("how do i run", "test command", "run the tests", "how to test")):
            command = " ".join(index.test_command) if index.test_command else "no test command detected"
            answer_parts.append(f"Test command: `{command}`")
            if index.build_commands:
                answer_parts.append("Build commands: " + ", ".join(f"`{c}`" for c in index.build_commands))

        if any(phrase in lowered for phrase in ("what changed", "recent commits", "git log", "recently")):
            commits = self.history.log(limit=6)
            if commits:
                answer_parts.append("Recent commits:")
                answer_parts.append("\n".join(f"- {c.date} `{c.sha[:8]}` {c.subject}" for c in commits))
                citations.append({"path": "", "lineno": 0, "snippet": commits[0].subject})

        # Symbol / code search for the remainder of the question.
        identifiers = [word.strip("`'\"?.,()") for word in question.split() if len(word) > 3]
        symbol_hits = []
        for identifier in identifiers[:6]:
            symbol_hits.extend(index.find_symbols(identifier, exact=False)[:3])
        symbol_hits = symbol_hits[:6]
        if symbol_hits and not answer_parts:
            answer_parts.append("Most relevant definitions:")
            for symbol in symbol_hits:
                answer_parts.append(f"- `{symbol.name}` ({symbol.kind}) in `{symbol.path}:{symbol.lineno}` — {excerpt(symbol.doc or symbol.signature, 120)}")
                citations.append({"path": symbol.path, "lineno": symbol.lineno, "snippet": symbol.signature or symbol.name})

        code_hits = index.search_code(question, limit=4)
        if code_hits and len(answer_parts) < 3:
            answer_parts.append("Matching code:")
            for hit in code_hits:
                answer_parts.append(f"- `{hit['path']}:{hit['lineno']}` — {excerpt(hit['line'], 140)}")
                citations.append(hit)

        lessons = self.lessons.find_similar(text=question, limit=2, min_score=0.22)
        if lessons:
            answer_parts.append("Related debugging history:")
            for match in lessons:
                answer_parts.append(f"- {excerpt(match.lesson.symptom, 120)} → {excerpt(match.lesson.root_cause, 160)} (outcome: {match.lesson.outcome})")

        if not answer_parts:
            # Nothing local matched — one grounded model pass over the index summary.
            profile = self.router.route("explain").to_dict()
            completion, decision = self.router.call(
                "explain",
                "\n".join(
                    [
                        "Answer the developer's question about this repository using only the context below.",
                        "If the context is insufficient, say what is missing. Be concise (max 120 words).",
                        "",
                        self.memory.load().brief(limit=6),
                        "",
                        f"Question: {question}",
                    ]
                ),
                system="You are FixPilot answering a developer from their phone.",
                max_tokens=400,
            )
            if completion.ok:
                answer_parts.append(completion.text)
                answer_parts.append(f"_(answered by {decision.profile_key})_")
            else:
                answer_parts.append(
                    "I could not find that in this repository's index, and no local model was reachable to reason about it. "
                    "Try naming a file, function or error message."
                )
        return {"answer": "\n\n".join(answer_parts), "citations": citations[:8], "question": question}

    def search(self, query: str, limit: int = 20) -> dict:
        return {
            "symbols": [s.to_dict() for s in self.index.search_symbols(query, limit=limit)],
            "code": self.index.search_code(query, limit=limit),
            "files": [path for path in self.index.files if query.lower() in path.lower()][:limit],
        }

    def diff_preview(self, session_id: str) -> dict:
        session = self._require(session_id)
        return {"patch": session.patch.get("text", ""), "stats": session.patch.get("stats", {}), "files": session.patch.get("files", [])}

    def working_tree_diff(self) -> dict:
        diff = self.history.current_diff()
        return {"diff": diff[:60_000], "dirty": bool(diff.strip())}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _emit(self, session: Session, kind: str, payload: dict, *, actor: str = "agent") -> None:
        self.sessions.log(session.id, kind, payload, actor=actor)

    def _require(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id}")
        return session

    # -- ingested payload persistence (keeps session.json small) --------
    def _save_ingested(self, session_id: str, ingested: IngestedInput) -> None:
        write_json(self.sessions.dir_for(session_id) / "input.json", ingested.to_dict())

    def _load_ingested(self, session_id: str) -> IngestedInput | None:
        payload = read_json(self.sessions.dir_for(session_id) / "input.json")
        if not payload:
            return None
        ingested = IngestedInput(
            id=payload.get("id", new_id("in")),
            channel=payload.get("channel", "text"),
            text=payload.get("text", ""),
            transcript=payload.get("transcript", ""),
            language=payload.get("language", "en"),
            attachments=payload.get("attachments", []),
            hint_intent=payload.get("hint_intent", ""),
            confidence=float(payload.get("confidence", 0.6)),
            created_at=payload.get("created_at", now_iso()),
            warnings=payload.get("warnings", []),
        )
        from ..models.media import LogSignal

        ingested.signals = [LogSignal(**{k: v for k, v in item.items() if k in LogSignal.__slots__}) for item in payload.get("signals", [])]
        return ingested

    # -- candidates -----------------------------------------------------
    def _save_candidates(self, session_id: str, candidates: list[FixCandidate]) -> None:
        payload = [
            {
                "strategy": c.strategy,
                "path": c.path,
                "old_content": c.old_content,
                "new_content": c.new_content,
                "reason": c.reason,
                "confidence": c.confidence,
                "lineno": c.lineno,
                "symbol": c.symbol,
                "behaviour_change": c.behaviour_change,
                "verification_hint": c.verification_hint,
                "notes": c.notes,
            }
            for c in candidates
        ]
        write_json(self.sessions.dir_for(session_id) / "candidates.json", payload)

    def _load_candidates(self, session_id: str) -> list[dict]:
        return read_json(self.sessions.dir_for(session_id) / "candidates.json", []) or []

    def _load_scratch(self, session_id: str) -> dict:
        return read_json(self.sessions.dir_for(session_id) / "scratch.json", {}) or {}

    def save_scratch(self, session_id: str, **fields: Any) -> None:
        payload = self._load_scratch(session_id)
        payload.update(fields)
        write_json(self.sessions.dir_for(session_id) / "scratch.json", payload)

    # -- backups / rollback --------------------------------------------
    def _latest_backup(self, session: Session) -> dict[str, str]:
        base = self.sessions.backup_dir(session.id)
        if not base.exists():
            return {}
        attempts = sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name)
        if not attempts:
            return {}
        latest = attempts[-1]
        backup: dict[str, str] = {}
        for path in latest.rglob("*"):
            if path.is_file():
                rel = rel_path(path, latest)
                try:
                    backup[rel] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return backup

    def _restore(self, backup: dict[str, str]) -> ApplyResult:
        if not backup:
            return ApplyResult(ok=True)
        return revert_patch(self.settings.repo_root, backup)

    def _rollback_files(self, session: Session, backup: dict[str, str], note: str = "") -> dict:
        base = self.sessions.backup_dir(session.id)
        result = self._restore(backup)
        archived = base / "restored"
        try:
            (archived / "note.txt").parent.mkdir(parents=True, exist_ok=True)
            (archived / "note.txt").write_text(f"{now_iso()} {note}\n", encoding="utf-8")
        except OSError:
            pass
        session.rollback = {
            "available": False,
            "files": list(backup.keys()),
            "at": now_iso(),
            "note": note,
            "ok": result.ok,
        }
        self.sessions.save(session)
        return {"restored": list(backup.keys()), "ok": result.ok, "errors": result.failed}

    # -- refinement ------------------------------------------------------
    def _request_refined_patch(self, session: Session, *, failure_text: str, instruction: str = "", previous: dict | None = None) -> list[FixCandidate]:
        if not failure_text.strip():
            return []
        top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
        path = (previous or {}).get("path") or top.get("file") or ""
        if not path or path not in self.index.files:
            return []
        source = "\n".join(self.index.source_lines(path))
        previous_diff = self.scanner.sanitize(session.patch.get("text", ""))[:6000]
        prompt = "\n".join(
            [
                "A previous patch was applied and the tests still fail.",
                "",
                "## Root cause (believed)",
                str(top.get("cause", "")),
                str(top.get("explanation", ""))[:800],
                "",
                "## Patch that was applied and did NOT verify",
                "```diff",
                previous_diff or "(unavailable)",
                "```",
                "",
                "## Failure output after applying it",
                "```",
                self.scanner.sanitize(failure_text)[:4000],
                "```",
            ]
            + ([f"\n## Developer instruction\n{instruction}"] if instruction else [])
            + [
                "",
                "## Current file content",
                f"{path}:",
                source[:24_000],
                "",
                "Produce a corrected, minimal patch as JSON with this exact shape:",
                '{"root_cause": "...", "confidence": 0.0, "files": [{"path": "%s", "reason": "...", "edits": [{"find": "...", "replace": "..."}]}]}' % path,
                "`find` must appear exactly once in the file. Reply with JSON only.",
            ]
        )
        completion, decision = self.router.call(
            "test_repair",
            prompt,
            system=(
                "You are FixPilot repairing a patch that failed its tests. "
                "Explain nothing: return JSON only, minimal edits, no new dependencies."
            ),
            max_tokens=1400,
            temperature=0.05,
            best=True,
        )
        if not completion.ok:
            self._emit(session, "refinement.unavailable", {"error": completion.error, "model": decision.profile_key if decision else ""})
            return []
        payload = _extract_json(completion.text)
        if not payload:
            self._emit(session, "refinement.discarded", {"reason": "model reply was not JSON", "model": completion.model})
            return []
        candidates: list[FixCandidate] = []
        for file_entry in payload.get("files", [])[:3]:
            entry_path = str(file_entry.get("path", path)).strip().lstrip("./")
            if entry_path not in self.index.files:
                entry_path = path
            updated = "\n".join(self.index.source_lines(entry_path))
            original = updated
            applied = 0
            for edit in file_entry.get("edits", [])[:8]:
                find = str(edit.get("find", ""))
                replace = str(edit.get("replace", ""))
                if find and updated.count(find) == 1:
                    updated = updated.replace(find, replace, 1)
                    applied += 1
            if applied == 0 or updated == original:
                continue
            candidates.append(
                FixCandidate(
                    strategy=f"refined:{completion.model}",
                    path=entry_path,
                    old_content=original,
                    new_content=updated,
                    reason=str(file_entry.get("reason") or "refined patch after test failure"),
                    confidence=0.55,
                    lineno=int(top.get("lineno") or 0),
                    symbol=str(top.get("symbol") or ""),
                    behaviour_change=True,
                    verification_hint="refined after a failed verification run",
                    notes=[f"authored by {completion.model} after failure feedback"],
                )
            )
        self._emit(
            session,
            "model.route",
            {
                "task": "test_repair",
                "model": decision.profile_key if decision else completion.model,
                "privacy": decision.privacy if decision else "",
                "latency_ms": completion.latency_ms,
                "purpose": "refinement after failed verification",
            },
        )
        return candidates

    # -- learning ---------------------------------------------------------
    def _record_lesson(self, session: Session, investigation: Investigation | None, *, outcome: str, root_cause: str = "") -> None:
        top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
        cause = root_cause or str(top.get("cause", ""))
        ingested = self._load_ingested(session.id)
        files = session.patch.get("files") or ([top.get("file")] if top.get("file") else [])
        verification = session.verification.get("status", "")
        try:
            lesson = self.lessons.record(
                session_id=session.id,
                ingested=ingested,
                root_cause=cause,
                strategy=str(session.patch.get("strategy_applied") or top.get("strategy") or ""),
                patch_summary=excerpt(session.patch.get("summary") or session.patch.get("text", ""), 500),
                verification=str(verification),
                outcome=outcome,
                files=list(files),
                confidence=float(session.verification.get("confidence") or top.get("score") or 0.5),
            )
            self._emit(session, "lesson.recorded", {"lesson_id": lesson.id, "outcome": outcome})
        except Exception as exc:  # pragma: no cover
            self._emit(session, "lesson.record_failed", {"error": str(exc)})
        try:
            self.memory.record_outcome(
                session_id=session.id,
                summary=f"{cause[:180]} → {session.patch.get('strategy_applied') or top.get('strategy') or 'manual review'}",
                success=outcome == "verified",
                files=list(files),
                strategy=str(session.patch.get("strategy_applied") or top.get("strategy") or ""),
            )
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _version() -> str:
    from .. import __version__

    return __version__


def _title_for(ingested: IngestedInput) -> str:
    base = summary_of(ingested.text.replace("\n", " "), 80) or "Bug report"
    prefix = {"voice": "Voice: ", "image": "Screenshot: ", "log": "Log: ", "mixed": "Report: ", "text": ""}.get(ingested.channel, "")
    return f"{prefix}{base}"


def _investigation_dict(investigation: Investigation) -> dict:
    return {
        "understanding": investigation.understanding,
        "evidence": investigation.evidence_dicts(),
        "hypotheses": investigation.hypothesis_dicts(),
        "skills_run": investigation.skills_run,
        "plan": investigation.plan,
        "notes": investigation.notes,
        "duration_ms": investigation.duration_ms,
        "lessons_used": investigation.lessons_used,
    }


def _cause_markdown(session: Session) -> str:
    top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
    if not top:
        return "No root cause was established."
    return (
        f"**{top.get('cause', '')}**\n\n{top.get('explanation', '')}\n\n"
        f"Confidence {int(float(top.get('score') or top.get('confidence') or 0) * 100)}% · category {top.get('category', 'unknown')}"
    )


def _patch_markdown(session: Session) -> str:
    if not session.patch.get("text"):
        return session.patch.get("summary", "No patch was produced.")
    risk = session.patch.get("risk", {})
    return "\n".join(
        [
            f"**{session.patch.get('summary', '')}**",
            "",
            f"Files: {', '.join(session.patch.get('files', []))}",
            f"Risk: {risk.get('level', 'unknown')}" + (f" — {'; '.join(risk.get('notes', []))}" if risk.get("notes") else ""),
            "",
            "```diff",
            session.patch.get("text", "")[:6000],
            "```",
        ]
    )


def _targets_from_session(session: Session) -> list[str]:
    """Explicit test node ids recorded during planning (if any)."""
    recorded = [str(item) for item in (session.patch.get("test_targets") or []) if isinstance(item, str)]
    if recorded:
        return [item for item in recorded if "::" in item][:6]
    targets: list[str] = []
    for entry in session.patch.get("verification_plan") or []:
        if isinstance(entry, str) and "::" in entry:
            targets.extend(part for part in entry.split() if "::" in part)
    return targets[:6]


def _failure_context(outcome) -> str:
    payload = outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome or {})
    parts = list(payload.get("reasons") or [])
    for run in payload.get("runs") or []:
        if run.get("exit_code") not in (0, None):
            parts.append(f"$ {run.get('command')} (exit {run.get('exit_code')})")
            if run.get("stdout_tail"):
                parts.append(str(run["stdout_tail"])[-2000:])
            if run.get("stderr_tail"):
                parts.append(str(run["stderr_tail"])[-2000:])
    return "\n".join(parts)


def _failure_context_from_session(session: Session) -> str:
    return _failure_context(session.verification or {})


def _extract_json(text: str) -> dict | None:
    import re as _re

    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = _re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
