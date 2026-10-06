"""Skill registry, applicability and hypothesis ranking."""

from __future__ import annotations

import unittest

from fixpilot.skills import build_registry
from fixpilot.skills.base import (
    CATEGORY_PRIORS,
    Evidence,
    Hypothesis,
    dedupe_hypotheses,
    rank_hypotheses,
    text_similarity,
)

from .support import TempRepo, make_context


class FakeSignal:
    def __init__(self, kind: str) -> None:
        self.kind = kind


class FakeIngested:
    def __init__(self, text: str = "", kinds: tuple[str, ...] = (), intent: str = "") -> None:
        self.text = text
        self.signals = [FakeSignal(kind) for kind in kinds]
        self.hint_intent = intent


class FakeStats:
    languages = {"python": 3}


class FakeIndex:
    stats = FakeStats()
    files: dict = {}
    symbols: dict = {}


class FakeContext:
    def __init__(self, text: str = "", kinds: tuple[str, ...] = (), scratch=None) -> None:
        self.ingested = FakeIngested(text, kinds)
        self.index = FakeIndex()
        self.scratch = scratch if scratch is not None else {}

    @property
    def text(self) -> str:
        return self.ingested.text

    @property
    def signals(self):
        return self.ingested.signals


class RegistryTests(unittest.TestCase):
    def test_builtin_registry_is_stable(self) -> None:
        registry = build_registry()
        names = [skill.name for skill in registry.all()]
        self.assertEqual(len(registry), 11)
        self.assertIn("traceback_localizer", names)
        self.assertIn("patch_author", names)
        self.assertTrue(registry.get("patch_author").post_ranking)
        self.assertEqual(len(registry.summary()), 11)

    def test_register_rejects_duplicates_unless_replaced(self) -> None:
        registry = build_registry()
        skill = registry.get("traceback_localizer")
        with self.assertRaises(ValueError):
            registry.register(skill)
        registry.register(skill, replace=True)
        self.assertEqual(len(registry), 11)

    def test_traceback_skill_only_applies_with_frames(self) -> None:
        skill = build_registry().get("traceback_localizer")
        self.assertFalse(skill.applies(FakeContext("KeyError in the basket", kinds=["level"])))
        self.assertTrue(skill.applies(FakeContext("Traceback (most recent call last)", kinds=["frame"])))
        self.assertGreater(
            skill.score(FakeContext("Traceback ... KeyError", kinds=["frame"])),
            skill.score(FakeContext("KeyError", kinds=["level"])),
        )

    def test_patch_author_never_runs_in_the_first_pass(self) -> None:
        repo = TempRepo()
        try:
            ctx = make_context(repo.settings, text="Traceback (most recent call last):\n  File \"app/inventory.py\", line 18, in stock_level\n    record = INVENTORY[sku]\nKeyError: 'x'")
            selected = [skill.name for skill, _ in build_registry().select(ctx)]
        finally:
            repo.cleanup()
        self.assertNotIn("patch_author", selected)
        self.assertIn("traceback_localizer", selected)


class RankingTests(unittest.TestCase):
    def test_category_priors_cover_the_strategies_that_matter(self) -> None:
        self.assertGreater(CATEGORY_PRIORS["exception"], CATEGORY_PRIORS["ui"])
        self.assertIn("null-safety", CATEGORY_PRIORS)

    def test_dedupe_merges_identical_causes(self) -> None:
        a = Hypothesis(cause="KeyError on unknown sku", category="exception", file="app/inventory.py", strategy="safe-key-access")
        b = Hypothesis(cause="KeyError on unknown sku", category="exception", file="app/inventory.py", confidence=0.7, evidence_ids=["ev_2"])
        merged = dedupe_hypotheses([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, 0.7)
        self.assertEqual(merged[0].evidence_ids, ["ev_2"])
        self.assertEqual(merged[0].strategy, "safe-key-access")

    def test_ranking_prefers_evidence_backed_hypotheses(self) -> None:
        strong = Hypothesis(cause="A", category="exception", file="app/inventory.py", confidence=0.6, evidence_ids=["ev_1", "ev_2"])
        weak = Hypothesis(cause="B", category="unknown", file="app/inventory.py", confidence=0.6, evidence_ids=[])
        evidence = [
            Evidence(kind="traceback", claim="frame", id="ev_1", confidence=0.9),
            Evidence(kind="source", claim="line", id="ev_2", confidence=0.8),
        ]
        ranked = rank_hypotheses([weak, strong], FakeContext(scratch={"evidence": evidence}))
        self.assertIs(ranked[0], strong)
        self.assertGreater(strong.ranked_score, weak.ranked_score)

    def test_text_similarity_is_symmetric(self) -> None:
        value = text_similarity("KeyError on unknown sku", "unknown sku raises KeyError")
        self.assertEqual(value, text_similarity("unknown sku raises KeyError", "KeyError on unknown sku"))
        self.assertGreater(value, 0.3)


if __name__ == "__main__":
    unittest.main()
