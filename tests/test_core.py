"""The reasoning core: evidence grounding, patch planning and the verification contract."""

from __future__ import annotations

import unittest

from fixpilot.core.agent import FixPilotAgent
from fixpilot.core.verifier import build_test_command, syntax_check
from fixpilot.models.media import InputAdapter
from fixpilot.store import AuditLog

from .support import TempRepo

CASES = {
    "bug-001-critical-stock-lookup.txt": ("app/inventory.py", "stock_level"),
    "bug-002-average-price-empty.txt": ("app/pricing.py", "average_price"),
    "bug-003-quantity-parsing.txt": ("app/notes.py", "parse_line"),
}


class InvestigatorTests(unittest.TestCase):
    """Evidence, not vibes: every hypothesis must point at real code."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.agent = FixPilotAgent(self.repo.settings)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_all_fixture_reports_localise_to_the_right_symbol(self) -> None:
        for report, (expected_path, expected_symbol) in CASES.items():
            with self.subTest(report=report):
                session = self.agent.run({"text": self.repo.report(report)})["session"]
                top = session["hypotheses"][0]
                self.assertEqual(top["file"], expected_path)
                self.assertEqual(top["symbol"], expected_symbol)
                self.assertGreater(top["score"], 0.4)
                self.assertTrue(session["evidence"], "a hypothesis without evidence is a guess")
                self.assertTrue(top["falsifier"], "a claim you cannot falsify is not a hypothesis")

    def test_evidence_covers_more_than_the_stack_trace(self) -> None:
        session = self.agent.run(
            {"text": self.repo.report("bug-001-critical-stock-lookup.txt")}
        )["session"]
        kinds = {item["kind"] for item in session["evidence"]}
        self.assertIn("traceback", kinds)
        self.assertTrue({"test", "source"} & kinds, kinds)

    def test_scores_shrink_when_the_report_is_vague(self) -> None:
        strong = self.agent.run(
            {"text": self.repo.report("bug-001-critical-stock-lookup.txt")}
        )["session"]
        weak = self.agent.run(
            {"text": "sometimes the dashboard looks wrong after a while, not sure when"}
        )["session"]
        best_weak = max([h["score"] for h in weak["hypotheses"]] or [0.0])
        self.assertGreater(strong["hypotheses"][0]["score"], best_weak)

    def test_secrets_from_the_report_never_reach_the_session(self) -> None:
        secret = "sk-live-abcdef1234567890"
        session = self.agent.run(
            {"text": f"FATAL: payment provider rejected the call\nOPENAI_API_KEY={secret}\n"}
        )["session"]
        self.assertNotIn(secret, repr(session), "a pasted token must be redacted at intake")
        self.assertIn("OPENAI_API_KEY", session["input"]["text"], "the label survives so the report stays readable")
        self.assertIn("redacted", " ".join(session["input"]["warnings"]))


class PlannerTests(unittest.TestCase):
    """A patch plan is a proposal with a blast radius, not a text blob."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.agent = FixPilotAgent(self.repo.settings)
        self.session = self.agent.run(
            {"text": self.repo.report("bug-001-critical-stock-lookup.txt")}
        )["session"]

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_plan_carries_risk_verification_and_targets(self) -> None:
        patch = self.session["patch"]
        risk = patch["risk"]
        self.assertIn(risk["level"], {"low", "medium"})
        self.assertTrue(risk["notes"])
        self.assertTrue(risk["behaviour_change"])
        blast = risk["blast_radius"]
        self.assertEqual(blast["changed"], ["app/inventory.py"])
        self.assertIn("tests/test_inventory.py", blast["dependent_tests"])
        self.assertIn("stock_level", blast["callers"])
        self.assertTrue(patch["verification_plan"])
        self.assertEqual(patch["strategies"], ["safe-key-access"])
        self.assertTrue(risk["descriptions"])

    def test_plan_never_touches_sensitive_or_outside_files(self) -> None:
        for path in self.session["patch"]["files"]:
            self.assertNotIn(".env", path)
            self.assertFalse(path.startswith("/"))
            self.assertNotIn("..", path)

    def test_test_command_is_narrowed_to_the_implicated_test(self) -> None:
        patch = self.session["patch"]
        command = build_test_command(
            ["python3", "-m", "unittest", "discover", "-v"],
            patch["test_targets"],
            patch["test_files"],
        )
        self.assertEqual(command[:4], ["python3", "-m", "unittest", "-v"])
        self.assertIn("tests.test_inventory", command)


class VerificationContractTests(unittest.TestCase):
    """`verified` is earned by an executed, passing test run — nothing else."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.agent = FixPilotAgent(self.repo.settings)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_the_loop_refines_until_the_suite_passes(self) -> None:
        session_id = self.agent.run(
            {"text": self.repo.report("bug-003-quantity-parsing.txt")}
        )["session"]["id"]
        self.agent.approve(session_id, approved=True)
        outcome = self.agent.apply_and_verify(session_id)
        self.assertTrue(outcome["verified"], outcome)
        final = self.agent.sessions.get(session_id)
        self.assertEqual(final.verification["tests"]["failed"], 0)
        self.assertGreater(final.verification["tests"]["passed"], 0)
        self.assertTrue(final.tests, "the raw test output must be kept for the report")

    def test_a_patch_that_cannot_pass_verification_is_rolled_back(self) -> None:
        session_id = self.agent.run(
            {"text": self.repo.report("bug-001-critical-stock-lookup.txt")}
        )["session"]["id"]
        original = (self.repo.root / "app" / "inventory.py").read_text(encoding="utf-8")

        # An unrelated, permanently failing test: no candidate can satisfy it, so the
        # agent must exhaust refinement and restore the tree instead of leaving a
        # half-verified patch behind.
        failing = self.repo.root / "tests" / "test_inventory.py"
        failing.write_text(
            failing.read_text(encoding="utf-8")
            + "\n\nclass UnrelatedFailureTests(unittest.TestCase):\n"
            "    def test_environment_is_broken(self):\n"
            "        self.fail('simulated unrelated failure')\n"
            "    def test_second_failure_keeps_it_failing(self):\n"
            "        self.fail('simulated unrelated failure')\n",
            encoding="utf-8",
        )

        self.agent.approve(session_id, approved=True)
        outcome = self.agent.apply_and_verify(session_id)

        self.assertFalse(outcome["verified"], outcome)
        self.assertTrue(outcome.get("rolled_back"), outcome)
        self.assertEqual(self.agent.sessions.get(session_id).status, "rolled_back")
        self.assertEqual(
            (self.repo.root / "app" / "inventory.py").read_text(encoding="utf-8"),
            original,
            "a failed verification must not leave the patch applied",
        )

    def test_syntax_gate_rejects_unparseable_files(self) -> None:
        broken = self.repo.root / "app" / "inventory.py"
        broken.write_text("def nope(:\n    pass\n", encoding="utf-8")
        results = syntax_check(["app/inventory.py", "app/pricing.py"], self.repo.root)
        self.assertFalse(results["app/inventory.py"]["ok"])
        self.assertIn("line 1", results["app/inventory.py"]["error"])
        self.assertTrue(results["app/pricing.py"]["ok"])


class IntakeTests(unittest.TestCase):
    def test_screenshot_style_report_is_accepted(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        adapter = InputAdapter(repo.settings, AuditLog(repo.settings.audit_log))
        ingested = adapter.from_text("checkout total is wrong\n[image attached: checkout-basket.png]")
        self.assertEqual(ingested.channel, "text")
        self.assertIn("checkout total", ingested.text)


if __name__ == "__main__":
    unittest.main()
