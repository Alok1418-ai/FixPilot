"""FixPilot's HTTP API — the surface the phone talks to.

Standard library only (``http.server`` + ``ThreadingHTTPServer``), so the whole
product runs with zero installs.  Design decisions worth knowing:

* **Same-origin only.** No CORS headers are emitted; the PWA is served by this
  same process, and the phone reaches it either directly on the LAN or through
  the iQOO Office Kit bridge.
* **Token-gated mutations.** A per-process token is embedded into the served page
  and required on every ``POST`` (header ``X-FixPilot-Token`` or ``?token=``), so
  a stray page in another tab cannot drive the agent.
* **Never trusts the client.** Approvals, policy verdicts and verification all
  happen server-side; the API is a thin, auditable shell over the agent.
"""

from __future__ import annotations

import errno
import json
import mimetypes
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..core.agent import FixPilotAgent
from ..officekit import OfficeKitBridge
from ..util import excerpt, new_id, now_iso, redact
from ..security.secrets import is_sensitive_path, scan_text

MAX_BODY_BYTES = 8 * 1024 * 1024  # screenshots as base64


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    raw_body: bytes = b""
    remote: str = ""
    params: dict[str, str] = field(default_factory=dict)
    token: str = ""

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    def param(self, name: str, default: str = "") -> str:
        return self.params.get(name, default)


@dataclass
class Response:
    status: int = 200
    payload: Any = None
    content_type: str = "application/json"
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class FixPilotServer:
    def __init__(self, agent: FixPilotAgent, settings: Settings, *, require_token: bool = True) -> None:
        self.agent = agent
        self.settings = settings
        self.token = new_id("tok", 24)
        self.require_token = require_token
        self.bridge = OfficeKitBridge(settings)
        self.static_dir = Path(__file__).resolve().parent.parent / "web"
        self.started_at = now_iso()
        self.routes: list[tuple[str, re.Pattern[str], Callable[[Request], Response]]] = []
        self._register_routes()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def route(self, method: str, pattern: str, handler: Callable[[Request], Response] | None = None):
        """Register a route; usable directly (``route("GET", p, h)``) or as a decorator."""
        compiled = re.compile("^" + pattern.replace("*", "[^/]+") + "$")

        def decorator(target: Callable[[Request], Response]):
            self.routes.append((method.upper(), compiled, target))
            return target

        if handler is not None:
            return decorator(handler)
        return decorator

    def dispatch(self, request: Request) -> Response:
        for method, pattern, handler in self.routes:
            if method != request.method:
                continue
            match = pattern.match(request.path)
            if not match:
                continue
            request.params = {key: urllib.parse.unquote(value) for key, value in match.groupdict().items()}
            if request.method == "POST" and self.require_token and request.token != self.token:
                raise ApiError(401, "invalid or missing FixPilot token (X-FixPilot-Token header)")
            return handler(request)
        if request.path.startswith("/api/"):
            raise ApiError(404, f"no route for {request.method} {request.path}")
        return self._serve_static(request)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        for method, path, handler in (
            ("GET", r"/api/health", self.h_health),
            ("GET", r"/api/overview", self.h_overview),
            ("GET", r"/api/skills", self.h_skills),
            ("GET", r"/api/models", self.h_models),
            ("GET", r"/api/memory", self.h_memory),
            ("GET", r"/api/lessons", self.h_lessons),
            ("GET", r"/api/sandbox", self.h_sandbox),
            ("GET", r"/api/audit", self.h_audit),
            ("GET", r"/api/samples", self.h_samples),
            ("GET", r"/api/tree", self.h_tree),
            ("GET", r"/api/file", self.h_file),
            ("GET", r"/api/search", self.h_search),
            ("GET", r"/api/diff/worktree", self.h_worktree_diff),
            ("GET", r"/api/officekit", self.h_officekit),
            ("POST", r"/api/input", self.h_input),
            ("POST", r"/api/sessions", self.h_create_session),
            ("GET", r"/api/sessions", self.h_list_sessions),
            ("GET", r"/api/sessions/(?P<id>[^/]+)", self.h_get_session),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/status", self.h_status),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/timeline", self.h_timeline),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/events", self.h_events),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/explain", self.h_explain),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/report", self.h_report),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/diff", self.h_diff),
            ("GET", r"/api/sessions/(?P<id>[^/]+)/replay", self.h_replay),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/investigate", self.h_investigate),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/approve", self.h_approve),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/apply", self.h_apply),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/refine", self.h_refine),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/rollback", self.h_rollback),
            ("POST", r"/api/sessions/(?P<id>[^/]+)/cancel", self.h_cancel),
            ("POST", r"/api/ask", self.h_ask),
            ("POST", r"/api/command", self.h_command),
            ("POST", r"/api/officekit/pair", self.h_officekit_pair),
            ("POST", r"/api/officekit/heartbeat", self.h_officekit_heartbeat),
            ("POST", r"/api/officekit/jobs", self.h_officekit_job_create),
            ("GET", r"/api/officekit/jobs", self.h_officekit_jobs),
            ("POST", r"/api/officekit/jobs/(?P<id>[^/]+)/claim", self.h_officekit_job_claim),
            ("POST", r"/api/officekit/jobs/(?P<id>[^/]+)/complete", self.h_officekit_job_complete),
        ):
            self.route(method, path, handler)

    # -- health / introspection ----------------------------------------
    def h_health(self, request: Request) -> Response:
        return Response(200, {"ok": True, "product": "FixPilot", "started_at": self.started_at, "now": now_iso()})

    def h_overview(self, request: Request) -> Response:
        return Response(200, self.agent.overview())

    def h_skills(self, request: Request) -> Response:
        return Response(200, {"skills": self.agent.registry.summary(), "plan": [s.name for s in self.agent.registry.all()]})

    def h_models(self, request: Request) -> Response:
        return Response(200, self.agent.router.describe())

    def h_memory(self, request: Request) -> Response:
        memory = self.agent.memory.load()
        return Response(200, {"memory": memory.summary(), "facts": [f.to_dict() for f in memory.facts[-40:]]})

    def h_lessons(self, request: Request) -> Response:
        return Response(200, self.agent.lessons.summary())

    def h_sandbox(self, request: Request) -> Response:
        return Response(200, {"stats": self.agent.executor.stats(), "policy": self.agent.policy.describe()})

    def h_audit(self, request: Request) -> Response:
        limit = int(request.q("limit", "80") or 80)
        return Response(200, {"entries": self.agent.audit.tail(limit=limit)})

    def h_samples(self, request: Request) -> Response:
        candidates = [
            self.settings.repo_root / "reports",
            self.settings.repo_root / "examples" / "sample-project" / "reports",
            Path(__file__).resolve().parent.parent / "samples",
        ]
        samples: list[dict] = []
        seen: set[str] = set()
        for sample_dir in candidates:
            for path in sorted(sample_dir.glob("*.txt")) if sample_dir.is_dir() else []:
                if path.stem in seen:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                seen.add(path.stem)
                samples.append({"name": path.stem, "label": path.stem.replace("-", " ").title(), "text": text, "bytes": len(text)})
        if not samples:
            samples = [
                {
                    "name": "template-traceback",
                    "label": "Paste a traceback",
                    "text": "Traceback (most recent call last):\n  File \"/app/service.py\", line 42, in handle\n    return rows[0]\nIndexError: list index out of range\n",
                }
            ]
        return Response(200, {"samples": samples})

    # -- repo inspection ----------------------------------------------
    def h_tree(self, request: Request) -> Response:
        index = self.agent.index
        limit = int(request.q("limit", "400") or 400)
        files = sorted(index.files)
        return Response(
            200,
            {
                "root": str(index.root),
                "files": [
                    {"path": path, "language": index.files[path].language, "lines": index.files[path].lines,
                     "symbols": len(index.files[path].symbols), "tests": bool(re.search(r"(^|/)(tests?|__tests__)(/|$)|(^|/)test_", path))}
                    for path in files[:limit]
                ],
                "total": len(files),
            },
        )

    def h_file(self, request: Request) -> Response:
        path = request.q("path")
        sensitive, label = is_sensitive_path(path)
        if sensitive:
            raise ApiError(403, f"{path} is a protected {label} and is never served")
        target = (self.agent.index.root / path).resolve()
        try:
            target.relative_to(self.agent.index.root.resolve())
        except ValueError:
            raise ApiError(400, "path escapes the repository")
        if not target.is_file():
            raise ApiError(404, f"{path} not found")
        text = target.read_text(encoding="utf-8", errors="replace")[:200_000]
        findings = scan_text(text, path=path)
        return Response(
            200,
            {
                "path": path,
                "text": redact(text),
                "bytes": target.stat().st_size,
                "secrets_redacted": len(findings),
            },
        )

    def h_search(self, request: Request) -> Response:
        query = request.q("q") or request.q("query")
        if not query:
            raise ApiError(400, "provide ?q=")
        return Response(200, self.agent.search(query, limit=int(request.q("limit", "20") or 20)))

    def h_worktree_diff(self, request: Request) -> Response:
        return Response(200, self.agent.working_tree_diff())

    # -- input & sessions ---------------------------------------------
    def h_input(self, request: Request) -> Response:
        ingested = self.agent.ingest(request.body)
        return Response(200, {"input": ingested.to_dict()})

    def h_create_session(self, request: Request) -> Response:
        payload = dict(request.body)
        cost_budget = payload.pop("cost_budget", self.settings.models.strategy and "deep")
        result = self.agent.run(payload, cost_budget=cost_budget or "deep")
        return Response(201, result)

    def h_list_sessions(self, request: Request) -> Response:
        return Response(200, {"sessions": self.agent.list_sessions(limit=int(request.q("limit", "30") or 30))})

    def h_get_session(self, request: Request) -> Response:
        return Response(200, self._session_payload(request.param("id")))

    def h_status(self, request: Request) -> Response:
        return Response(200, self.agent.status(request.param("id")))

    def h_timeline(self, request: Request) -> Response:
        return Response(200, {"timeline": self.agent.sessions.timeline(request.param("id"))})

    def h_events(self, request: Request) -> Response:
        since = int(request.q("since_seq", "0") or 0)
        return Response(200, {"events": self.agent.sessions.events(request.param("id"), since_seq=since)})

    def h_explain(self, request: Request) -> Response:
        return Response(200, self.agent.explain(request.param("id")))

    def h_report(self, request: Request) -> Response:
        report = self.agent.report(request.param("id"))
        if request.q("format") == "markdown":
            return Response(200, report["markdown"], content_type="text/markdown; charset=utf-8")
        return Response(200, report)

    def h_diff(self, request: Request) -> Response:
        return Response(200, self.agent.diff_preview(request.param("id")))

    def h_replay(self, request: Request) -> Response:
        since = int(request.q("since_seq", "0") or 0)
        return Response(200, self.agent.replay(request.param("id"), since_seq=since))

    def h_investigate(self, request: Request) -> Response:
        session_id = request.param("id")
        budget = request.body.get("cost_budget", "deep")
        return Response(200, self.agent.investigate(session_id, cost_budget=budget, propose=request.body.get("propose", True)))

    def h_approve(self, request: Request) -> Response:
        approved = bool(request.body.get("approved", True))
        note = str(request.body.get("note", ""))
        result = self.agent.approve(request.param("id"), approved=approved, note=note, actor="phone")
        if not result.get("ok"):
            raise ApiError(409, result.get("error", "approval failed"))
        if approved and request.body.get("apply", True):
            verified = self.agent.apply_and_verify(request.param("id"))
            return Response(200, {**result, **verified})
        return Response(200, result)

    def h_apply(self, request: Request) -> Response:
        result = self.agent.apply_and_verify(
            request.param("id"), auto_rollback=bool(request.body.get("auto_rollback", True))
        )
        return Response(200 if result.get("ok") or result.get("rolled_back") else 409, result)

    def h_refine(self, request: Request) -> Response:
        result = self.agent.refine(request.param("id"), instruction=str(request.body.get("instruction", "")))
        return Response(200, result)

    def h_rollback(self, request: Request) -> Response:
        result = self.agent.rollback(request.param("id"), note=str(request.body.get("note", "")) or "rollback from the phone")
        return Response(200 if result.get("ok") else 409, result)

    def h_cancel(self, request: Request) -> Response:
        return Response(200, self.agent.cancel(request.param("id")))

    # -- assistant & sandbox console ----------------------------------
    def h_ask(self, request: Request) -> Response:
        question = str(request.body.get("question") or request.body.get("q") or "")
        return Response(200, self.agent.answer(question))

    def h_command(self, request: Request) -> Response:
        command = str(request.body.get("command", "")).strip()
        if not command:
            raise ApiError(400, "provide {command: '...'}")
        approved = bool(request.body.get("approved", False))
        verdict = self.agent.policy.evaluate_string(command, cwd=self.settings.repo_root, purpose="console command from the phone")
        if verdict.decision == "deny":
            self.agent.audit.record({"kind": "console.blocked", "command": command, "category": verdict.category})
            return Response(403, {"blocked": True, "verdict": verdict.to_dict()})
        result = self.agent.executor.run_string(
            command,
            cwd=self.settings.repo_root,
            approved=approved,
            timeout=int(request.body.get("timeout") or self.settings.sandbox.quick_timeout_seconds),
            purpose="console command from the phone",
        )
        return Response(200, {"verdict": verdict.to_dict(), "result": result.to_dict()})

    # -- iQOO Office Kit bridge ---------------------------------------
    def h_officekit(self, request: Request) -> Response:
        return Response(200, self.bridge.describe())

    def h_officekit_pair(self, request: Request) -> Response:
        result = self.bridge.pair(
            device_name=str(request.body.get("device_name", "iQOO phone")),
            device_id=str(request.body.get("device_id", "")),
            capabilities=list(request.body.get("capabilities") or []),
            repo_root=str(request.body.get("repo_root") or self.settings.repo_root),
        )
        return Response(201, result)

    def h_officekit_heartbeat(self, request: Request) -> Response:
        result = self.bridge.heartbeat(device_id=str(request.body.get("device_id", "")), stats=request.body.get("stats") or {})
        return Response(200 if result.get("ok") else 404, result)

    def h_officekit_jobs(self, request: Request) -> Response:
        device = request.q("device")
        return Response(200, {"jobs": self.bridge.list_jobs(device=device or None, limit=int(request.q("limit", "20") or 20))})

    def h_officekit_job_create(self, request: Request) -> Response:
        job = self.bridge.enqueue(
            kind=str(request.body.get("kind", "verify")),
            payload=request.body.get("payload") or {},
            session_id=str(request.body.get("session_id", "")),
        )
        return Response(201, job)

    def h_officekit_job_claim(self, request: Request) -> Response:
        job = self.bridge.claim(request.param("id"), device_id=str(request.body.get("device_id", "")))
        return Response(200 if job else 409, job or {"ok": False, "error": "job is not claimable"})

    def h_officekit_job_complete(self, request: Request) -> Response:
        job = self.bridge.complete(
            request.param("id"),
            result=request.body.get("result") or {},
            status=str(request.body.get("status", "completed")),
        )
        return Response(200 if job else 404, job or {"ok": False, "error": "unknown job"})

    # ------------------------------------------------------------------
    # Static PWA
    # ------------------------------------------------------------------
    def _serve_static(self, request: Request) -> Response:
        if request.method != "GET":
            raise ApiError(405, "method not allowed")
        relative = request.path.lstrip("/") or "index.html"
        target = (self.static_dir / relative).resolve()
        try:
            target.relative_to(self.static_dir.resolve())
        except ValueError:
            raise ApiError(403, "forbidden")
        if not target.is_file():
            target = self.static_dir / "index.html"
            if not target.is_file():
                raise ApiError(404, "web assets not bundled")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.name == "index.html":
            html = target.read_text(encoding="utf-8")
            html = html.replace("__FIXPILOT_TOKEN__", self.token)
            body = html.encode("utf-8")
            return Response(200, None, "text/html; charset=utf-8", raw=body)
        return Response(200, None, content_type, raw=target.read_bytes())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _session_payload(self, session_id: str) -> dict:
        session = self.agent.sessions.get(session_id)
        if session is None:
            raise ApiError(404, f"unknown session {session_id}")
        return {
            "session": session.to_dict(),
            "timeline": self.agent.sessions.timeline(session_id),
            "explain": self.agent.explain(session_id),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "FixPilot"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler signature
        if self.server.verbose:  # type: ignore[attr-defined]
            print(f"[api] {self.address_string()} {format % args}")

    # -- verbs ---------------------------------------------------------
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method: str) -> None:
        app: FixPilotServer = self.server.app  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = b""
        if length:
            if length > MAX_BODY_BYTES:
                self._respond(Response(413, {"error": "payload too large"}))
                return
            raw_body = self.rfile.read(length)
        body: dict[str, Any] = {}
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8", "replace"))
                if not isinstance(body, dict):
                    body = {"value": body}
            except json.JSONDecodeError:
                body = {"raw": raw_body.decode("utf-8", "replace")[:4000]}
        query = urllib.parse.parse_qs(parsed.query)
        token = self.headers.get("X-FixPilot-Token") or (query.get("token", [""])[0])
        request = Request(
            method=method,
            path=parsed.path,
            query=query,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
            raw_body=raw_body,
            remote=self.client_address[0] if self.client_address else "",
            token=token,
        )
        try:
            response = app.dispatch(request)
        except ApiError as exc:
            response = Response(exc.status, {"error": exc.message})
        except KeyError as exc:
            response = Response(404, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - last-resort guard
            response = Response(500, {"error": f"{type(exc).__name__}: {exc}"})
        self._respond(response)

    def _respond(self, response: Response) -> None:
        if response.raw is not None:
            body = response.raw
        elif isinstance(response.payload, str) and response.content_type.startswith("text/"):
            body = response.payload.encode("utf-8")
        else:
            body = json.dumps(response.payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class Server:
    """Lifecycle wrapper used by the CLI and the app entry point."""

    def __init__(self, agent: FixPilotAgent, settings: Settings, *, require_token: bool = True, verbose: bool = False) -> None:
        self.agent = agent
        self.settings = settings
        self.app = FixPilotServer(agent, settings, require_token=require_token)
        self.httpd = ThreadingHTTPServer((settings.host, settings.port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = self.app  # type: ignore[attr-defined]
        self.httpd.verbose = verbose  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    def url(self, host: str | None = None) -> str:
        display_host = host or ("127.0.0.1" if self.settings.host in {"0.0.0.0", ""} else self.settings.host)
        return f"http://{display_host}:{self.settings.port}/?token={self.app.token}"

    def start_background(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fixpilot-http", daemon=True)
        self.thread.start()

    def serve_forever(self, banner: Callable[[str], None] = print) -> None:
        banner(self.banner())
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            banner("\n[fixpilot] shutting down")
        finally:
            self.httpd.server_close()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def banner(self) -> str:
        lan = _lan_ip()
        lines = [
            "",
            "  FixPilot is running.",
            f"  Local:   http://127.0.0.1:{self.settings.port}/?token={self.app.token}",
        ]
        if lan:
            lines.append(f"  Phone:   http://{lan}:{self.settings.port}/?token={self.app.token}")
        lines += [
            f"  Repo:    {self.settings.repo_root}",
            f"  Agent:   {len(self.agent.registry)} skills · model strategy '{self.settings.models.strategy}'",
            "  Press Ctrl-C to stop.",
            "",
        ]
        return "\n".join(lines)


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def build_server(settings: Settings, *, verbose: bool = False, require_token: bool = True) -> Server:
    agent = FixPilotAgent(settings)
    try:
        return Server(agent, settings, require_token=require_token, verbose=verbose)
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
            raise SystemExit(
                f"[fixpilot] cannot bind {settings.host}:{settings.port} ({exc.strerror}). "
                f"Another process is already using that port — stop it or pick another with "
                f"FIXPILOT_PORT=<port>."
            ) from exc
        raise
