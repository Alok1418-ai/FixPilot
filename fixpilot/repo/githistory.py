"""Git history as first-class evidence.

Bugs are rarely random: they cluster in churn hotspots, in recently rewritten
code, and in the commits that "fixed" something the previous week.  FixPilot
reads history to rank hypotheses and to explain *why* a line is wrong.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..util import clean_text, excerpt


@dataclass(slots=True)
class Commit:
    sha: str
    author: str
    date: str
    subject: str
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sha": self.sha[:10],
            "author": self.author,
            "date": self.date,
            "subject": self.subject,
            "files": self.files[:12],
        }


@dataclass(slots=True)
class BlameLine:
    path: str
    lineno: int
    sha: str
    author: str
    date: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "lineno": self.lineno,
            "sha": self.sha[:10],
            "author": self.author,
            "date": self.date,
            "summary": self.summary,
        }


class GitHistory:
    """Thin, defensive wrapper around the ``git`` CLI."""

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled and (root / ".git").exists()
        self._log_cache: list[Commit] | None = None

    # -- primitives ----------------------------------------------------
    def run(self, args: list[str], timeout: int = 25) -> str:
        if not self.enabled:
            return ""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if proc.returncode != 0 and not proc.stdout:
            return ""
        return proc.stdout

    def is_dirty(self) -> bool:
        return bool(self.run(["status", "--porcelain"]).strip())

    def head(self) -> str:
        return self.run(["rev-parse", "--short", "HEAD"]).strip()

    def branch(self) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()

    def current_diff(self) -> str:
        return self.run(["diff", "--unified=3", "HEAD"])

    # -- history -------------------------------------------------------
    def log(self, limit: int = 60, path: str | None = None) -> list[Commit]:
        if self._log_cache is None or path:
            args = ["log", f"-{max(1, limit)}", "--date=short", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e"]
            if path:
                args += ["--", path]
            raw = self.run(args)
            self._commits = self._parse_log(raw)  # type: ignore[attr-defined]
            if path:
                return self._commits  # type: ignore[attr-defined]
            self._log_cache = self._commits  # type: ignore[attr-defined]
        return self._log_cache or []

    @staticmethod
    def _parse_log(raw: str) -> list[Commit]:
        commits: list[Commit] = []
        for chunk in raw.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            parts = chunk.split("\x1f")
            if len(parts) < 4:
                continue
            commits.append(Commit(sha=parts[0], author=parts[1], date=parts[2], subject=parts[3]))
        return commits

    def commits_touching(self, path: str, limit: int = 5) -> list[Commit]:
        return self.log(limit=limit, path=path)

    def churn(self, limit: int = 400) -> dict[str, int]:
        """Commit count per file across recent history (hotspot input)."""
        raw = self.run(["log", f"-{limit}", "--name-only", "--pretty=format:%x1e"])
        counts: dict[str, int] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line == "\x1e" or "/" not in line and "." not in line:
                continue
            counts[line] = counts.get(line, 0) + 1
        return counts

    def hotspots(self, limit: int = 12) -> list[dict]:
        counts = self.churn()
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return [{"path": path, "commits": count} for path, count in top]

    def blame_line(self, path: str, lineno: int) -> BlameLine | None:
        raw = self.run(["blame", "-L", f"{lineno},{lineno}", "--porcelain", "--", path])
        if not raw.strip():
            return None
        sha = ""
        author = ""
        date = ""
        summary = ""
        for line in raw.splitlines():
            if not sha and re.match(r"^[0-9a-f]{7,40} ", line):
                sha = line.split(" ")[0]
            elif line.startswith("author "):
                author = line[len("author "):]
            elif line.startswith("author-time "):
                try:
                    import datetime

                    date = datetime.datetime.fromtimestamp(int(line.split(" ")[1]), datetime.timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, IndexError):
                    date = ""
            elif line.startswith("summary "):
                summary = line[len("summary "):]
        if not sha:
            return None
        return BlameLine(path=path, lineno=lineno, sha=sha, author=author, date=date, summary=summary)

    def log_for_line(self, path: str, lineno: int, limit: int = 5) -> list[Commit]:
        """Commits whose diff touched a given line (via ``git log -L``)."""
        raw = self.run(["log", "-L", f"{lineno},{lineno}:{path}", "-s", f"-{limit}", "--date=short", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e"], timeout=40)
        return self._parse_log(raw)

    def pickaxe(self, needle: str, limit: int = 5) -> list[Commit]:
        """Find commits that added/removed a literal string (symbol, magic value)."""
        if not needle or len(needle) < 3:
            return []
        raw = self.run(
            ["log", f"-{limit}", "-S", needle, "--date=short", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e"],
            timeout=40,
        )
        return self._parse_log(raw)

    def recent_regressions(self, limit: int = 8) -> list[Commit]:
        """Commits whose message *sounds* like a botched or reverted fix."""
        pattern = re.compile(r"\b(revert|hotfix|regress|broke|broken|rollback|fix\s*fix|oops)\b", re.I)
        return [commit for commit in self.log(limit=200) if pattern.search(commit.subject)][:limit]

    def diff_of(self, sha: str, max_bytes: int = 20_000) -> str:
        return clean_text(self.run(["show", "--unified=2", "--stat", sha], timeout=30), limit=max_bytes)

    def changed_files(self, sha: str) -> list[str]:
        raw = self.run(["show", "--pretty=format:", "--name-only", sha])
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def describe(self) -> dict:
        if not self.enabled:
            return {"is_git": False}
        return {
            "is_git": True,
            "head": self.head(),
            "branch": self.branch(),
            "dirty": self.is_dirty(),
            "commits_indexed": len(self.log(limit=60)),
            "recent": [c.to_dict() for c in self.log(limit=8)],
            "regression_smells": [c.to_dict() for c in self.recent_regressions(limit=5)],
            "hotspots": self.hotspots(limit=8),
        }

    def evidence_for_line(self, path: str, lineno: int) -> dict:
        """Compact, human-readable history evidence for a suspicious line."""
        blame = self.blame_line(path, lineno)
        payload: dict = {"path": path, "lineno": lineno}
        if blame:
            payload["blame"] = blame.to_dict()
            payload["summary"] = (
                f"{path}:{lineno} last changed {blame.date or 'unknown date'} by {blame.author or 'unknown'} "
                f"in {blame.sha[:8]} — “{excerpt(blame.summary, 90)}”"
            )
        recent = self.commits_touching(path, limit=3)
        if recent:
            payload["file_commits"] = [c.to_dict() for c in recent]
        return payload
