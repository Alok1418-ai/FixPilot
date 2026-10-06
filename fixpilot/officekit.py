"""iQOO Office Kit bridge: phone as command centre, workstation as muscle.

The phone is the interface; the heavy work (indexing, model inference, running
tests) happens on the paired computer.  This module models that link explicitly
so the product degrades honestly:

``direct``   the phone and the workstation are on the same network and the phone
             talks to the workstation's FixPilot server directly.
``relay``    the link is down (phone on cellular, laptop asleep).  Commands are
             queued as **jobs**; when Office Kit restores the link the worker
             claims and executes them, and the phone shows the results.

Nothing here is magic: it is a durable job queue with device liveness, a pairing
handshake, and a worker that only performs jobs through the *same* policy and
sandbox as everything else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings
from .store import read_json, write_json
from .util import new_id, now_iso

STALE_AFTER_SECONDS = 90
JOB_KINDS = ("run_tests", "apply_patch", "collect_evidence", "run_command", "sync_repo", "checkpoint", "verify")
JOB_QUEUED = "queued"
JOB_CLAIMED = "claimed"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"


@dataclass(slots=True)
class Device:
    id: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    repo_root: str = ""
    token: str = ""
    paired_at: str = ""
    last_seen: str = ""
    last_seen_epoch: float = 0.0
    stats: dict = field(default_factory=dict)

    def status(self) -> str:
        if not self.last_seen_epoch:
            return "paired"
        return "online" if time.time() - self.last_seen_epoch < STALE_AFTER_SECONDS else "stale"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status(),
            "capabilities": self.capabilities,
            "repo_root": self.repo_root,
            "paired_at": self.paired_at,
            "last_seen": self.last_seen,
            "stats": self.stats,
        }


class OfficeKitBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.data_dir / "officekit"
        self.root.mkdir(parents=True, exist_ok=True)
        self.devices_path = self.root / "devices.json"
        self.jobs_path = self.root / "jobs.json"
        self._devices: dict[str, Device] | None = None
        self._jobs: list[dict] | None = None

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------
    def devices(self) -> dict[str, Device]:
        if self._devices is None:
            payload = read_json(self.devices_path, {"devices": []}) or {"devices": []}
            self._devices = {}
            for item in payload.get("devices", []):
                device = Device(
                    id=item.get("id", new_id("dev", 8)),
                    name=item.get("name", "device"),
                    capabilities=list(item.get("capabilities", [])),
                    repo_root=item.get("repo_root", ""),
                    token=item.get("token", ""),
                    paired_at=item.get("paired_at", ""),
                    last_seen=item.get("last_seen", ""),
                    last_seen_epoch=float(item.get("last_seen_epoch", 0) or 0),
                    stats=item.get("stats", {}),
                )
                self._devices[device.id] = device
        return self._devices

    def _save_devices(self) -> None:
        payload = {"devices": [{**d.to_dict(), "token": d.token, "last_seen_epoch": d.last_seen_epoch} for d in self.devices().values()]}
        write_json(self.devices_path, payload)

    def pair(self, *, device_name: str, device_id: str = "", capabilities: list[str] | None = None, repo_root: str = "") -> dict:
        device = self.devices().get(device_id) if device_id else None
        if device is None:
            device = Device(
                id=new_id("dev", 10),
                name=device_name or "iQOO phone",
                token=new_id("devtok", 16),
                paired_at=now_iso(),
            )
            self.devices()[device.id] = device
        device.name = device_name or device.name
        device.capabilities = capabilities or ["screen", "voice", "camera", "officekit"]
        device.repo_root = repo_root or device.repo_root or str(self.settings.repo_root)
        device.last_seen = now_iso()
        device.last_seen_epoch = time.time()
        self._save_devices()
        return {
            "ok": True,
            "device": device.to_dict(),
            "device_token": device.token,
            "pairing_code": f"{device.token[:4].upper()}-{device.token[4:8].upper()}",
            "next": [
                "Open FixPilot on the phone and enter the pairing code (or scan the server QR).",
                "Keep the Office Kit connection enabled so the phone can reach this workstation.",
                "Commands issued while the link is down are queued and run automatically on reconnect.",
            ],
        }

    def heartbeat(self, *, device_id: str, stats: dict | None = None) -> dict:
        device = self.devices().get(device_id)
        if device is None:
            return {"ok": False, "error": f"unknown device {device_id}"}
        device.last_seen = now_iso()
        device.last_seen_epoch = time.time()
        if stats:
            device.stats = {**device.stats, **stats}
        self._save_devices()
        queued = [job for job in self.jobs() if job["status"] == JOB_QUEUED]
        return {"ok": True, "device": device.to_dict(), "queued_jobs": len(queued), "server_time": now_iso()}

    # ------------------------------------------------------------------
    # Jobs (the offline queue)
    # ------------------------------------------------------------------
    def jobs(self) -> list[dict]:
        if self._jobs is None:
            payload = read_json(self.jobs_path, {"jobs": []}) or {"jobs": []}
            self._jobs = list(payload.get("jobs", []))
        return self._jobs

    def _save_jobs(self) -> None:
        write_json(self.jobs_path, {"jobs": self.jobs()[-400:]})

    def enqueue(self, *, kind: str, payload: dict | None = None, session_id: str = "") -> dict:
        job = {
            "id": new_id("job", 10),
            "kind": kind if kind in JOB_KINDS else "run_command",
            "payload": payload or {},
            "session_id": session_id,
            "status": JOB_QUEUED,
            "created_at": now_iso(),
            "claimed_by": "",
            "claimed_at": "",
            "completed_at": "",
            "result": {},
        }
        self.jobs().append(job)
        self._save_jobs()
        return job

    def claim(self, job_id: str, *, device_id: str = "") -> dict | None:
        for job in self.jobs():
            if job["id"] != job_id:
                continue
            if job["status"] not in {JOB_QUEUED, JOB_FAILED}:
                return None
            job["status"] = JOB_CLAIMED
            job["claimed_by"] = device_id
            job["claimed_at"] = now_iso()
            self._save_jobs()
            return job
        return None

    def next_job(self, *, device_id: str = "") -> dict | None:
        for job in self.jobs():
            if job["status"] == JOB_QUEUED:
                return self.claim(job["id"], device_id=device_id)
        return None

    def complete(self, job_id: str, *, result: dict, status: str = JOB_COMPLETED) -> dict | None:
        for job in self.jobs():
            if job["id"] != job_id:
                continue
            job["status"] = status if status in {JOB_COMPLETED, JOB_FAILED} else JOB_COMPLETED
            job["completed_at"] = now_iso()
            job["result"] = result
            self._save_jobs()
            return job
        return None

    def list_jobs(self, *, device: str | None = None, limit: int = 20) -> list[dict]:
        jobs = [job for job in self.jobs() if not device or job["claimed_by"] == device]
        return list(reversed(jobs[-limit:]))

    def queue_depth(self) -> dict:
        counts: dict[str, int] = {}
        for job in self.jobs():
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Description & worker
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        devices = [device.to_dict() for device in self.devices().values()]
        online = [device for device in devices if device["status"] == "online"]
        return {
            "mode": "direct" if online else ("relay" if devices else "unpaired"),
            "devices": devices,
            "online_devices": len(online),
            "queue": self.queue_depth(),
            "job_kinds": list(JOB_KINDS),
            "how_it_works": [
                "Pair once: the phone receives a device token; the workstation keeps the repo, models and sandbox.",
                "Direct mode: the phone drives the workstation's FixPilot server over the local network.",
                "Relay mode: when the link drops, commands become jobs in a durable queue and run on reconnect.",
                "Every relayed job still passes the command policy and the sandbox — the link never widens privileges.",
            ],
        }

    def run_worker(self, *, device_id: str = "workstation", poll_seconds: float = 2.0, max_jobs: int = 0, on_job: Callable[[dict], dict] | None = None) -> dict:
        """Claim and execute queued jobs (used by ``fixpilot officekit worker``)."""
        executed = 0
        failures = 0
        while True:
            job = self.next_job(device_id=device_id)
            if job is None:
                if max_jobs and executed + failures >= max_jobs:
                    break
                time.sleep(poll_seconds)
                continue
            try:
                result = on_job(job) if on_job else {"ok": False, "error": "no job handler configured"}
                status = JOB_COMPLETED if result.get("ok", True) else JOB_FAILED
                self.complete(job["id"], result=result, status=status)
                failures += 0 if status == JOB_COMPLETED else 1
            except Exception as exc:  # pragma: no cover - worker resilience
                self.complete(job["id"], result={"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=JOB_FAILED)
                failures += 1
            executed += 1
            if max_jobs and executed + failures >= max_jobs:
                break
        return {"executed": executed, "failures": failures, "queue": self.queue_depth()}
