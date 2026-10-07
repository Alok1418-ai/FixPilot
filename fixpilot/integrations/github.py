"""GitHub insights for the phone dashboard: PRs, issues, checks and cadence.

FixPilot's own sessions answer *"did we verify this fix?"*.  This module answers
the question that follows: **did the fix survive contact with the repository?**
Was CI green before the merge, how often do changes get reverted, how long do
fixes sit unreviewed, is the backlog growing.

Design constraints, all inherited from the rest of the product:

* **Standard library only.** ``urllib.request`` + ``json`` — no SDK, no install.
* **Strictly read-only.** ``GET`` requests to the GitHub REST API. No writes, no
  webhooks, no GraphQL, no token ever returned to a client.
* **Never raise into a request path.** A bad token, a rate limit or a dead
  network degrades to the last cached snapshot with an explicit ``stale`` flag,
  because a dashboard that 500s is worse than one showing yesterday's numbers
  with a warning attached.
* **Pure metrics.** Fetching is separated from arithmetic: every scoring and
  bucketing function takes plain dicts, so the metrics are unit-tested offline
  against fixtures without touching the network.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import Settings
from ..repo.githistory import GitHistory
from ..store import read_json, write_json
from ..util import now_iso

USER_AGENT = "FixPilot (+https://github.com/Alok1418-ai/FixPilot)"
API_VERSION = "2022-11-28"
DEFAULT_HOST = "github.com"

# ``git@github.com:owner/name.git`` and friends, with credentials stripped.
_URL_LIKE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<path>.+)$")
_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
_REVERT = re.compile(r"^\s*revert\b[\s:\"-]*", re.I)
# Quoted subject of a revert commit: Revert "Add stock lookup guard" (#42)
_REVERT_TARGET = re.compile(r"revert\s+[\"“'](.+?)[\"”']", re.I)

FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
FAILED_STATES = {"failure", "error"}
OK_STATES = {"success"}


class GitHubError(Exception):
    """A GitHub call failed in a way worth explaining to a human."""

    def __init__(self, message: str, *, status: int = 0, hint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.hint = hint


# ---------------------------------------------------------------------------
# repository resolution
# ---------------------------------------------------------------------------


def parse_remote_url(url: str) -> dict[str, str] | None:
    """Parse any git remote URL form into ``{"host", "owner", "name", "repo"}``.

    Handles ``https://``, ``ssh://``, ``git://`` and scp-like ``git@host:o/r``,
    strips embedded credentials (``https://user:token@host/...``) and any
    trailing ``.git``.  Returns ``None`` for local paths and anything else that
    is not unambiguously ``host/owner/name``.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return None
    match = _URL_LIKE.match(raw) or _SCP_LIKE.match(raw)
    if match is None:
        return None
    host = (match.group("host") or "").lower()
    path = (match.group("path") or "").strip("/")
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or not host or "." not in host:
        return None
    owner, name = parts[-2], parts[-1]
    if not _SEGMENT.match(owner) or not _SEGMENT.match(name):
        return None
    return {"host": host, "owner": owner, "name": name, "repo": f"{owner}/{name}"}


def _remote_from_config(root: Path) -> str:
    """Read ``origin``'s URL straight out of ``.git/config`` (no ``git`` needed)."""
    config = root / ".git" / "config"
    if not config.is_file():
        return ""
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = ""
    fallback = ""
    for line in text.splitlines():
        stripped = line.strip()
        section_match = re.match(r'^\[(.+?)\]$', stripped)
        if section_match:
            section = section_match.group(1).lower()
            continue
        if not section.startswith('remote "'):
            continue
        key, _, value = stripped.partition("=")
        if key.strip().lower() != "url":
            continue
        url = value.strip()
        if section == 'remote "origin"':
            return url
        fallback = fallback or url
    return fallback


def detect_repo(root: Path, *, host: str = DEFAULT_HOST) -> dict[str, str] | None:
    """Resolve the repository FixPilot is serving to ``owner/name``.

    Prefers the real ``git remote`` (which honours insteadOf rewrites), falls
    back to parsing ``.git/config``.  Remotes on a host other than the one the
    API is pointed at are ignored unless they match — a token must never be
    sent to a host the operator did not configure.
    """
    candidates = [
        GitHistory(root).run(["remote", "get-url", "origin"]).strip(),
        _remote_from_config(root),
    ]
    for candidate in candidates:
        parsed = parse_remote_url(candidate)
        if parsed and parsed["host"] == host.lower():
            return parsed
    return None


# ---------------------------------------------------------------------------
# small time / statistics helpers (pure)
# ---------------------------------------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _hours_between(start: Any, end: Any) -> float | None:
    first, second = _parse_time(start), _parse_time(end)
    if first is None or second is None:
        return None
    return round((second - first).total_seconds() / 3600.0, 2)


def _days_since(value: Any, now: datetime) -> float | None:
    stamp = _parse_time(value)
    if stamp is None:
        return None
    return round((now - stamp).total_seconds() / 86400.0, 1)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 2)


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def is_revert(title: str) -> bool:
    return bool(_REVERT.match(title or ""))


def revert_target(title: str) -> str:
    match = _REVERT_TARGET.search(title or "")
    return match.group(1).strip() if match else ""


def _author(entry: dict[str, Any]) -> str:
    user = entry.get("user") or {}
    return str(user.get("login") or "") or "ghost"


def _labels(entry: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for label in entry.get("labels") or []:
        if not isinstance(label, dict):
            continue
        name = str(label.get("name") or "").strip()
        if name:
            out.append({"name": name, "color": str(label.get("color") or "")})
    return out


def _short_sha(value: Any) -> str:
    return str(value or "")[:7]


# ---------------------------------------------------------------------------
# CI verdicts (pure)
# ---------------------------------------------------------------------------


def check_verdict(check_runs: Iterable[dict[str, Any]]) -> str:
    """Collapse a commit's check runs into ``passed`` / ``failed`` / ``running`` / ``none``."""
    runs = [run for run in (check_runs or []) if isinstance(run, dict)]
    if not runs:
        return "none"
    for run in runs:
        if str(run.get("conclusion") or "") in FAILED_CONCLUSIONS:
            return "failed"
    for run in runs:
        if str(run.get("status") or "") != "completed":
            return "running"
    for run in runs:
        conclusion = str(run.get("conclusion") or "")
        if conclusion in OK_CONCLUSIONS:
            continue
        if run.get("conclusion") is None:
            return "running"
        return "failed"
    return "passed"


def status_verdict(state: Any) -> str:
    """Map a combined commit status (classic CI) to the same vocabulary."""
    text = str(state or "").lower()
    if text in OK_STATES:
        return "passed"
    if text in FAILED_STATES:
        return "failed"
    if text:
        return "running"
    return "none"


# ---------------------------------------------------------------------------
# metrics (pure — every function below is fixture-testable without a network)
# ---------------------------------------------------------------------------


def summarise_pulls(
    pulls: list[dict[str, Any]],
    checks: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    stale_days: int = 7,
) -> dict[str, Any]:
    """Delivery quality across pull requests: merge rate, reverts, CI-before-merge."""
    now = now or datetime.now(timezone.utc)
    checks = checks or {}
    merged = [pull for pull in pulls if pull.get("merged_at")]
    rejected = [pull for pull in pulls if not pull.get("merged_at") and str(pull.get("state")) == "closed"]
    open_pulls = [pull for pull in pulls if str(pull.get("state")) == "open"]

    def verdict_for(pull: dict[str, Any]) -> str:
        sha = str((pull.get("head") or {}).get("sha") or "")
        return checks.get(sha) or checks.get(_short_sha(sha)) or "none"

    merge_hours = [hours for hours in (_hours_between(pull.get("created_at"), pull.get("merged_at")) for pull in merged) if hours is not None]
    open_ages = [age for age in (_days_since(pull.get("created_at"), now) for pull in open_pulls) if age is not None]

    reverts = [pull for pull in merged if is_revert(str(pull.get("title") or ""))]
    merged_verdicts = {id(pull): verdict_for(pull) for pull in merged}
    green_merged = sum(1 for verdict in merged_verdicts.values() if verdict == "passed")
    red_merged = sum(1 for verdict in merged_verdicts.values() if verdict == "failed")
    judged_merged = green_merged + red_merged

    open_verdicts = [verdict_for(pull) for pull in open_pulls]
    unreviewed = sum(1 for pull in open_pulls if not int(pull.get("review_comments") or 0))

    return {
        "sampled": len(pulls),
        "open": len(open_pulls),
        "draft": sum(1 for pull in open_pulls if pull.get("draft")),
        "ready": sum(1 for pull in open_pulls if not pull.get("draft")),
        "merged": len(merged),
        "closed_unmerged": len(rejected),
        "merge_rate": _ratio(len(merged), len(merged) + len(rejected)),
        "reverted": len(reverts),
        "revert_rate": _ratio(len(reverts), len(merged)),
        "stale_open": sum(1 for age in open_ages if age is not None and age >= stale_days),
        "unreviewed_open": unreviewed,
        "oldest_open_days": max(open_ages) if open_ages else None,
        "median_merge_hours": _percentile(merge_hours, 0.5),
        "p90_merge_hours": _percentile(merge_hours, 0.9),
        "median_open_age_days": _percentile([age for age in open_ages], 0.5),
        "ci_green_merged": green_merged,
        "ci_red_merged": red_merged,
        "ci_green_rate": _ratio(green_merged, judged_merged),
        "ci_judged": judged_merged,
        "ci_running_open": sum(1 for verdict in open_verdicts if verdict == "running"),
        "ci_failing_open": sum(1 for verdict in open_verdicts if verdict == "failed"),
        "ci_green_open": sum(1 for verdict in open_verdicts if verdict == "passed"),
    }


def summarise_issues(issues: list[dict[str, Any]], *, now: datetime | None = None, stale_days: int = 30) -> dict[str, Any]:
    """Backlog shape: open/closed, staleness, and which labels dominate."""
    now = now or datetime.now(timezone.utc)
    open_issues = [issue for issue in issues if str(issue.get("state")) == "open"]
    closed = [issue for issue in issues if str(issue.get("state")) == "closed"]
    ages = [age for age in (_days_since(issue.get("created_at"), now) for issue in open_issues) if age is not None]
    counts: dict[str, int] = {}
    for issue in open_issues:
        for label in _labels(issue):
            counts[label["name"]] = counts.get(label["name"], 0) + 1
    labels = [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]]
    return {
        "sampled": len(issues),
        "open": len(open_issues),
        "closed": len(closed),
        "stale_open": sum(1 for age in ages if age is not None and age >= stale_days),
        "unlabelled_open": sum(1 for issue in open_issues if not _labels(issue)),
        "oldest_open_days": max(ages) if ages else None,
        "median_open_age_days": _percentile(ages, 0.5),
        "labels": labels,
    }


def week_buckets(weeks: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """ISO-week buckets ending with the week that contains ``now`` (oldest first)."""
    now = now or datetime.now(timezone.utc)
    anchor = now.astimezone(timezone.utc)
    start = (anchor - timedelta(days=anchor.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: list[dict[str, Any]] = []
    for offset in range(max(1, weeks) - 1, -1, -1):
        begin = start - timedelta(days=7 * offset)
        buckets.append(
            {
                "start": begin.isoformat(),
                "end": (begin + timedelta(days=7)).isoformat(),
                "label": begin.strftime("%d %b"),
                "iso": begin.isocalendar()[:2],
                "opened": 0,
                "merged": 0,
                "closed_unmerged": 0,
                "reverted": 0,
                "issues_opened": 0,
                "issues_closed": 0,
                "ci_red_merged": 0,
            }
        )
    return buckets


def trend_series(
    buckets: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    checks: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fill weekly buckets in place (and return them) from raw GitHub objects."""
    checks = checks or {}
    by_start = {bucket["start"]: bucket for bucket in buckets}
    ordered = sorted(buckets, key=lambda bucket: bucket["start"])

    def bucket_for(value: Any) -> dict[str, Any] | None:
        stamp = _parse_time(value)
        if stamp is None:
            return None
        begin = (stamp.astimezone(timezone.utc) - timedelta(days=stamp.astimezone(timezone.utc).weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return by_start.get(begin.isoformat())

    for pull in pulls:
        merged_at = pull.get("merged_at")
        for key, value in (("opened", pull.get("created_at")), ("merged", merged_at), ("closed_unmerged", None if merged_at else pull.get("closed_at"))):
            if not value:
                continue
            bucket = bucket_for(value)
            if bucket is None:
                continue
            bucket[key] += 1
            if key == "merged":
                if is_revert(str(pull.get("title") or "")):
                    bucket["reverted"] += 1
                sha = str((pull.get("head") or {}).get("sha") or "")
                if (checks.get(sha) or checks.get(_short_sha(sha))) == "failed":
                    bucket["ci_red_merged"] += 1
    for issue in issues:
        for key, value in (("issues_opened", issue.get("created_at")), ("issues_closed", issue.get("closed_at"))):
            if not value:
                continue
            bucket = bucket_for(value)
            if bucket is not None:
                bucket[key] += 1
    return ordered


def summarise_authors(pulls: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    """Who is landing the merges (and who has the most open WIP)."""
    table: dict[str, dict[str, Any]] = {}
    for pull in pulls:
        login = _author(pull)
        row = table.setdefault(login, {"login": login, "merged": 0, "opened": 0, "open": 0})
        row["opened"] += 1
        if pull.get("merged_at"):
            row["merged"] += 1
        elif str(pull.get("state")) == "open":
            row["open"] += 1
    ranked = sorted(table.values(), key=lambda row: (-row["merged"], -row["opened"], row["login"]))
    return ranked[:limit]


def queue_entries(pulls: list[dict[str, Any]], checks: dict[str, str] | None, *, now: datetime | None = None, limit: int = 8) -> list[dict[str, Any]]:
    """Open pull requests, most recently updated first, with a CI badge each."""
    now = now or datetime.now(timezone.utc)
    checks = checks or {}
    rows: list[dict[str, Any]] = []
    for pull in pulls:
        if str(pull.get("state")) != "open":
            continue
        sha = str((pull.get("head") or {}).get("sha") or "")
        rows.append(
            {
                "number": int(pull.get("number") or 0),
                "title": str(pull.get("title") or ""),
                "url": str(pull.get("html_url") or ""),
                "author": _author(pull),
                "draft": bool(pull.get("draft")),
                "age_days": _days_since(pull.get("created_at"), now),
                "updated_days": _days_since(pull.get("updated_at"), now),
                "checks": checks.get(sha) or checks.get(_short_sha(sha)) or "none",
                "additions": int(pull.get("additions") or 0),
                "deletions": int(pull.get("deletions") or 0),
                "changed_files": int(pull.get("changed_files") or 0),
                "review_comments": int(pull.get("review_comments") or 0),
                "labels": _labels(pull)[:4],
            }
        )
    rows.sort(key=lambda row: (row["updated_days"] if row["updated_days"] is not None else 9e9))
    return rows[:limit]


def merge_entries(pulls: list[dict[str, Any]], checks: dict[str, str] | None, *, limit: int = 6) -> list[dict[str, Any]]:
    """Most recent merges (with their CI verdict and time-to-merge)."""
    checks = checks or {}
    rows: list[dict[str, Any]] = []
    for pull in pulls:
        if not pull.get("merged_at"):
            continue
        sha = str((pull.get("head") or {}).get("sha") or "")
        title = str(pull.get("title") or "")
        rows.append(
            {
                "number": int(pull.get("number") or 0),
                "title": title,
                "url": str(pull.get("html_url") or ""),
                "author": _author(pull),
                "merged_at": str(pull.get("merged_at") or ""),
                "hours_to_merge": _hours_between(pull.get("created_at"), pull.get("merged_at")),
                "checks": checks.get(sha) or checks.get(_short_sha(sha)) or "none",
                "additions": int(pull.get("additions") or 0),
                "deletions": int(pull.get("deletions") or 0),
                "revert": is_revert(title),
                "reverts": revert_target(title) or None,
            }
        )
    rows.sort(key=lambda row: row["merged_at"], reverse=True)
    return rows[:limit]


def revert_entries(pulls: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    """Merged reverts — the clearest evidence a fix did not hold."""
    rows = [entry for entry in merge_entries(pulls, None, limit=len(pulls) or 1) if entry["revert"]]
    return rows[:limit]


def summarise_workflow_runs(runs: list[dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
    """Health of the default branch and the most recent workflow runs."""
    recent: list[dict[str, Any]] = []
    for run in (runs or []):
        if not isinstance(run, dict):
            continue
        recent.append(
            {
                "name": str(run.get("name") or run.get("display_title") or "workflow"),
                "status": str(run.get("status") or ""),
                "conclusion": str(run.get("conclusion") or ""),
                "branch": str(run.get("head_branch") or ""),
                "sha": _short_sha(run.get("head_sha")),
                "url": str(run.get("html_url") or ""),
                "updated_at": str(run.get("updated_at") or ""),
                "event": str(run.get("event") or ""),
            }
        )
    passed = sum(1 for run in recent if run["conclusion"] == "success")
    failed = sum(1 for run in recent if run["conclusion"] in FAILED_CONCLUSIONS)
    return {"sampled": len(recent), "passed": passed, "failed": failed, "recent": recent[:limit]}


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Minimal, read-only GitHub REST client (stdlib ``urllib``)."""

    def __init__(self, settings: Settings) -> None:
        config = settings.github
        self.settings = settings
        self.api_base = (config.api_base or "https://api.github.com").rstrip("/")
        self.token = config.token
        self.timeout = max(3, config.timeout)
        self.rate_limit: dict[str, Any] = {}
        self._lock = threading.Lock()
        host = urllib.parse.urlparse(self.api_base).hostname or ""
        self.default_host = DEFAULT_HOST if host.endswith("github.com") else host

    @property
    def authenticated(self) -> bool:
        """Whether requests carry a token — and a token only ever goes to ``api_base``."""
        return bool(self.token)

    # -- transport ------------------------------------------------------
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if params:
            cleaned = {key: value for key, value in params.items() if value not in (None, "")}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned)}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                self._record_rate(response.headers)
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            self._record_rate(exc.headers)
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"could not reach {self.api_base}", hint=str(exc.reason)) from None
        except (OSError, ValueError) as exc:
            raise GitHubError("malformed response from GitHub", hint=str(exc)) from None

    def _record_rate(self, headers: Any) -> None:
        if headers is None:
            return
        try:
            remaining = headers.get("x-ratelimit-remaining")
            limit = headers.get("x-ratelimit-limit")
            reset = headers.get("x-ratelimit-reset")
        except AttributeError:
            return
        if remaining is None and limit is None:
            return
        reset_at = ""
        if reset:
            try:
                reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                reset_at = ""
        with self._lock:
            self.rate_limit = {
                "remaining": int(remaining) if remaining is not None else None,
                "limit": int(limit) if limit is not None else None,
                "reset_at": reset_at,
            }

    def _http_error(self, exc: urllib.error.HTTPError) -> GitHubError:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        except (ValueError, OSError):
            payload = {}
        detail = str(payload.get("message") or exc.reason or "").strip()
        if exc.code == 401:
            return GitHubError("GitHub rejected the token (401)", status=401, hint="Set FIXPILOT_GITHUB_TOKEN to a valid fine-grained token with read-only repository access.")
        if exc.code == 404:
            return GitHubError(f"repository not found or not visible to this token (404)", status=404, hint="Check FIXPILOT_GITHUB_REPO and the token's repository access.")
        if exc.code == 403 and "rate limit" in detail.lower():
            reset = self.rate_limit.get("reset_at") or ""
            return GitHubError("GitHub rate limit reached (403)", status=403, hint=f"Resets at {reset} UTC — add a token to raise the limit from 60 to 5000 requests/hour.")
        if exc.code == 403:
            return GitHubError(f"GitHub refused the request (403): {detail or 'forbidden'}", status=403, hint="The token may lack the required read scopes (actions, checks, pull requests, issues).")
        return GitHubError(f"GitHub returned {exc.code}: {detail or 'error'}", status=exc.code)

    @property
    def rate_limited(self) -> bool:
        remaining = self.rate_limit.get("remaining")
        return isinstance(remaining, int) and remaining <= 5

    # -- endpoints ------------------------------------------------------
    def repository(self, repo: str) -> dict[str, Any]:
        return self.get(f"/repos/{repo}") or {}

    def pull_requests(self, repo: str, *, limit: int) -> list[dict[str, Any]]:
        payload = self.get(
            f"/repos/{repo}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": max(1, min(100, limit))},
        )
        return [item for item in payload or [] if isinstance(item, dict)]

    def issues(self, repo: str, *, limit: int) -> list[dict[str, Any]]:
        payload = self.get(
            f"/repos/{repo}/issues",
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": max(1, min(100, limit))},
        )
        # The issues endpoint also returns pull requests; drop them.
        return [item for item in payload or [] if isinstance(item, dict) and "pull_request" not in item]

    def check_verdict_for(self, repo: str, sha: str) -> str:
        """Check runs (Actions) first, combined status (classic CI) second."""
        if not sha or self.rate_limited:
            return "none"
        try:
            payload = self.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
            runs = (payload or {}).get("check_runs") or []
            if runs:
                return check_verdict(runs)
        except GitHubError as exc:
            if exc.status in (401, 403, 404, 422):
                return "none"
            raise
        try:
            payload = self.get(f"/repos/{repo}/commits/{sha}/status")
            if not isinstance(payload, dict):
                return "none"
            # GitHub reports ``state: "pending"`` for a commit with *no* statuses
            # at all — that is "no CI here", not "CI is still running".
            if not payload.get("statuses") and not int(payload.get("total_count") or 0):
                return "none"
            return status_verdict(payload.get("state"))
        except GitHubError:
            return "none"

    def check_verdicts(self, repo: str, shas: list[str], *, budget: int) -> dict[str, str]:
        """Verdicts for up to ``budget`` commits, fetched concurrently and politely."""
        wanted = [sha for sha in dict.fromkeys(shas) if sha][: max(0, budget)]
        if not wanted:
            return {}
        verdicts: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for sha, verdict in zip(wanted, pool.map(lambda sha: self.check_verdict_for(repo, sha), wanted)):
                verdicts[sha] = verdict
        return verdicts

    def workflow_runs(self, repo: str, *, limit: int, branch: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": max(1, min(100, limit))}
        if branch:
            params["branch"] = branch
        payload = self.get(f"/repos/{repo}/actions/runs", params)
        return [item for item in (payload or {}).get("workflow_runs") or [] if isinstance(item, dict)]

    def latest_commit(self, repo: str, branch: str = "") -> dict[str, Any]:
        payload = self.get(f"/repos/{repo}/commits", {"per_page": 1, **({"sha": branch} if branch else {})})
        if isinstance(payload, list) and payload:
            return payload[0] if isinstance(payload[0], dict) else {}
        return {}


# ---------------------------------------------------------------------------
# insight collector (caching + degradation)
# ---------------------------------------------------------------------------


class GitHubInsights:
    """Fetch, summarise, cache and degrade gracefully for ``GET /api/github``."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        try:
            settings.github_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # a read-only data dir must not stop the server from booting
        self.client = client or GitHubClient(settings)
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._fetched_at: float = 0.0

    # -- helpers --------------------------------------------------------
    @property
    def snapshot_path(self) -> Path:
        return self.settings.github_dir / "snapshot.json"

    def resolve_repo(self, override: str = "") -> str:
        """``owner/name`` from the query, settings, or the served repo's git remote."""
        for candidate in (override, self.settings.github.repo):
            parsed = parse_remote_url(candidate) or None
            if parsed:
                return parsed["repo"]
            text = (candidate or "").strip()
            if text.count("/") == 1 and all(_SEGMENT.match(part) for part in text.split("/")):
                return text
        host = getattr(self.client, "default_host", DEFAULT_HOST) or DEFAULT_HOST
        detected = detect_repo(self.settings.repo_root, host=host)
        return detected["repo"] if detected else ""

    def _offline_payload(self, repo: str, *, reason: str, error: str = "", hint: str = "") -> dict[str, Any]:
        return {
            "configured": reason != "no_repository",
            "ok": False,
            "reason": reason,
            "stale": False,
            "error": error,
            "hint": hint or self._setup_hint(reason),
            "authenticated": bool(getattr(self.client, "authenticated", False)),
            "repository": repo,
            "repository_url": f"https://{getattr(self.client, 'default_host', DEFAULT_HOST)}/{repo}" if repo else "",
            "fetched_at": "",
            "rate_limit": dict(getattr(self.client, "rate_limit", {}) or {}),
            "repo": {},
            "pulls": {},
            "issues": {},
            "ci": {},
            "trend": [],
            "queue": [],
            "merges": [],
            "reverts": [],
            "authors": [],
            "settings": self._settings_block(),
        }

    def _settings_block(self) -> dict[str, Any]:
        config = self.settings.github
        return {
            "weeks": config.weeks,
            "max_pull_requests": config.max_pull_requests,
            "max_issues": config.max_issues,
            "check_budget": config.check_budget,
            "cache_seconds": config.cache_seconds,
            "workflow_runs": config.workflow_runs,
        }

    @staticmethod
    def _setup_hint(reason: str) -> str:
        if reason == "no_repository":
            return "No GitHub remote found for this repository. Set FIXPILOT_GITHUB_REPO=owner/name (or add an origin remote)."
        if reason == "no_token":
            return "Set FIXPILOT_GITHUB_TOKEN (or GITHUB_TOKEN / GH_TOKEN) to a read-only token."
        return ""

    # -- the one entry point -------------------------------------------
    def load(self, *, force: bool = False, repo: str = "", weeks: int = 0) -> dict[str, Any]:
        """Return the dashboard payload. Never raises: failures come back as data."""
        resolved = self.resolve_repo(repo)
        if not resolved:
            return self._offline_payload("", reason="no_repository")
        if not bool(getattr(self.client, "authenticated", False)) and not self.settings.github.anonymous:
            return self._offline_payload(resolved, reason="no_token")

        config = self.settings.github
        ttl = max(0, config.cache_seconds if not force else 0)
        with self._lock:
            fresh = self._payload is not None and (time.monotonic() - self._fetched_at) <= ttl
            if fresh and self._payload is not None and self._payload.get("repository") == resolved:
                payload = dict(self._payload)
                payload["from_cache"] = True
                payload["age_seconds"] = round(time.monotonic() - self._fetched_at, 1)
                return payload

        try:
            payload = self._collect(resolved, weeks=weeks or config.weeks)
        except GitHubError as exc:
            return self._degrade(resolved, str(exc), exc.hint)
        except Exception as exc:  # noqa: BLE001 - a dashboard must not take the server down
            return self._degrade(resolved, f"{type(exc).__name__}: {exc}", "")

        with self._lock:
            self._payload = payload
            self._fetched_at = time.monotonic()
        try:
            write_json(self.snapshot_path, payload)
        except OSError:
            pass  # a read-only data dir must not break the dashboard
        return dict(payload, from_cache=False, age_seconds=0.0)

    def _degrade(self, repo: str, error: str, hint: str) -> dict[str, Any]:
        """Serve the last good snapshot (flagged stale) when a live fetch fails."""
        snapshot = read_json(self.snapshot_path, None)
        if isinstance(snapshot, dict) and snapshot.get("repository") == repo and snapshot.get("repo"):
            payload = dict(snapshot)
            payload.update(
                {
                    "configured": True,
                    "ok": False,
                    "stale": True,
                    "reason": "stale_snapshot",
                    "error": error,
                    "hint": hint or "Showing the last successful sync; tap Refresh to retry.",
                    "from_cache": True,
                    "rate_limit": dict(getattr(self.client, "rate_limit", {}) or {}),
                }
            )
            return payload
        return self._offline_payload(repo, reason="unavailable", error=error, hint=hint)

    # -- the actual fetch ----------------------------------------------
    def _collect(self, repo: str, *, weeks: int) -> dict[str, Any]:
        started = time.monotonic()
        config = self.settings.github
        client = self.client
        now = datetime.now(timezone.utc)

        info = client.repository(repo)
        default_branch = str(info.get("default_branch") or "")
        pulls = client.pull_requests(repo, limit=config.max_pull_requests)
        issues = client.issues(repo, limit=config.max_issues)

        # Enrich with additions/deletions/file counts in one call per PR, but
        # only for the newest slice — those are the numbers a human acts on.
        pulls = [dict(pull) for pull in pulls]
        detail_budget = max(0, min(len(pulls), config.detail_budget))
        if detail_budget:
            active = sorted(pulls, key=lambda pull: str(pull.get("updated_at") or ""), reverse=True)[:detail_budget]
            with ThreadPoolExecutor(max_workers=4) as pool:
                details = pool.map(lambda pull: self._pull_detail(repo, int(pull.get("number") or 0)), active)
                for pull, detail in zip(active, details):
                    if detail:
                        pull.update(detail)

        shas: list[str] = []
        for pull in sorted(pulls, key=lambda pull: str(pull.get("updated_at") or ""), reverse=True):
            sha = str((pull.get("head") or {}).get("sha") or "")
            if sha and sha not in shas:
                shas.append(sha)
        checks = client.check_verdicts(repo, shas, budget=config.check_budget)

        runs: list[dict[str, Any]] = []
        if config.workflow_runs:
            runs = client.workflow_runs(repo, limit=8, branch=default_branch)
        head_commit = client.latest_commit(repo, default_branch) if default_branch else {}

        buckets = trend_series(week_buckets(weeks, now=now), pulls, issues, checks)
        workflow = summarise_workflow_runs(runs, limit=5)
        pull_stats = summarise_pulls(pulls, checks, now=now)

        return {
            "configured": True,
            "ok": True,
            "reason": "",
            "stale": False,
            "error": "",
            "hint": "" if bool(getattr(client, "authenticated", False)) else "Running unauthenticated — 60 requests/hour. Add a token for 5000/hour and private repos.",
            "authenticated": bool(getattr(client, "authenticated", False)),
            "repository": repo,
            "repository_url": str(info.get("html_url") or f"https://{getattr(client, 'default_host', DEFAULT_HOST)}/{repo}"),
            "fetched_at": now_iso(),
            "from_cache": False,
            "age_seconds": 0.0,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "rate_limit": dict(getattr(client, "rate_limit", {}) or {}),
            "truncated": len(pulls) >= config.max_pull_requests,
            "repo": {
                "full_name": str(info.get("full_name") or repo),
                "description": str(info.get("description") or ""),
                "url": str(info.get("html_url") or ""),
                "default_branch": default_branch,
                "language": str(info.get("language") or ""),
                "stars": int(info.get("stargazers_count") or 0),
                "forks": int(info.get("forks_count") or 0),
                "watchers": int(info.get("subscribers_count") or info.get("watchers_count") or 0),
                "open_issues": int(info.get("open_issues_count") or 0),
                "private": bool(info.get("private")),
                "archived": bool(info.get("archived")),
                "topics": [str(topic) for topic in (info.get("topics") or [])][:6],
                "pushed_at": str(info.get("pushed_at") or ""),
                "created_at": str(info.get("created_at") or ""),
                "age_days": _days_since(info.get("created_at"), now),
                "pushed_days": _days_since(info.get("pushed_at"), now),
                "license": str((info.get("license") or {}).get("spdx_id") or ""),
            },
            "pulls": pull_stats,
            "issues": summarise_issues(issues, now=now),
            "ci": {
                **workflow,
                "red_open": pull_stats["ci_failing_open"],
                "running_open": pull_stats["ci_running_open"],
                "default_branch": default_branch,
                "head": {
                    "sha": _short_sha(head_commit.get("sha")),
                    "message": str(((head_commit.get("commit") or {}).get("message") or "")).splitlines()[0][:120],
                    "author": str(((head_commit.get("author") or {}).get("login") or "")),
                    "date": str(((head_commit.get("commit") or {}).get("author") or {}).get("date") or ""),
                    "url": str(head_commit.get("html_url") or ""),
                } if head_commit else {},
            },
            "trend": buckets,
            "queue": queue_entries(pulls, checks, now=now),
            "merges": merge_entries(pulls, checks),
            "reverts": revert_entries(pulls),
            "authors": summarise_authors(pulls),
            "settings": self._settings_block(),
        }

    def _pull_detail(self, repo: str, number: int) -> dict[str, Any]:
        if not number:
            return {}
        try:
            payload = self.client.get(f"/repos/{repo}/pulls/{number}")
        except GitHubError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            "additions": int(payload.get("additions") or 0),
            "deletions": int(payload.get("deletions") or 0),
            "changed_files": int(payload.get("changed_files") or 0),
            "review_comments": int(payload.get("review_comments") or 0),
            "comments": int(payload.get("comments") or 0),
            "mergeable_state": str(payload.get("mergeable_state") or ""),
            "requested_reviewers": [str((user or {}).get("login") or "") for user in payload.get("requested_reviewers") or []][:3],
        }
