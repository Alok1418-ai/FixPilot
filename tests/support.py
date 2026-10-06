"""Shared helpers: throwaway repository copies and settings for tests."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fixpilot.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PROJECT = REPO_ROOT / "examples" / "sample-project"
REPORTS = SAMPLE_PROJECT / "reports"


class TempRepo:
    """A disposable copy of the sample project with its own ``.fixpilot`` state."""

    def __init__(self, source: Path | None = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="fixpilot-test-"))
        source = source or SAMPLE_PROJECT
        shutil.copytree(source, self.root, dirs_exist_ok=True)
        for junk in self.root.rglob(".fixpilot"):
            shutil.rmtree(junk, ignore_errors=True)
        for junk in self.root.rglob("__pycache__"):
            shutil.rmtree(junk, ignore_errors=True)

    @property
    def settings(self) -> Settings:
        return Settings(repo_root=self.root)

    def report(self, name: str) -> str:
        return (REPORTS / name).read_text(encoding="utf-8")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def sample_settings() -> Settings:
    return Settings(repo_root=SAMPLE_PROJECT)


def make_context(settings: Settings, *, text: str = "", signals: tuple[str, ...] = (), scratch: dict | None = None, emit=None):
    """A real SkillContext over a throwaway repo — used by skill-level tests."""
    from fixpilot.core.verifier import Verifier
    from fixpilot.memory.lessons import LessonStore
    from fixpilot.memory.project import ProjectMemoryStore
    from fixpilot.models.media import IngestedInput, InputAdapter, LogSignal
    from fixpilot.models.providers import build_providers
    from fixpilot.models.router import ModelRouter
    from fixpilot.repo.githistory import GitHistory
    from fixpilot.repo.graph import CodeGraph
    from fixpilot.repo.indexer import Indexer
    from fixpilot.security.executor import SandboxExecutor
    from fixpilot.security.policy import CommandPolicy
    from fixpilot.skills.base import SkillContext
    from fixpilot.store import AuditLog

    audit = AuditLog(settings.audit_log)
    index = Indexer(settings).build(force=True)
    adapter = InputAdapter(settings, audit)
    ingested = adapter.from_text(text) if text else IngestedInput(id="in_test", channel="text", text="")
    ingested.signals = [
        LogSignal(kind=kind, value=kind, path="app/inventory.py" if kind == "frame" else "", lineno=18 if kind == "frame" else 0)
        for kind in signals
    ] or ingested.signals
    ctx = SkillContext(
        settings=settings,
        index=index,
        graph=CodeGraph(index),
        history=GitHistory(settings.repo_root),
        executor=SandboxExecutor(settings, CommandPolicy(settings), audit),
        router=ModelRouter(settings, build_providers(settings), audit),
        memory=ProjectMemoryStore(settings),
        lessons=LessonStore(settings),
        ingested=ingested,
        session=None,
        emit=emit,
        scratch=scratch if scratch is not None else {},
    )
    return ctx
