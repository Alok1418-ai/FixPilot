"""Debugging sessions: the unit of work the phone drives.

A session is a state machine with an append-only event log.  The log is what
makes replay possible: every hypothesis, evidence bundle, model route, approval,
command, test run and rollback is an event with a sequence number, so the phone
can scrub back through exactly what happened — and a *new* agent process can pick
the session up mid-flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..store import EventLog, read_json, read_jsonl, write_json
from ..util import now_iso, new_id, short_hash

# Session lifecycle ---------------------------------------------------------
INTAKE = "intake"
INVESTIGATING = "investigating"
HYPOTHESIS = "hypothesis"
PATCH_PROPOSED = "patch_proposed"
AWAITING_APPROVAL = "awaiting_approval"
APPLYING = "applying"
VERIFYING = "verifying"
REFINING = "refining"
VERIFIED = "verified"
FAILED = "failed"
ROLLED_BACK = "rolled_back"
REJECTED = "rejected"
CANCELLED = "cancelled"

TERMINAL = {VERIFIED, FAILED, ROLLED_BACK, REJECTED, CANCELLED}
ACTIVE = {INTAKE, INVESTIGATING, HYPOTHESIS, PATCH_PROPOSED, AWAITING_APPROVAL, APPLYING, VERIFYING, REFINING}

TRANSITIONS: dict[str, set[str]] = {
    INTAKE: {INVESTIGATING, CANCELLED, FAILED},
    INVESTIGATING: {HYPOTHESIS, FAILED, CANCELLED},
    HYPOTHESIS: {PATCH_PROPOSED, INVESTIGATING, FAILED, CANCELLED},
    PATCH_PROPOSED: {AWAITING_APPROVAL, REJECTED, CANCELLED, APPLYING},
    AWAITING_APPROVAL: {APPLYING, REJECTED, CANCELLED, PATCH_PROPOSED},
    APPLYING: {VERIFYING, FAILED, ROLLED_BACK},
    VERIFYING: {VERIFIED, REFINING, FAILED, ROLLED_BACK},
    REFINING: {PATCH_PROPOSED, VERIFYING, FAILED, ROLLED_BACK, AWAITING_APPROVAL},
    VERIFIED: {INVESTIGATING, ROLLED_BACK},
    FAILED: {INVESTIGATING, ROLLED_BACK, CANCELLED},
    ROLLED_BACK: {INVESTIGATING, VERIFIED},
    REJECTED: {INVESTIGATING, PATCH_PROPOSED, CANCELLED},
    CANCELLED: set(),
}

STEP_LABELS = {
    INTAKE: "Understanding the report",
    INVESTIGATING: "Gathering evidence",
    HYPOTHESIS: "Ranking root causes",
    PATCH_PROPOSED: "Drafting patch",
    AWAITING_APPROVAL: "Waiting for your approval",
    APPLYING: "Applying patch",
    VERIFYING: "Running tests",
    REFINING: "Refining the patch",
    VERIFIED: "Verified",
    FAILED: "Could not verify",
    ROLLED_BACK: "Rolled back",
    REJECTED: "Rejected by you",
    CANCELLED: "Cancelled",
}


@dataclass(slots=True)
class Session:
    id: str
    title: str = ""
    status: str = INTAKE
    channel: str = "text"
    created_at: str = ""
    updated_at: str = ""
    repo: dict = field(default_factory=dict)
    input: dict = field(default_factory=dict)
    understanding: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    patch: dict = field(default_factory=dict)
    approval: dict = field(default_factory=dict)
    applies: list[dict] = field(default_factory=list)
    tests: list[dict] = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    rollback: dict = field(default_factory=dict)
    model_routes: list[dict] = field(default_factory=list)
    lessons_used: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    iterations: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "status_label": STEP_LABELS.get(self.status, self.status),
            "channel": self.channel,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "repo": self.repo,
            "input": self.input,
            "understanding": self.understanding,
            "evidence": self.evidence,
            "hypotheses": self.hypotheses,
            "patch": self.patch,
            "approval": self.approval,
            "applies": self.applies,
            "tests": self.tests,
            "verification": self.verification,
            "rollback": self.rollback,
            "model_routes": self.model_routes,
            "lessons_used": self.lessons_used,
            "plan": self.plan,
            "metrics": self.metrics,
            "iterations": self.iterations,
            "error": self.error or None,
            "notes": self.notes,
            "terminal": self.status in TERMINAL,
        }

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "status_label": STEP_LABELS.get(self.status, self.status),
            "channel": self.channel,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "iterations": self.iterations,
            "patch_files": self.patch.get("files", []),
            "verification": self.verification.get("status", ""),
            "root_cause": (self.understanding.get("root_cause") or "")[:200],
            "confidence": self.understanding.get("confidence", 0),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Session":
        session = cls(id=payload.get("id") or new_id("sess", 8))
        for key in (
            "title", "status", "channel", "created_at", "updated_at", "repo", "input", "understanding",
            "evidence", "hypotheses", "patch", "approval", "applies", "tests", "verification", "rollback",
            "model_routes", "lessons_used", "plan", "metrics", "iterations", "error", "notes",
        ):
            if key in payload and payload[key] is not None:
                setattr(session, key, payload[key])
        return session

    # -- helpers -------------------------------------------------------
    def can_transition(self, to_status: str) -> bool:
        return to_status in TRANSITIONS.get(self.status, set()) or to_status == self.status


class SessionStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.sessions_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------
    def dir_for(self, session_id: str) -> Path:
        return self.root / session_id

    def json_path(self, session_id: str) -> Path:
        return self.dir_for(session_id) / "session.json"

    def events_path(self, session_id: str) -> Path:
        return self.dir_for(session_id) / "events.jsonl"

    def patch_path(self, session_id: str) -> Path:
        return self.dir_for(session_id) / "patch.diff"

    def backup_dir(self, session_id: str) -> Path:
        return self.dir_for(session_id) / "backup"

    # -- lifecycle -----------------------------------------------------
    def create(self, *, title: str, channel: str, repo: dict, input_payload: dict) -> Session:
        session = Session(
            id=new_id("sess", 10),
            title=title or "Untitled debugging session",
            status=INTAKE,
            channel=channel,
            created_at=now_iso(),
            updated_at=now_iso(),
            repo=repo,
            input=input_payload,
        )
        self.dir_for(session.id).mkdir(parents=True, exist_ok=True)
        self.save(session)
        self.log(session.id, "session.created", {"title": session.title, "channel": channel})
        return session

    def get(self, session_id: str) -> Session | None:
        payload = read_json(self.json_path(session_id))
        if not payload:
            return None
        return Session.from_dict(payload)

    def save(self, session: Session) -> Session:
        session.updated_at = now_iso()
        self.dir_for(session.id).mkdir(parents=True, exist_ok=True)
        write_json(self.json_path(session.id), session.to_dict())
        return session

    def update(self, session: Session, **fields: Any) -> Session:
        for key, value in fields.items():
            if value is not None:
                setattr(session, key, value)
        return self.save(session)

    def set_status(self, session: Session, status: str, *, note: str = "", force: bool = False) -> Session:
        if not force and not session.can_transition(status):
            self.log(session.id, "session.invalid_transition", {"from": session.status, "to": status, "note": note}, actor="system")
            return session
        previous = session.status
        session.status = status
        self.save(session)
        self.log(session.id, "session.status", {"from": previous, "to": status, "label": STEP_LABELS.get(status, status), "note": note})
        return session

    def log(self, session_id: str, kind: str, payload: dict | None = None, *, actor: str = "agent") -> dict:
        log = EventLog(self.events_path(session_id))
        return log.append(kind, payload, actor=actor)

    def events(self, session_id: str, limit: int | None = None, since_seq: int = 0) -> list[dict]:
        return EventLog(self.events_path(session_id)).events(limit=limit, since_seq=since_seq)

    def list(self, *, limit: int = 40, status: str | None = None) -> list[dict]:
        sessions: list[Session] = []
        if not self.root.exists():
            return []
        for child in sorted(self.root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            session = self.get(child.name)
            if session is None:
                continue
            if status and session.status != status:
                continue
            sessions.append(session)
        sessions.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
        return [s.summary() for s in sessions[:limit]]

    def active(self) -> list[dict]:
        return [item for item in self.list(limit=100) if item["status"] in ACTIVE]

    def delete(self, session_id: str) -> bool:
        import shutil

        target = self.dir_for(session_id)
        if not target.exists():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # -- replay --------------------------------------------------------
    def replay(self, session_id: str, *, since_seq: int = 0, limit: int = 400) -> dict:
        session = self.get(session_id)
        if session is None:
            return {}
        events = self.events(session_id, limit=limit, since_seq=since_seq)
        return {
            "session": session.summary(),
            "status": session.status,
            "events": events,
            "count": len(events),
            "next_seq": events[-1]["seq"] if events else since_seq,
        }

    def timeline(self, session_id: str) -> list[dict]:
        """Human-scale milestones for the phone UI (no raw internals)."""
        session = self.get(session_id)
        if session is None:
            return []
        milestones: list[dict] = []
        for event in self.events(session_id):
            kind = event.get("kind", "")
            payload = event.get("payload", {})
            label = None
            detail = ""
            if kind == "session.created":
                label = "Report received"
                detail = payload.get("title", "")
            elif kind == "input.understood":
                label = "Input understood"
                detail = payload.get("summary", "")
            elif kind == "evidence.gathered":
                label = "Evidence gathered"
                detail = f"{payload.get('count', 0)} items"
            elif kind == "hypothesis.ranked":
                label = "Root cause ranked"
                detail = (payload.get("top") or {}).get("cause", "")
            elif kind == "patch.proposed":
                label = "Patch drafted"
                detail = f"{payload.get('files', 0)} file(s), +{payload.get('additions', 0)}/-{payload.get('deletions', 0)}"
            elif kind == "approval.requested":
                label = "Approval requested"
                detail = "waiting on you"
            elif kind == "approval.granted":
                label = "Approved"
                detail = payload.get("note", "")
            elif kind == "approval.rejected":
                label = "Rejected"
                detail = payload.get("note", "")
            elif kind == "patch.applied":
                label = "Patch applied"
                detail = ", ".join(payload.get("files", [])[:3])
            elif kind == "tests.completed":
                label = "Tests finished"
                detail = f"{payload.get('passed', 0)} passed, {payload.get('failed', 0)} failed"
            elif kind == "verification.completed":
                label = "Verification complete"
                detail = payload.get("status", "")
            elif kind == "patch.refined":
                label = "Patch refined"
                detail = payload.get("reason", "")
            elif kind == "rollback.completed":
                label = "Rolled back"
                detail = ", ".join(payload.get("files", [])[:3])
            elif kind == "model.route":
                label = "Model routed"
                detail = f"{payload.get('task')} → {payload.get('model')}"
            elif kind == "lesson.reused":
                label = "Reused past lesson"
                detail = payload.get("symptom", "")
            if label:
                milestones.append(
                    {
                        "seq": event.get("seq"),
                        "ts": event.get("ts"),
                        "kind": kind,
                        "label": label,
                        "detail": detail,
                        "actor": event.get("actor", "agent"),
                    }
                )
        return milestones
