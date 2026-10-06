"""Unified-diff generation, validation and application.

FixPilot never blind-writes files.  A change is always expressed as a reviewable
unified diff, validated against policy (no path escapes, no credential files, no
oversized patches), applied with context verification, and then re-verified from
the resulting file on disk.  If the context does not match — a real risk when a
model writes the diff — the applier reports *exactly* which line disagreed so the
refinement loop can correct the patch instead of guessing.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import Settings
from ..util import atomic_write_text, read_text, truncate_bytes
from ..security.secrets import is_sensitive_path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class PatchError(Exception):
    """Raised when a patch cannot be safely represented or applied."""


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # prefixed with ' ', '+', '-'

    def to_dict(self) -> dict:
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "lines": len(self.lines),
        }


@dataclass(slots=True)
class FilePatch:
    path: str
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_delete: bool = False
    binary: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "is_new": self.is_new,
            "is_delete": self.is_delete,
            "hunks": [h.to_dict() for h in self.hunks],
            "additions": self.additions,
            "deletions": self.deletions,
        }

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("+"))

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("-"))


@dataclass(slots=True)
class ParsedPatch:
    raw: str
    files: list[FilePatch] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def touched_paths(self) -> list[str]:
        return [f.path for f in self.files]

    def to_dict(self) -> dict:
        return {
            "files": [f.to_dict() for f in self.files],
            "additions": self.additions,
            "deletions": self.deletions,
            "bytes": len(self.raw.encode("utf-8", "replace")),
        }

    def summary(self) -> str:
        return f"{len(self.files)} file(s), +{self.additions}/-{self.deletions}"


@dataclass(slots=True)
class ApplyResult:
    ok: bool = False
    dry_run: bool = False
    applied: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    error: str = ""
    backup: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "applied": self.applied,
            "failed": self.failed,
            "error": self.error or None,
            "backup_files": list(self.backup.keys()),
        }


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def normalise_path(path: str) -> str:
    cleaned = str(path).replace("\\", "/").strip()
    if cleaned.startswith(("a/", "b/")):
        cleaned = cleaned[2:]
    return str(PurePosixPath(cleaned)).lstrip("./")


def make_diff(path: str, old: str, new: str, *, context: int = 3) -> str:
    """Produce a unified diff for a single file (git-compatible headers)."""
    rel = normalise_path(path)
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=context, lineterm="")
    body = "\n".join(diff)
    if not body:
        return ""
    header = f"diff --git a/{rel} b/{rel}\n"
    return f"{header}{body}\n"


@dataclass(slots=True)
class FileProposal:
    """A candidate change: this file should look like *this*."""

    path: str
    new_content: str
    old_content: str = ""
    reason: str = ""
    strategy: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "reason": self.reason,
            "strategy": self.strategy,
            "confidence": round(self.confidence, 2),
            "old_bytes": len(self.old_content.encode("utf-8", "replace")),
            "new_bytes": len(self.new_content.encode("utf-8", "replace")),
            "changed": self.old_content != self.new_content,
        }


def proposals_to_patch(proposals: list[FileProposal]) -> tuple[str, ParsedPatch]:
    """Render proposals as one unified diff, dropping no-op proposals."""
    chunks: list[str] = []
    for proposal in proposals:
        if proposal.old_content == proposal.new_content:
            continue
        old = proposal.old_content
        if old and not old.endswith("\n"):
            old += "\n"
        new = proposal.new_content
        if new and not new.endswith("\n"):
            new += "\n"
        diff = make_diff(proposal.path, old, new)
        if diff:
            chunks.append(diff)
    raw = "\n".join(chunks)
    return raw, parse_unified(raw)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_unified(raw: str) -> ParsedPatch:
    patch = ParsedPatch(raw=raw or "")
    current: FilePatch | None = None
    current_hunk: Hunk | None = None
    lines = (raw or "").replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("diff --git "):
            current, current_hunk = None, None
            continue
        if line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip()
            if index < len(lines) and lines[index].startswith("+++ "):
                new_path = lines[index][4:].split("\t")[0].strip()
                index += 1
                path = normalise_path(new_path if new_path != "/dev/null" else old_path)
                current = FilePatch(
                    path=path,
                    old_path=normalise_path(old_path),
                    new_path=normalise_path(new_path),
                    is_new=old_path == "/dev/null",
                    is_delete=new_path == "/dev/null",
                )
                patch.files.append(current)
                current_hunk = None
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if current is not None:
                current.binary = True
            continue
        match = HUNK_RE.match(line)
        if match and current is not None:
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current_hunk = Hunk(old_start=old_start, old_count=old_count, new_start=new_start, new_count=new_count)
            current.hunks.append(current_hunk)
            continue
        if current_hunk is not None:
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            if line == "" and index >= len(lines):
                continue
            if line[:1] in {" ", "+", "-"}:
                current_hunk.lines.append(line)
            elif line == "":
                current_hunk.lines.append(" ")
            # Anything else ends the hunk region for this file.
            elif not line.startswith(("index ", "old mode", "new mode", "similarity", "rename")):
                current_hunk = None
    return patch


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_patch(patch: ParsedPatch, root: Path, settings: Settings, *, allow_new_files: bool = True) -> list[str]:
    """Return a list of policy violations (empty means the patch is acceptable)."""
    problems: list[str] = []
    if not patch.files:
        problems.append("patch contains no file changes")
        return problems
    if len(patch.files) > settings.sandbox.max_patch_files:
        problems.append(f"patch touches {len(patch.files)} files, limit is {settings.sandbox.max_patch_files}")
    size = len(patch.raw.encode("utf-8", "replace"))
    if size > settings.sandbox.max_patch_bytes:
        problems.append(f"patch is {size} bytes, limit is {settings.sandbox.max_patch_bytes}")
    root_resolved = root.resolve()
    seen: set[str] = set()
    for file in patch.files:
        for raw_path in {file.path, file.old_path, file.new_path}:
            if raw_path in {"", "/dev/null"}:
                continue
            pure = PurePosixPath(raw_path)
            if pure.is_absolute() or ".." in pure.parts:
                problems.append(f"path escapes the repository: {raw_path}")
            if file.path in seen:
                problems.append(f"duplicate file entry in patch: {file.path}")
                break
        seen.add(file.path)
        sensitive, label = is_sensitive_path(file.path)
        if sensitive:
            problems.append(f"refusing to patch protected {label}: {file.path}")
        target = (root / file.path)
        try:
            target.resolve().relative_to(root_resolved)
        except ValueError:
            problems.append(f"path resolves outside the repository: {file.path}")
        if file.binary:
            problems.append(f"binary patches are not supported: {file.path}")
        if file.is_delete:
            problems.append(f"file deletion is not automated: {file.path}")
        if file.is_new and not allow_new_files:
            problems.append(f"new files are not allowed in this patch: {file.path}")
        if not file.hunks and not file.is_new:
            problems.append(f"no hunks for {file.path} (empty or malformed diff)")
        if not target.exists() and not file.is_new:
            problems.append(f"target file does not exist: {file.path}")
    return problems


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def _split_keep(content: str) -> tuple[list[str], bool]:
    trailing = content.endswith("\n")
    lines = content.split("\n")
    if trailing:
        lines = lines[:-1]
    return lines, trailing


def _join(lines: list[str], trailing: bool) -> str:
    body = "\n".join(lines)
    if trailing and not body.endswith("\n"):
        body += "\n"
    return body


def _old_side(hunk: Hunk) -> list[str]:
    return [line[1:] for line in hunk.lines if line[:1] in {" ", "-"}]


def _new_side(hunk: Hunk) -> list[str]:
    return [line[1:] for line in hunk.lines if line[:1] in {" ", "+"}]


def _match_score(window: list[str], expected: list[str]) -> float:
    if not expected:
        return 0.0
    matcher = difflib.SequenceMatcher(a=window, b=expected, autojunk=False)
    return matcher.ratio()


def apply_to_content(
    content: str,
    file_patch: FilePatch,
    *,
    strict: bool = False,
    fuzzy_threshold: float = 0.72,
    search_window: int = 400,
) -> tuple[str, list[dict]]:
    """Apply hunks to one file's text.  Returns (new_content, notes)."""
    if file_patch.is_new:
        body = _join(_new_side_from_new_file(file_patch), True)
        return body, [{"path": file_patch.path, "note": "new file created", "offset": 0}]
    lines, trailing = _split_keep(content)
    notes: list[dict] = []
    # Apply bottom-up so earlier line numbers stay valid.
    for hunk in sorted(file_patch.hunks, key=lambda h: h.old_start, reverse=True):
        expected = _old_side(hunk)
        replacement = _new_side(hunk)
        target = hunk.old_start - 1
        applied = False
        for offset in _offset_candidates(len(lines), target, len(expected), search_window):
            window = lines[offset : offset + len(expected)]
            exact = window == expected
            score = 1.0 if exact else _match_score(window, expected)
            if exact or (not strict and score >= fuzzy_threshold):
                lines[offset : offset + len(expected)] = replacement
                notes.append(
                    {
                        "path": file_patch.path,
                        "hunk": f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@",
                        "offset": offset + 1,
                        "drift": offset - target,
                        "match": round(score, 3),
                        "fuzzy": not exact,
                    }
                )
                applied = True
                break
        if not applied:
            actual = lines[max(0, target) : max(0, target) + max(1, len(expected))]
            diff_detail = _first_mismatch(expected, actual)
            raise PatchError(
                f"context mismatch in {file_patch.path} at line {hunk.old_start}: {diff_detail}"
            )
    return _join(lines, trailing), notes


def _new_side_from_new_file(file_patch: FilePatch) -> list[str]:
    return [line[1:] for hunk in file_patch.hunks for line in hunk.lines if line[:1] in {" ", "+"}]


def _offset_candidates(total: int, target: int, length: int, window: int) -> list[int]:
    """Preferred positions: exact, then nearest, then outward search."""
    candidates = [target]
    for delta in range(1, window + 1):
        for position in (target + delta, target - delta):
            if 0 <= position <= max(0, total - length) or (length == 0 and 0 <= position <= total):
                candidates.append(position)
    return candidates


def _first_mismatch(expected: list[str], actual: list[str]) -> str:
    for index, (want, got) in enumerate(zip(expected, actual)):
        if want != got:
            return f"expected {want.strip()[:80]!r} but found {got.strip()[:80]!r} (context line {index + 1})"
    if len(actual) < len(expected):
        return f"file ended early: expected {len(expected)} context lines, found {len(actual)}"
    return "no matching context window found"


def apply_patch(
    root: Path,
    patch: ParsedPatch,
    settings: Settings,
    *,
    dry_run: bool = False,
    strict: bool = False,
    backup_dir: Path | None = None,
) -> ApplyResult:
    """Apply a parsed patch to the working tree, verifying every file."""
    result = ApplyResult(dry_run=dry_run)
    problems = validate_patch(patch, root, settings)
    if problems:
        result.error = "; ".join(problems)
        result.failed = [{"path": p.path, "error": "policy violation"} for p in patch.files]
        return result

    staged: dict[str, str] = {}
    for file_patch in patch.files:
        target = (root / file_patch.path)
        if file_patch.is_new:
            original = ""
        else:
            try:
                original = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.failed.append({"path": file_patch.path, "error": f"cannot read file: {exc}"})
                continue
        try:
            updated, notes = apply_to_content(original, file_patch, strict=strict)
        except PatchError as exc:
            result.failed.append({"path": file_patch.path, "error": str(exc)})
            continue
        if updated == original and not file_patch.is_new:
            result.failed.append({"path": file_patch.path, "error": "patch produced no change"})
            continue
        staged[file_patch.path] = updated
        result.backup[file_patch.path] = original
        result.applied.append(
            {
                "path": file_patch.path,
                "hunks": len(file_patch.hunks),
                "additions": file_patch.additions,
                "deletions": file_patch.deletions,
                "notes": notes,
            }
        )

    if result.failed:
        result.error = "one or more files could not be patched"
        return result
    if dry_run:
        result.ok = True
        return result

    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for path, content in staged.items():
            target = root / path
            if backup_dir is not None:
                backup_target = backup_dir / path
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(backup_target, result.backup.get(path, ""))
            atomic_write_text(target, content)
    except OSError as exc:
        result.ok = False
        result.error = f"write failed: {exc}"
        _restore(root, result.backup)
        return result

    result.ok = True
    return result


def _restore(root: Path, backup: dict[str, str]) -> None:
    for path, content in backup.items():
        try:
            atomic_write_text(root / path, content)
        except OSError:
            continue


def revert_patch(root: Path, backup: dict[str, str]) -> ApplyResult:
    """Restore files captured in a previous :class:`ApplyResult.backup`."""
    result = ApplyResult()
    for path, content in backup.items():
        try:
            target = root / path
            if content == "" and not target.exists():
                continue
            atomic_write_text(target, content)
            result.applied.append({"path": path, "restored": True})
        except OSError as exc:
            result.failed.append({"path": path, "error": str(exc)})
    result.ok = not result.failed
    if result.failed:
        result.error = "rollback incomplete"
    return result


def patch_preview(patch: ParsedPatch, max_lines: int = 400) -> str:
    """Trimmed diff text for phone-sized review."""
    body, truncated = truncate_bytes(patch.raw, max_lines * 80)
    return body + ("\n... [diff truncated for display]" if truncated else "")


def diff_stats(patch: ParsedPatch) -> dict:
    return {
        "files": len(patch.files),
        "additions": patch.additions,
        "deletions": patch.deletions,
        "paths": patch.touched_paths(),
        "bytes": len(patch.raw.encode("utf-8", "replace")),
    }


def file_snapshot(path: Path) -> str:
    return read_text(path) if path.exists() else ""
