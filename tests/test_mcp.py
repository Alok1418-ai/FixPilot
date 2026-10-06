"""The MCP surface: a phone or desktop agent must be able to drive FixPilot headlessly."""

from __future__ import annotations

import json
import os
import sys
import unittest

from fixpilot.core.agent import FixPilotAgent
from fixpilot.mcp import MCPClient, MCPServer, TOOL_SPECS

from .support import REPO_ROOT, TempRepo


class MCPProtocolTests(unittest.TestCase):
    """In-process protocol checks — fast, no subprocess."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.agent = FixPilotAgent(self.repo.settings)
        self.server = MCPServer(self.agent, self.repo.settings)

    def tearDown(self) -> None:
        self.repo.cleanup()

    # -- helpers -------------------------------------------------------
    def call(self, tool: str, arguments: dict) -> dict:
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        )
        self.assertIsNotNone(response)
        self.assertNotIn("error", response)
        return response["result"]

    @staticmethod
    def text(result: dict) -> str:
        return "\n".join(part.get("text", "") for part in result.get("content", []))

    # -- tests ---------------------------------------------------------
    def test_initialize_handshake(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        result = response["result"]
        self.assertEqual(result["serverInfo"]["name"], "fixpilot")
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("confirm=true", result["instructions"])

    def test_tools_list_matches_the_registry(self) -> None:
        response = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        self.assertEqual(set(tools), set(TOOL_SPECS))
        self.assertEqual(len(tools), 13)
        for tool in tools.values():
            self.assertIn("inputSchema", tool)
            self.assertTrue(tool["description"])

    def test_search_and_read_file(self) -> None:
        found = self.text(self.call("fixpilot_search", {"query": "stock_level"}))
        self.assertIn("app/inventory.py", found)
        source = self.text(self.call("fixpilot_read_file", {"path": "app/inventory.py"}))
        self.assertIn("record = INVENTORY[sku]", source)

    def test_policy_check_blocks_destructive_commands(self) -> None:
        body = self.text(self.call("fixpilot_policy_check", {"command": "rm -rf /"}))
        self.assertIn("deny", body)
        self.assertIn("destructive", body)

    def test_missing_required_arguments_are_a_tool_error(self) -> None:
        result = self.call("fixpilot_decide", {"approved": True, "confirm": True})
        self.assertTrue(result["isError"])
        self.assertIn("missing required argument", self.text(result))

    def test_mutations_require_confirm(self) -> None:
        result = self.call("fixpilot_decide", {"id": "sess_missing", "approved": True})
        self.assertTrue(result["isError"])
        self.assertIn("confirm", self.text(result).lower())

    def test_unknown_tool_and_methods_are_reported(self) -> None:
        bad_tool = self.call("fixpilot_nope", {})
        self.assertTrue(bad_tool["isError"])
        unknown = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/punch"})
        self.assertEqual(unknown["error"]["code"], -32601)
        malformed = self.server.handle({"id": 4, "method": "initialize"})
        self.assertEqual(malformed["error"]["code"], -32600)

    def test_full_loop_through_mcp_tools(self) -> None:
        report = self.repo.report("bug-001-critical-stock-lookup.txt")
        session = json.loads(self.text(self.call("fixpilot_investigate", {"text": report})))
        self.assertEqual(session["status"], "awaiting_approval")
        self.assertEqual(session["root_cause"]["file"], "app/inventory.py")
        self.assertIn("stock_level", session["root_cause"]["cause"])
        self.assertTrue(session["patch"]["diff"])
        session_id = session["id"]

        decided = self.call(
            "fixpilot_decide",
            {"id": session_id, "approved": True, "confirm": True, "actor": "phone"},
        )
        self.assertFalse(decided["isError"], self.text(decided))

        applied = json.loads(self.text(self.call("fixpilot_apply", {"id": session_id, "confirm": True})))
        self.assertTrue(applied.get("verified"), applied)
        self.assertEqual(applied["status"], "verified")
        self.assertEqual(applied["verification"], "verified")
        self.assertGreaterEqual(applied["tests"]["passed"], 5)

        digest = json.loads(self.text(self.call("fixpilot_session", {"id": session_id})))
        self.assertEqual(digest["status"], "verified")
        replayed = json.loads(self.text(self.call("fixpilot_replay", {"id": session_id})))
        self.assertTrue(replayed["events"])
        report_md = self.text(self.call("fixpilot_report", {"id": session_id}))
        self.assertIn("Root cause", report_md)

    def test_audit_log_records_tool_calls(self) -> None:
        self.call("fixpilot_policy_check", {"command": "git status"})
        entries = self.agent.audit.tail(20) if hasattr(self.agent.audit, "tail") else None
        if entries is None:  # pragma: no cover - audit API guard
            self.skipTest("AuditLog exposes no tail()")
        self.assertTrue(any(entry.get("tool") == "fixpilot_policy_check" for entry in entries))


class MCPClientTests(unittest.TestCase):
    """Subprocess round trip: the same client code the CLI uses, over real stdio."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.command = [
            sys.executable,
            "-m",
            "fixpilot",
            "--repo",
            str(self.repo.root),
            "mcp",
            "serve",
        ]
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(REPO_ROOT)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_client_can_list_and_call_tools(self) -> None:
        with MCPClient(self.command, env=self.env, timeout=90.0) as client:
            self.assertEqual(client.server_info.get("name"), "fixpilot")
            tools = client.list_tools()
            self.assertEqual(len(tools), len(TOOL_SPECS))
            body = client.call_tool("fixpilot_overview", {})
            self.assertIn("repo", body.lower())

    def test_client_reports_unknown_tools_without_dying(self) -> None:
        with MCPClient(self.command, env=self.env, timeout=90.0) as client:
            with self.assertRaises(RuntimeError):
                client.call_tool("fixpilot_nope", {})


if __name__ == "__main__":
    unittest.main()
