"""FixPilot command line: the same agent the phone drives, from a terminal.

    python3 -m fixpilot serve                 # start the phone-facing server
    python3 -m fixpilot index                 # build/refresh the codebase index
    python3 -m fixpilot fix "traceback text…" # run the whole loop, ask before applying
    python3 -m fixpilot fix --report-file bug.txt --apply
    python3 -m fixpilot ask "where is stock_level defined?"
    python3 -m fixpilot sessions
    python3 -m fixpilot officekit worker      # run queued phone commands
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import get_settings, load_dotenv
from .core.agent import FixPilotAgent
from .core.narrator import explain_cause, explain_patch, explain_verification, spoken_summary
from .memory.sessions import Session
from .officekit import OfficeKitBridge
from .util import excerpt


def _fmt(status: str) -> str:
    marks = {
        "verified": "✅",
        "failed": "❌",
        "rolled_back": "↩︎",
        "awaiting_approval": "⏸",
        "patch_proposed": "📝",
    }
    return f"{marks.get(status, '•')} {status.replace('_', ' ')}"


def _print_overview(agent: FixPilotAgent) -> None:
    overview = agent.overview()
    repo = overview["repo"]
    stats = repo["stats"]
    print(f"FixPilot {overview['product']['version']} — {overview['product']['tagline']}")
    print(f"  repo        {repo['root']}")
    print(f"  git         {repo['git_branch'] or 'n/a'} @ {(repo['git_head'] or '')[:10]}"
          f"{' (dirty)' if repo['dirty'] else ''}")
    print(f"  index       {stats['files']} files, {stats['symbols']} symbols, {stats['test_files']} test files")
    print(f"  tests       {' '.join(repo['test_command']) or 'no test command detected'}")
    print(f"  models      strategy={overview['models']['strategy']}, cloud={'yes' if overview['models']['cloud_allowed'] else 'no'}")
    available = [p for p in overview["models"]["profiles"] if p["available"]]
    print(f"  available   {', '.join(p['model'] for p in available) or 'deterministic core only'}")
    print(f"  skills      {len(overview['skills'])} registered")
    print(f"  memory      {overview['memory']['facts']} facts · lessons {overview['lessons']['total']} "
          f"({overview['lessons']['verified']} verified)")


def _print_session(session: Session, agent: FixPilotAgent, *, show_diff: bool = True) -> None:
    print(f"\nSession {session.id} — {_fmt(session.status)}")
    if session.status == "awaiting_approval":
        print("  (proposed patch — nothing has been written to your files yet)")
    print(f"  {session.title[:100]}")
    if session.understanding.get("summary"):
        print(f"  understood: {excerpt(session.understanding['summary'], 160)}")
    top = (session.hypotheses or [{}])[0] if session.hypotheses else {}
    if top:
        print(f"  root cause: {excerpt(top.get('cause', ''), 160)}")
        print(f"  confidence: {int(float(top.get('score') or 0) * 100)}% · category {top.get('category')} · "
              f"strategy {top.get('strategy')}")
    if session.evidence:
        print("  evidence:")
        for item in session.evidence[:5]:
            where = ""
            if item.get("path"):
                where = f" ({item['path']}:{item['lineno']})" if item.get("lineno") else f" ({item['path']})"
            print(f"    - [{item['kind']}] {excerpt(item['claim'], 120)}{where}")
    if session.patch.get("summary"):
        print(f"  patch: {excerpt(session.patch['summary'], 160)}")
    if show_diff and session.patch.get("text"):
        print()
        for line in session.patch["text"].splitlines()[:60]:
            print(f"    {line}")
    if session.verification:
        print(f"  verification: {session.verification.get('status')} "
              f"(confidence {session.verification.get('confidence')})")
        tests = session.verification.get("tests") or {}
        if tests:
            print(f"    tests: {tests.get('passed', 0)} passed · {tests.get('failed', 0)} failed · "
                  f"{tests.get('errors', 0)} errors ({tests.get('framework')})")
        for reason in (session.verification.get("reasons") or [])[:4]:
            print(f"    - {excerpt(reason, 150)}")
    print(f"\n  spoken: {spoken_summary(session)}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace, settings) -> int:
    from .api.server import build_server

    server = build_server(settings, verbose=args.verbose, require_token=not args.no_auth)
    if args.background:
        server.start_background()
        print(server.banner())
        return 0
    server.serve_forever()
    return 0


def cmd_index(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    summary = agent.refresh_index(force=args.force)
    print(json.dumps(summary, indent=2) if args.json else _index_text(summary))
    return 0


def _index_text(summary: dict) -> str:
    stats = summary["stats"]
    lines = [
        f"indexed {stats['files']} files ({stats['source_files']} source, {stats['test_files']} test) "
        f"in {stats['indexed_ms']}ms",
        f"symbols:  {stats['symbols']}",
        f"languages: {', '.join(f'{k}×{v}' for k, v in list(stats['languages'].items())[:8])}",
        f"conventions: {', '.join(summary['conventions'][:6]) or 'none detected'}",
    ]
    return "\n".join(lines)


def cmd_fix(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    text = args.report or ""
    if args.report_file:
        text = Path(args.report_file).read_text(encoding="utf-8", errors="replace")
    payload: dict = {"text": text}
    if args.voice:
        payload = {"transcript": text}
    if not text.strip():
        print("Provide a report: fix \"<text>\" or --report-file path", file=sys.stderr)
        return 2

    print(f"Investigating with {len(agent.registry)} skills…")
    result = agent.run(payload, cost_budget=args.budget)
    session = Session.from_dict(result["session"])
    _print_session(session, agent, show_diff=not args.no_diff)

    if not session.patch.get("text"):
        print("\nNo safe patch was produced for this root cause.")
        return 1

    approved = args.apply
    if not approved and sys.stdin.isatty() and not args.no_prompt:
        answer = input("\nApply this patch and run the tests? [y/N] ").strip().lower()
        approved = answer in {"y", "yes"}
    if not approved:
        print("\nNot applied. Review the diff, then re-run with --apply or approve from the phone.")
        return 0

    agent.approve(session.id, approved=True, note="approved from CLI")
    outcome = agent.apply_and_verify(session.id)
    final = Session.from_dict(outcome["session"])
    print()
    print(explain_verification(final.verification or {}))
    print(f"\n{_fmt(final.status)}")
    if final.patch.get("files"):
        print(f"files: {', '.join(final.patch['files'])}")
    if outcome.get("rolled_back"):
        print("The patch was rolled back automatically — the working tree is unchanged.")
    return 0 if outcome.get("verified") else 1


def cmd_ask(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    print(agent.answer(args.question)["answer"])
    return 0


def cmd_sessions(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    if args.action in {"list", "ls"}:
        for item in agent.list_sessions(limit=args.limit):
            print(f"{item['id']}  {_fmt(item['status']):<22} {item['channel']:<7} {excerpt(item['title'], 70)}")
        return 0
    if not args.id:
        print("Provide a session id (see `fixpilot sessions list`)", file=sys.stderr)
        return 2
    session = agent.sessions.get(args.id)
    if session is None:
        print(f"unknown session {args.id}", file=sys.stderr)
        return 1
    if args.action == "show":
        _print_session(session, agent)
    elif args.action == "replay":
        for event in agent.sessions.events(args.id):
            print(f"{event['seq']:>3} {event['ts']} {event['kind']:<26} {excerpt(json.dumps(event['payload']), 100)}")
    elif args.action == "timeline":
        for item in agent.sessions.timeline(args.id):
            print(f"{item['seq']:>3} {item['label']:<24} {excerpt(item['detail'], 90)}")
    elif args.action == "report":
        print(agent.report(args.id)["markdown"])
    elif args.action == "rollback":
        print(json.dumps(agent.rollback(args.id), indent=2))
    elif args.action == "diff":
        print(agent.diff_preview(args.id)["patch"])
    return 0


def cmd_skills(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    for skill in agent.registry.summary():
        print(f"{skill['name']:<26} p{skill['priority']:<3} {skill['cost']:<7} {skill['description']}")
    return 0


def cmd_models(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    info = agent.router.describe()
    print(f"strategy: {info['strategy']} · cloud escalation: {'allowed' if info['cloud_allowed'] else 'disabled'}")
    for profile in info["profiles"]:
        status = {True: "available", False: "unavailable", None: "unknown"}[profile["available"]]
        usage = profile["usage"]
        print(
            f"  {profile['model']:<28} {status:<12} {'local ' if profile['local'] else 'cloud '}"
            f"caps={','.join(profile['capabilities'])} calls={usage['calls']} avg={usage['avg_latency_ms']}ms"
        )
    return 0


def cmd_memory(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    print(json.dumps(agent.memory.load().summary(), indent=2))
    return 0


def cmd_lessons(args: argparse.Namespace, settings) -> int:
    agent = FixPilotAgent(settings)
    summary = agent.lessons.summary()
    print(f"{summary['total']} lessons ({summary['verified']} verified, {summary['reused']} reuses)")
    for lesson in summary["recent"]:
        print(f"  [{lesson['outcome']}] {excerpt(lesson['symptom'], 80)} → {excerpt(lesson['root_cause'], 90)}")
    return 0


def cmd_officekit(args: argparse.Namespace, settings) -> int:
    bridge = OfficeKitBridge(settings)
    if args.action == "pair":
        print(json.dumps(bridge.pair(device_name=args.name or "iQOO phone"), indent=2))
    elif args.action == "list":
        print(json.dumps(bridge.describe(), indent=2))
    elif args.action == "queue":
        print(json.dumps(bridge.enqueue(kind=args.kind, payload={"command": args.job_command}), indent=2))
    elif args.action == "worker":
        from .officekit import JOB_KINDS

        agent = FixPilotAgent(settings)

        def handle(job: dict) -> dict:
            kind = job.get("kind")
            print(f"  → job {job['id']} ({kind})")
            if kind == "run_tests":
                report, result = agent.verifier.run_tests(agent.index)
                return {"ok": result.ok, "summary": report.render(), "exit_code": result.exit_code}
            if kind in {"run_command", "verify"}:
                command = (job.get("payload") or {}).get("command", "")
                result = agent.executor.run_string(command, cwd=settings.repo_root, approved=True, purpose="relayed phone command")
                return {"ok": result.ok, "exit_code": result.exit_code, "stdout": result.stdout[-4000:], "error": result.error}
            if kind == "sync_repo":
                return {"ok": True, "summary": agent.refresh_index(force=True)["stats"]}
            if kind == "checkpoint":
                return {"ok": True, "dirty": agent.history.is_dirty(), "head": agent.history.head()}
            return {"ok": False, "error": f"job kind {kind!r} is not handled by this worker", "kinds": list(JOB_KINDS)}

        print("Office Kit worker started — Ctrl-C to stop.")
        print(json.dumps(bridge.run_worker(on_job=handle), indent=2))
    return 0


def cmd_mcp(args: argparse.Namespace, settings) -> int:
    from .mcp import MCPClient, MCPServer, TOOL_SPECS

    if args.action == "serve":
        server = MCPServer(FixPilotAgent(settings), settings)
        print(f"[fixpilot] MCP server on stdio · {len(TOOL_SPECS)} tools", file=sys.stderr)
        return server.serve_stdio()
    if args.action == "tools":
        if args.json:
            print(json.dumps(MCPServer(FixPilotAgent(settings), settings).tool_list(), indent=2))
            return 0
        for name, (description, schema) in TOOL_SPECS.items():
            required = ", ".join(schema.get("required", [])) or "no required fields"
            print(f"{name:<30} {description.splitlines()[0]}")
            print(f"{'':<30} args: {required}")
        return 0
    if args.action == "call":
        tool = args.tool or args.positional_tool
        if not tool:
            print("mcp call <tool> [json-arguments]", file=sys.stderr)
            return 2
        server = MCPServer(FixPilotAgent(settings), settings)
        result = server.call_tool(tool, json.loads(args.args or args.positional_args or "{}"))
        for part in result.get("content", []):
            print(part.get("text", ""))
        return 1 if result.get("isError") else 0
    if args.action == "client":
        if not args.server_command:
            print("mcp client --server-cmd '<server command>' [--list | --tool name --args '{}']", file=sys.stderr)
            return 2
        with MCPClient(args.server_command.split()) as client:
            print(f"[fixpilot] connected to {client.server_info.get('name', 'unknown server')} "
                  f"{client.server_info.get('version', '')}".strip())
            requested = args.tool or args.positional_tool
            if args.list or not requested:
                for tool in client.list_tools():
                    print(f"  {tool['name']:<32} {tool.get('description', '').splitlines()[0]}")
                return 0
            print(client.call_tool(requested, json.loads(args.args or args.positional_args or "{}")))
        return 0
    print(f"unknown mcp action {args.action!r}", file=sys.stderr)
    return 2


def cmd_status(args: argparse.Namespace, settings) -> int:
    _print_overview(FixPilotAgent(settings))
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fixpilot", description="Phone-first autonomous debugging agent.")
    parser.add_argument("--version", action="version", version=f"FixPilot {__version__}")
    parser.add_argument("--repo", help="repository to operate on (default: current directory)")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the phone-facing web server")
    serve.add_argument("--background", action="store_true", help="run the server in a background thread")
    serve.add_argument("--no-auth", action="store_true", help="disable the mutation token (local development only)")
    serve.add_argument("--verbose", action="store_true", help="log every request")
    serve.set_defaults(func=cmd_serve)

    index = sub.add_parser("index", help="build or refresh the codebase index")
    index.add_argument("--force", action="store_true", default=True)
    index.add_argument("--json", action="store_true")
    index.set_defaults(func=cmd_index)

    fix = sub.add_parser("fix", help="investigate a bug report and propose a patch")
    fix.add_argument("report", nargs="?", help="the bug report text (traceback, description, log)")
    fix.add_argument("--report-file", help="read the report from a file instead")
    fix.add_argument("--voice", action="store_true", help="treat the input as a speech transcript")
    fix.add_argument("--apply", action="store_true", help="apply and verify without prompting")
    fix.add_argument("--no-prompt", action="store_true", help="never prompt interactively")
    fix.add_argument("--no-diff", action="store_true", help="do not print the diff")
    fix.add_argument("--budget", choices=["fast", "normal", "deep"], default="deep")
    fix.set_defaults(func=cmd_fix)

    ask = sub.add_parser("ask", help="ask a question about the repository")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    sessions = sub.add_parser("sessions", help="inspect debugging sessions")
    sessions.add_argument("action", choices=["list", "ls", "show", "replay", "timeline", "report", "rollback", "diff"], nargs="?", default="list")
    sessions.add_argument("id", nargs="?", help="session id")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.set_defaults(func=cmd_sessions)

    skills = sub.add_parser("skills", help="list registered developer skills")
    skills.set_defaults(func=cmd_skills)

    models = sub.add_parser("models", help="show model routing and availability")
    models.set_defaults(func=cmd_models)

    memory = sub.add_parser("memory", help="show project memory")
    memory.set_defaults(func=cmd_memory)

    lessons = sub.add_parser("lessons", help="show lessons learned in this repository")
    lessons.set_defaults(func=cmd_lessons)

    officekit = sub.add_parser("officekit", help="iQOO Office Kit bridge: pairing, queue, worker")
    officekit.add_argument("action", choices=["pair", "list", "queue", "worker"], nargs="?", default="list")
    officekit.add_argument("--name", help="device name to pair")
    officekit.add_argument("--kind", default="run_command", help="job kind to enqueue")
    officekit.add_argument("--run", dest="job_command", default="", help="command payload for queued jobs")
    officekit.set_defaults(func=cmd_officekit)

    mcp = sub.add_parser("mcp", help="MCP tools: serve FixPilot to an editor, or call another tool server")
    mcp.add_argument("action", choices=["serve", "tools", "call", "client"], nargs="?", default="tools")
    mcp.add_argument("positional_tool", nargs="?", help="tool name for `call` / `client`")
    mcp.add_argument("positional_args", nargs="?", help="JSON arguments for the tool")
    mcp.add_argument("--tool", default="", help="tool name (alternative to the positional)")
    mcp.add_argument("--args", default="", help="JSON arguments (alternative to the positional)")
    mcp.add_argument("--json", action="store_true", help="print tool schemas as JSON")
    mcp.add_argument("--server-cmd", dest="server_command", default="", help="for `client`: the MCP server command to spawn")
    mcp.add_argument("--list", action="store_true", help="for `client`: list the remote tools and exit")
    mcp.set_defaults(func=cmd_mcp)

    status = sub.add_parser("status", help="one-screen overview of the repo and runtime")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    settings = get_settings(refresh=True)
    if args.repo:
        settings = get_settings(refresh=True)
        settings.repo_root = Path(args.repo).resolve()
        settings.data_dir = settings.repo_root / ".fixpilot"
        settings.ensure_dirs()
    try:
        return args.func(args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
