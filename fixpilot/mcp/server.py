"""FixPilot as a Model Context Protocol server (stdio, JSON-RPC 2.0).

Any MCP-capable client (an editor agent, Claude Desktop, a local LLM harness)
can drive the same investigation loop the phone drives.  The server exposes the
agent's *read* surface freely and keeps every mutating tool gated: approval,
apply and command execution all run through the normal policy and require an
explicit ``confirm`` flag from the caller.

    python3 -m fixpilot mcp serve          # speaks MCP on stdin/stdout
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m fixpilot mcp serve

Nothing here imports a third-party SDK on purpose: the protocol is small and
stdlib-only keeps the deployment story the same as the rest of FixPilot.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO

from ..config import Settings
from ..core.agent import FixPilotAgent
from ..memory.sessions import Session
from ..security.executor import ExecResult
from ..util import excerpt

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fixpilot", "version": "0.4.0"}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


#: name -> (description, input schema).  Kept declarative so `tools/list` and
#: the dispatcher can never drift apart.
TOOL_SPECS: dict[str, tuple[str, dict]] = {
    "fixpilot_overview": (
        "Repository, index, model routing and memory summary for the project FixPilot is attached to.",
        _schema({}),
    ),
    "fixpilot_search": (
        "Search indexed symbols and source text. Returns paths with line numbers.",
        _schema({"query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, ["query"]),
    ),
    "fixpilot_read_file": (
        "Read a source file (optionally a line range). Secret-looking lines are redacted.",
        _schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "max_lines": {"type": "integer", "default": 400},
            },
            ["path"],
        ),
    ),
    "fixpilot_list_sessions": (
        "List debugging sessions, newest first.",
        _schema({"limit": {"type": "integer", "default": 20}}),
    ),
    "fixpilot_session": (
        "Full state of one session: understanding, evidence, hypotheses, patch, verification.",
        _schema({"id": {"type": "string"}}, ["id"]),
    ),
    "fixpilot_investigate": (
        "Run the investigation loop on a bug report (text, log or voice transcript). "
        "Proposes a patch but never writes it — approval is a separate, confirmed call.",
        _schema(
            {
                "text": {"type": "string"},
                "channel": {"type": "string", "enum": ["text", "log", "voice", "image"]},
                "cost_budget": {"type": "string", "enum": ["fast", "normal", "deep"], "default": "deep"},
            },
            ["text"],
        ),
    ),
    "fixpilot_decide": (
        "Record your approval or rejection of the proposed patch. confirm=true is mandatory: "
        "this is the human-in-the-loop gate.",
        _schema(
            {"id": {"type": "string"}, "approved": {"type": "boolean"}, "note": {"type": "string"}, "confirm": {"type": "boolean"}},
            ["id", "approved", "confirm"],
        ),
    ),
    "fixpilot_apply": (
        "Apply the approved patch, run the relevant tests, refine on failure and roll back if "
        "verification fails. requires confirm=true.",
        _schema({"id": {"type": "string"}, "confirm": {"type": "boolean"}}, ["id", "confirm"]),
    ),
    "fixpilot_verify": (
        "Run the project's test suite (optionally narrowed) against the current working tree.",
        _schema({"targets": {"type": "array", "items": {"type": "string"}}, "test_files": {"type": "array", "items": {"type": "string"}}}),
    ),
    "fixpilot_policy_check": (
        "Ask the command policy how it would treat a shell command, without running it.",
        _schema({"command": {"type": "string", "cwd": {"type": "string"}}}, ["command"]),
    ),
    "fixpilot_report": (
        "Markdown report for a finished session — the 'I found a bug → I verified the fix' story.",
        _schema({"id": {"type": "string"}}, ["id"]),
    ),
    "fixpilot_replay": (
        "Replay a session's event log for debugging the debugging session.",
        _schema({"id": {"type": "string"}, "since_seq": {"type": "integer", "default": 0}}, ["id"]),
    ),
    "fixpilot_officekit_enqueue": (
        "Queue a job for the paired workstation (iQOO Office Kit relay): run tests, apply a patch, "
        "collect evidence, sync the repo or checkpoint state.",
        _schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["run_tests", "apply_patch", "collect_evidence", "run_command", "sync_repo", "checkpoint", "verify"],
                },
                "payload": {"type": "object"},
                "session_id": {"type": "string"},
            },
            ["kind"],
        ),
    ),
}


def _text(value: Any) -> dict:
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, default=str)
    return {"content": [{"type": "text", "text": value}], "isError": False}


def _error_text(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class MCPServer:
    """Handles MCP requests against a :class:`FixPilotAgent`."""

    def __init__(self, agent: FixPilotAgent, settings: Settings | None = None) -> None:
        self.agent = agent
        self.settings = settings or agent.settings
        self.bridge = agent.bridge if hasattr(agent, "bridge") else None
        self.tools: dict[str, Callable[[dict], Any]] = {
            "fixpilot_overview": self.t_overview,
            "fixpilot_search": self.t_search,
            "fixpilot_read_file": self.t_read_file,
            "fixpilot_list_sessions": self.t_list_sessions,
            "fixpilot_session": self.t_session,
            "fixpilot_investigate": self.t_investigate,
            "fixpilot_decide": self.t_decide,
            "fixpilot_apply": self.t_apply,
            "fixpilot_verify": self.t_verify,
            "fixpilot_policy_check": self.t_policy_check,
            "fixpilot_report": self.t_report,
            "fixpilot_replay": self.t_replay,
            "fixpilot_officekit_enqueue": self.t_officekit_enqueue,
        }

    # ------------------------------------------------------------------
    # protocol
    # ------------------------------------------------------------------
    def handle(self, message: dict) -> dict | None:
        """Handle one JSON-RPC message; returns None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, JSONRPC_INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method is None:
            return self._error(request_id, JSONRPC_INVALID_REQUEST, "missing method")
        if request_id is None and str(method).startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return self._ok(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                        "instructions": (
                            "FixPilot investigates bugs, proposes reviewable patches and verifies them "
                            "with the project's own tests. Nothing is written until fixpilot_decide is "
                            "called with confirm=true."
                        ),
                    },
                )
            if method == "ping":
                return self._ok(request_id, {})
            if method == "tools/list":
                return self._ok(request_id, {"tools": self.tool_list()})
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                return self._ok(request_id, self.call_tool(str(name), dict(arguments)))
            if method in {"resources/list", "prompts/list"}:
                return self._ok(request_id, {"resources": []} if method.startswith("resources") else {"prompts": []})
            return self._error(request_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method {method!r}")
        except Exception as exc:  # a tool crash must not kill the session
            return self._error(request_id, JSONRPC_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def call_tool(self, name: str, arguments: dict) -> dict:
        handler = self.tools.get(name)
        if handler is None:
            return _error_text(f"unknown tool {name!r}")
        self.agent.audit.record(
            {"actor": "mcp", "event": "tool.call", "tool": name, "arguments": _safe_args(arguments)}
        )
        missing = [key for key in TOOL_SPECS.get(name, ("", {}))[1].get("required", []) if arguments.get(key) in (None, "")]
        if missing:
            return _error_text(f"missing required argument(s) for {name}: {', '.join(missing)}")
        result = handler(arguments)
        if isinstance(result, dict) and set(result.keys()) == {"content", "isError"}:
            return result
        return _text(result)

    def tool_list(self) -> list[dict]:
        return [
            {"name": name, "description": TOOL_SPECS[name][0], "inputSchema": TOOL_SPECS[name][1]}
            for name in TOOL_SPECS
        ]

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------------
    # stdio loop
    # ------------------------------------------------------------------
    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response = self._error(None, JSONRPC_PARSE_ERROR, f"invalid JSON: {exc.msg}")
            else:
                response = self.handle(message)
            if response is None:
                continue
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()
        return 0

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def t_overview(self, args: dict) -> dict:
        overview = self.agent.overview()
        repo = overview["repo"]
        stats = repo["stats"]
        return {
            "product": overview["product"],
            "repo": {"root": repo["root"], "stats": stats, "test_command": repo["test_command"]},
            "models": {"strategy": overview["models"]["strategy"], "cloud_allowed": overview["models"]["cloud_allowed"]},
            "memory": {"facts": overview["memory"]["facts"], "conventions": overview["memory"]["conventions"][:6]},
            "lessons": overview["lessons"]["total"],
            "skills": [skill["name"] for skill in overview["skills"]],
        }

    def t_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        return {"query": query, "results": self.agent.search(query, limit=int(args.get("limit", 20)))}

    def t_read_file(self, args: dict) -> dict:
        path = str(args.get("path") or "")
        resolved = self.agent.index.resolve_path(path) or path.replace("\\", "/").lstrip("./")
        if resolved not in self.agent.index.files:
            raise ValueError(f"{path!r} is not part of the indexed repository")
        lines = self.agent.index.source_lines(resolved)
        start = max(1, int(args.get("start_line", 1)))
        end = int(args.get("end_line", 0)) or len(lines)
        end = min(end, len(lines), start + int(args.get("max_lines", 400)) - 1)
        numbered = [f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1)]
        return {"path": resolved, "start_line": start, "end_line": end, "text": "\n".join(numbered)}

    def t_list_sessions(self, args: dict) -> dict:
        return {"sessions": self.agent.list_sessions(limit=int(args.get("limit", 20)))}

    def t_session(self, args: dict) -> dict:
        session = self.agent.sessions.get(str(args.get("id") or ""))
        if session is None:
            raise ValueError(f"unknown session {args.get('id')!r}")
        return _session_digest(session)

    def t_investigate(self, args: dict) -> dict:
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        payload: dict = {"text": text, "channel": args.get("channel") or ("log" if "Traceback" in text else "text")}
        result = self.agent.run(payload, cost_budget=str(args.get("cost_budget", "deep")))
        return _session_digest(Session.from_dict(result["session"]))

    def t_decide(self, args: dict) -> dict:
        if not args.get("confirm"):
            return _error_text("refusing to record an approval without confirm=true — approvals are human decisions")
        result = self.agent.approve(
            str(args.get("id") or ""),
            approved=bool(args.get("approved")),
            note=str(args.get("note") or "decided via MCP"),
            actor="mcp",
        )
        if not result.get("ok"):
            return _error_text(result.get("error", "approval failed"))
        return {"ok": True, "decision": result["session"]["approval"], "session_id": result["session"]["id"]}

    def t_apply(self, args: dict) -> dict:
        if not args.get("confirm"):
            return _error_text("refusing to write to the repository without confirm=true")
        result = self.agent.apply_and_verify(str(args.get("id") or ""))
        session = Session.from_dict(result.get("session") or self.agent.sessions.get(str(args.get("id"))).to_dict())
        return {
            "ok": result.get("ok", False),
            "verified": result.get("verified", False),
            "rolled_back": result.get("rolled_back", False),
            "status": session.status,
            "verification": (session.verification or {}).get("status"),
            "tests": (session.verification or {}).get("tests", {}),
            "explanation": result.get("explanation", ""),
            "error": result.get("error"),
        }

    def t_verify(self, args: dict) -> dict:
        report, result = self.agent.verifier.run_tests(
            self.agent.index,
            targets=list(args.get("targets") or []),
            test_files=list(args.get("test_files") or []),
        )
        return {
            "ok": bool(report.ok),
            "command": result.command,
            "exit_code": result.exit_code,
            "summary": report.render(),
            "output": result.combined(4000),
        }

    def t_policy_check(self, args: dict) -> dict:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        verdict = self.agent.policy.evaluate_string(
            command, cwd=args.get("cwd") or self.settings.repo_root, purpose="asked over MCP"
        )
        return verdict.to_dict()

    def t_report(self, args: dict) -> dict:
        payload = self.agent.report(str(args.get("id") or ""))
        return {"session_id": payload["session_id"], "markdown": payload["markdown"]}

    def t_replay(self, args: dict) -> dict:
        return self.agent.replay(str(args.get("id") or ""), since_seq=int(args.get("since_seq", 0)))

    def t_officekit_enqueue(self, args: dict) -> dict:
        from ..officekit import OfficeKitBridge

        bridge = OfficeKitBridge(self.settings)
        return bridge.enqueue(
            kind=str(args.get("kind") or "run_command"),
            payload=dict(args.get("payload") or {}),
            session_id=str(args.get("session_id") or ""),
        )


def _session_digest(session: Session) -> dict:
    top = session.hypotheses[0] if session.hypotheses else {}
    patch = session.patch or {}
    verification = session.verification or {}
    return {
        "id": session.id,
        "title": session.title,
        "status": session.status,
        "understanding": session.understanding,
        "root_cause": {
            "cause": top.get("cause"),
            "category": top.get("category"),
            "file": top.get("file"),
            "lineno": top.get("lineno"),
            "strategy": top.get("strategy"),
            "confidence": top.get("score"),
        }
        if top
        else None,
        "evidence": [
            {"kind": item["kind"], "claim": item["claim"], "where": f"{item['path']}:{item['lineno']}" if item.get("path") else item.get("source")}
            for item in session.evidence[:8]
        ],
        "patch": {
            "files": patch.get("files", []),
            "summary": patch.get("summary", ""),
            "risk": patch.get("risk", {}),
            "diff": patch.get("text", ""),
        }
        if patch
        else None,
        "approval": session.approval,
        "verification": {
            "status": verification.get("status"),
            "confidence": verification.get("confidence"),
            "reasons": verification.get("reasons", [])[:5],
            "tests": verification.get("tests", {}),
        }
        if verification
        else None,
        "next_step": (
            "awaiting the human decision: call fixpilot_decide"
            if session.status == "awaiting_approval"
            else "patch applied and verified" if session.status == "verified" else session.status
        ),
    }


def _safe_args(arguments: dict) -> dict:
    """Audit-log arguments without letting long blobs blow up the log."""
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 200:
            safe[key] = excerpt(value, 120)
        else:
            safe[key] = value
    return safe


__all__ = ["MCPServer", "TOOL_SPECS", "PROTOCOL_VERSION"]
