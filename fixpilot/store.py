"""Durable, crash-safe persistence for FixPilot (no external database).

Layout under ``<repo>/.fixpilot``::

    sessions/<id>/session.json     session metadata + current status
    sessions/<id>/events.jsonl     append-only event log (replay source)
    sessions/<id>/patch.diff       last proposed / applied unified diff
    memory/<repo>/project.json     long-lived project memory
    audit.jsonl                    every sandboxed command + policy decision

Writes go through :func:`atomic_write_text`; appends are single ``write()``
calls on an ``O_APPEND`` handle so concurrent readers never see torn lines.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

from .util import now_iso, atomic_write_text


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:  # pragma: no cover - fsync unsupported on some FS
            pass


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit is not None and len(records) > limit:
        return records[-limit:]
    return records


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    yield from read_jsonl(path)


class AuditLog:
    """Append-only record of every command the agent tried to run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, entry: dict[str, Any]) -> None:
        payload = {"ts": now_iso(), **entry}
        try:
            append_jsonl(self.path, payload)
        except OSError:
            pass  # auditing must never break the agent loop

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        return read_jsonl(self.path, limit=limit)


class EventLog:
    """Per-session append-only event stream — the replay substrate."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        last = 0
        for record in read_jsonl(self.path):
            seq = record.get("seq")
            if isinstance(seq, int):
                last = max(last, seq)
        return last

    def append(self, kind: str, payload: dict[str, Any] | None = None, *, actor: str = "agent") -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": now_iso(),
            "kind": kind,
            "actor": actor,
            "payload": payload or {},
        }
        append_jsonl(self.path, record)
        return record

    def events(self, limit: int | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        records = read_jsonl(self.path)
        return [r for r in records if int(r.get("seq", 0)) > since_seq][-limit:] if limit else [
            r for r in records if int(r.get("seq", 0)) > since_seq
        ]


def merge_dicts(base: dict[str, Any], updates: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(base)
    for key, value in updates:
        out[key] = value
    return out
