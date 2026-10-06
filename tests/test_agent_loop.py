"""The end-to-end promise: bug report in, verified patch out, with a gate in between."""

from __future__ import annotations

import unittest

from fixpilot.core.agent import FixPilotAgent

from .support import TempRepo


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.agent = FixPilotAgent(self.settings)
        self.target = self.repo.root / "app" / "inventory.py"
        self.baseline = self.target.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_investigation_proposes_but_never_writes(self) -> None:
        result = self.agent.run({"text": self.repo.report("bug-001-critical-stock-lookup.txt")})
        session = result["session"]
        self.assertEqual(session["status"], "awaiting_approval")
        top = session["hypotheses"][0]
        self.assertEqual(top["file"], "app/inventory.py")
        self.assertIn("stock_level", top["symbol"])
        self.assertTrue(session["patch"]["files"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.baseline,
                         "investigation must not touch the working tree")

    def test_approval_then_apply_runs_the_tests_and_verifies(self) -> None:
        result = self.agent.run({"text": self.repo.report("bug-001-critical-stock-lookup.txt")})
        session_id = result["session"]["id"]

        approval = self.agent.approve(session_id, approved=True, note="unit test")
        self.assertTrue(approval["ok"])
        outcome = self.agent.apply_and_verify(session_id)

        self.assertTrue(outcome["verified"], outcome.get("explanation"))
        session = outcome["session"]
        self.assertEqual(session["status"], "verified")
        tests = session["verification"]["tests"]
        self.assertGreaterEqual(tests["passed"], 5)
        self.assertEqual(tests["failed"], 0)
        self.assertNotEqual(self.target.read_text(encoding="utf-8"), self.baseline)

        rollback = self.agent.rollback(session_id, note="unit test rollback")
        self.assertTrue(rollback["ok"], rollback.get("error"))
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.baseline)
        self.assertEqual(self.agent.sessions.get(session_id).status, "rolled_back")

    def test_rejected_patch_is_not_applied(self) -> None:
        result = self.agent.run({"text": self.repo.report("bug-002-average-price-empty.txt")})
        session_id = result["session"]["id"]
        self.agent.approve(session_id, approved=False, note="not yet")
        outcome = self.agent.apply_and_verify(session_id)
        self.assertFalse(outcome["ok"])
        self.assertEqual(self.agent.sessions.get(session_id).status, "rejected")
        self.assertEqual((self.repo.root / "app" / "pricing.py").read_text(encoding="utf-8"),
                         (self.repo.root / "app" / "pricing.py").read_text(encoding="utf-8"))

    def test_vague_report_produces_no_patch(self) -> None:
        result = self.agent.run({"text": "hey, something feels off in the checkout flow, can you take a look?"})
        session = result["session"]
        self.assertFalse(session["patch"].get("text"), "the agent must not fabricate a patch")
        self.assertIn(session["status"], {"investigating", "hypothesis", "failed", "cancelled"})

    def test_session_records_are_replayable(self) -> None:
        result = self.agent.run({"text": self.repo.report("bug-001-critical-stock-lookup.txt")})
        session_id = result["session"]["id"]
        timeline = self.agent.sessions.timeline(session_id)
        self.assertTrue(timeline)
        events = self.agent.sessions.events(session_id)
        self.assertGreater(len(events), 5)
        self.assertEqual(events[0]["kind"], "session.created")
        replayed = self.agent.replay(session_id)
        self.assertIn("events", replayed)
        report = self.agent.report(session_id)
        self.assertIn("Root cause", report["markdown"])
        self.assertTrue(self.agent.answer("where is stock_level defined?")["answer"])

    def test_command_policy_is_exposed_to_the_phone(self) -> None:
        verdict = self.agent.policy.evaluate_string("rm -rf /", cwd=self.settings.repo_root)
        self.assertEqual(verdict.decision, "deny")
        self.assertTrue(self.agent.executor.stats())


if __name__ == "__main__":
    unittest.main()
