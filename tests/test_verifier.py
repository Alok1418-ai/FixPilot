"""Test-output parsing and syntax checks — the honesty layer of 'verified'."""

from __future__ import annotations

import unittest

from fixpilot.core.verifier import build_test_command, parse_test_output, syntax_check

from .support import TempRepo

UNITTEST_OK = """test_known_sku_reports_its_stock (tests.test_inventory.StockLevelTests) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.004s

OK
"""

UNITTEST_FAIL = """test_unknown_sku_has_no_stock (tests.test_inventory.StockLevelTests) ... FAIL

======================================================================
FAIL: test_unknown_sku_has_no_stock (tests.test_inventory.StockLevelTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/srv/shop/tests/test_inventory.py", line 17, in test_unknown_sku_has_no_stock
    self.assertEqual(inventory.stock_level("not-a-real-sku"), 0)
AssertionError: KeyError not raised as expected

----------------------------------------------------------------------
Ran 5 tests in 0.004s

FAILED (failures=2, errors=1)
"""

PYTEST_OUTPUT = """============================= test session starts ==============================
collected 3 items

tests/test_pricing.py F.F                                              [100%]

=================================== FAILURES ===================================
__________________________________ test_empty __________________________________

    def test_empty():
>       assert average_price([]) == 0
E   ZeroDivisionError: division by zero

=========================== short test summary info ============================
FAILED tests/test_pricing.py::test_empty - ZeroDivisionError: division by zero
========================= 2 failed, 1 passed in 0.05s ==========================
"""

JEST_OUTPUT = """FAIL src/price.test.js
  ● empty basket

    expect(received).toBe(expected)

Tests:       1 failed, 2 passed, 3 total
"""

GO_OUTPUT = """--- FAIL: TestAverageEmpty (0.00s)
    price_test.go:14: got NaN, want 0
FAIL
exit status 1
FAIL\tgithub.com/shop/price\t0.012s
"""


class ParserTests(unittest.TestCase):
    def test_unittest_success(self) -> None:
        report = parse_test_output(UNITTEST_OK)
        self.assertEqual(report.framework, "unittest")
        self.assertTrue(report.ok)
        self.assertEqual((report.total, report.failed, report.errors), (5, 0, 0))

    def test_unittest_failure_is_never_mistaken_for_success(self) -> None:
        report = parse_test_output(UNITTEST_FAIL)
        self.assertFalse(report.ok, "a FAILED runner summary must never parse as ok")
        self.assertEqual(report.errors, 1)
        self.assertEqual(report.failed, 2)
        self.assertIn("FAILED", report.summary_line)

    def test_pytest_failure(self) -> None:
        report = parse_test_output(PYTEST_OUTPUT)
        self.assertEqual(report.framework, "pytest")
        self.assertFalse(report.ok)
        self.assertEqual(report.failed, 2)
        self.assertEqual(report.passed, 1)
        self.assertTrue(report.failures)

    def test_jest_and_go(self) -> None:
        jest = parse_test_output(JEST_OUTPUT)
        self.assertFalse(jest.ok)
        self.assertEqual((jest.failed, jest.passed), (1, 2))
        go = parse_test_output(GO_OUTPUT)
        self.assertFalse(go.ok)
        self.assertTrue(go.failures)

    def test_build_test_command_narrows_targets(self) -> None:
        pytest_cmd = build_test_command(["python3", "-m", "pytest", "-q"], ["tests/test_pricing.py::test_empty"], [])
        self.assertIn("tests/test_pricing.py::test_empty", pytest_cmd)
        unittest_cmd = build_test_command(["python3", "-m", "unittest", "discover", "-v"], [], ["tests/test_notes.py"])
        self.assertEqual(unittest_cmd[:4], ["python3", "-m", "unittest", "-v"])
        self.assertIn("tests.test_notes", unittest_cmd)


class SyntaxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_syntax_check_reports_broken_files(self) -> None:
        broken = self.repo.root / "app" / "broken.py"
        broken.write_text("def f(:\n    pass\n", encoding="utf-8")
        results = syntax_check(["app/broken.py", "app/inventory.py"], self.repo.root)
        self.assertFalse(results["app/broken.py"]["ok"])
        self.assertTrue(results["app/inventory.py"]["ok"])


if __name__ == "__main__":
    unittest.main()
