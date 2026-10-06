"""Skill registry assembly.

Built-in skills are registered here; additional skills can be loaded from an
importable package path via ``FIXPILOT_SKILLS_PATH`` (comma separated), which is
how a team ships its own house rules without forking FixPilot.
"""

from __future__ import annotations

import os

from .base import (
    Evidence,
    Hypothesis,
    Skill,
    SkillContext,
    SkillRegistry,
    SkillResult,
    dedupe_hypotheses,
    rank_hypotheses,
)
from .dependencies import DependencyDoctor
from .history import FlakyDetector, GitForensics
from .localization import LogPatternAnalyzer, StaticSmellScanner, TracebackLocalizer, UiScreenshotSkill
from .patching import PatchAuthor
from .verification import ReproBuilder, ReproHint, TestLocator

BUILTIN_SKILLS: tuple[type[Skill], ...] = (
    TracebackLocalizer,
    TestLocator,
    LogPatternAnalyzer,
    GitForensics,
    StaticSmellScanner,
    DependencyDoctor,
    FlakyDetector,
    ReproBuilder,
    UiScreenshotSkill,
    PatchAuthor,
    ReproHint,
)


def build_registry(extra_paths: list[str] | None = None) -> SkillRegistry:
    registry = SkillRegistry()
    for skill in BUILTIN_SKILLS:
        registry.register(skill)
    paths = extra_paths
    if paths is None:
        env = os.environ.get("FIXPILOT_SKILLS_PATH", "")
        paths = [part.strip() for part in env.split(",") if part.strip()]
    for path in paths or []:
        try:
            registry.discover(path)
        except Exception:  # pragma: no cover - third-party skill must not break startup
            continue
    return registry


_REGISTRY: SkillRegistry | None = None


def get_registry(refresh: bool = False) -> SkillRegistry:
    global _REGISTRY
    if _REGISTRY is None or refresh:
        _REGISTRY = build_registry()
    return _REGISTRY


__all__ = [
    "BUILTIN_SKILLS",
    "Evidence",
    "Hypothesis",
    "Skill",
    "SkillContext",
    "SkillRegistry",
    "SkillResult",
    "build_registry",
    "dedupe_hypotheses",
    "get_registry",
    "rank_hypotheses",
]
