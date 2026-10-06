"""Indexing, symbol lookup and test-command detection."""

from __future__ import annotations

import unittest

from fixpilot.repo.indexer import Indexer
from fixpilot.repo.symbols import is_test_path

from .support import TempRepo, make_context


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_build_indexes_symbols_and_tests(self) -> None:
        index = Indexer(self.settings).build(force=True)
        summary = index.summary()
        stats = summary["stats"]
        self.assertGreaterEqual(stats["files"], 6)
        self.assertGreater(stats["symbols"], 5)
        self.assertIn("app/inventory.py", index.files)

        found = index.find_symbols("stock_level")
        self.assertTrue(found)
        self.assertEqual(found[0].path, "app/inventory.py")

        ctx = make_context(self.settings, text="KeyError in app/inventory.py:18")
        symbol = ctx.symbol_at("app/inventory.py", 18)
        self.assertIsNotNone(symbol, "line 18 is inside stock_level")
        self.assertEqual(symbol.name, "stock_level")

        context = index.context("app/inventory.py", 18, before=2, after=2)
        self.assertTrue(context.get("lines") or context.get("text") or context.get("snippet"),
                        f"context() shape changed: {list(context)}")
        self.assertIn("def stock_level", "\n".join(index.source_lines("app/inventory.py")))

    def test_unittest_is_chosen_when_pytest_is_not_declared(self) -> None:
        index = Indexer(self.settings).build(force=True)
        self.assertEqual(index.test_command[:3], ["python3", "-m", "unittest"])

    def test_pytest_is_chosen_when_the_project_declares_it(self) -> None:
        (self.repo.root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", encoding="utf-8"
        )
        index = Indexer(self.settings).build(force=True)
        self.assertEqual(index.test_command[:3], ["python3", "-m", "pytest"])

    def test_resolve_path_maps_absolute_log_paths(self) -> None:
        ctx = make_context(self.settings, text="KeyError")
        self.assertEqual(ctx.resolve_path("/srv/shop/app/inventory.py"), "app/inventory.py")
        self.assertEqual(ctx.resolve_path("app/inventory.py"), "app/inventory.py")

    def test_risk_signals_are_dicts_with_tags(self) -> None:
        index = Indexer(self.settings).build(force=True)
        facts = index.files["app/notes.py"]
        self.assertTrue(all(isinstance(signal, dict) and "tag" in signal for signal in facts.risk_signals))


class TestPathTests(unittest.TestCase):
    def test_is_test_path(self) -> None:
        for path in ("tests/test_x.py", "app/tests/test_y.py", "x_test.go", "__tests__/a.test.js"):
            self.assertTrue(is_test_path(path), path)
        for path in ("app/inventory.py", "src/index.js", "main.go"):
            self.assertFalse(is_test_path(path), path)


if __name__ == "__main__":
    unittest.main()
