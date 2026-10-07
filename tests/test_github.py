"""GitHub insights: URL parsing, the pure metrics, caching and degradation.

Nothing in here touches the network.  The HTTP layer is exercised through a
stub client, and every metric function is fed fixtures — which is the whole
reason fetching and arithmetic are separate modules of the same file.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixpilot.api.server import ApiError, FixPilotServer, Request, SESSION_COOKIE
from fixpilot.config import Settings
from fixpilot.core.agent import FixPilotAgent
from fixpilot.integrations.github import (
    GitHubClient,
    GitHubError,
    GitHubInsights,
    check_verdict,
    detect_repo,
    is_revert,
    merge_entries,
    parse_remote_url,
    queue_entries,
    revert_target,
    status_verdict,
    summarise_authors,
    summarise_issues,
    summarise_pulls,
    summarise_workflow_runs,
    trend_series,
    week_buckets,
)

from .support import REPO_ROOT, TempRepo

WEB = REPO_ROOT / "fixpilot" / "web"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
REPO_SLUG = "acme/rocket"


def _sha(char: str) -> str:
    return (char * 40)[:40]


def _pull(number: int, title: str, *, state: str, created: str, merged: str = "", closed: str = "",
          sha: str = "", draft: bool = False, reviews: int = 0, additions: int = 0, deletions: int = 0,
          updated: str = "") -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "draft": draft,
        "created_at": created,
        "merged_at": merged or None,
        "closed_at": closed or None,
        "updated_at": updated or merged or closed or created,
        "review_comments": reviews,
        "additions": additions,
        "deletions": deletions,
        "changed_files": 2,
        "html_url": f"https://github.com/acme/rocket/pull/{number}",
        "user": {"login": "alok"},
        "labels": [],
        "head": {"sha": sha},
    }


PULLS = [
    _pull(1, "Clamp the retry delay", state="closed", created="2026-10-01T00:00:00Z",
          merged="2026-10-01T12:00:00Z", sha=_sha("1"), reviews=3),
    _pull(2, "Add the schema guard", state="closed", created="2026-10-02T00:00:00Z",
          merged="2026-10-04T00:00:00Z", sha=_sha("2"), reviews=1),
    _pull(3, 'Revert "Cache parsed batches"', state="closed", created="2026-09-22T09:00:00Z",
          merged="2026-09-22T10:00:00Z", sha=_sha("3")),
    _pull(4, "Swap the parser", state="closed", created="2026-09-29T00:00:00Z",
          closed="2026-09-30T00:00:00Z", sha=_sha("4")),
    _pull(5, "Bound the queue drain", state="open", created="2026-09-27T12:00:00Z", sha=_sha("5")),
    _pull(6, "WIP: rewrite the worker", state="open", created="2026-10-04T12:00:00Z", sha=_sha("6"), draft=True),
]

CHECKS = {_sha("1"): "passed", _sha("2"): "failed", _sha("3"): "passed", _sha("5"): "running"}

ISSUES = [
    {"number": 10, "state": "open", "created_at": "2026-08-20T00:00:00Z", "labels": [{"name": "bug"}]},
    {"number": 11, "state": "open", "created_at": "2026-10-05T00:00:00Z",
     "labels": [{"name": "bug"}, {"name": "enhancement"}]},
    {"number": 12, "state": "closed", "created_at": "2026-09-01T00:00:00Z", "closed_at": "2026-09-05T00:00:00Z", "labels": []},
]


class StubClient:
    """Stands in for :class:`GitHubClient` — records calls, never opens a socket."""

    def __init__(self, *, authenticated: bool = True, failure: Exception | None = None) -> None:
        self.authenticated = authenticated
        self.failure = failure
        self.default_host = "github.com"
        self.rate_limit = {"remaining": 4999, "limit": 5000, "reset_at": "2026-10-07T04:34:21+00:00"}
        self.calls: list[str] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure

    def repository(self, repo: str) -> dict:
        self._guard("repository")
        return {
            "full_name": repo,
            "html_url": f"https://github.com/{repo}",
            "description": "Telemetry pipeline",
            "default_branch": "main",
            "language": "Python",
            "stargazers_count": 412,
            "forks_count": 57,
            "subscribers_count": 21,
            "open_issues_count": 9,
            "private": False,
            "archived": False,
            "topics": ["observability"],
            "created_at": "2025-08-01T00:00:00Z",
            "pushed_at": "2026-10-07T09:00:00Z",
            "license": {"spdx_id": "MIT"},
        }

    def pull_requests(self, repo: str, *, limit: int) -> list[dict]:
        self._guard("pull_requests")
        return [dict(pull) for pull in PULLS]

    def issues(self, repo: str, *, limit: int) -> list[dict]:
        self._guard("issues")
        return [dict(issue) for issue in ISSUES]

    def check_verdicts(self, repo: str, shas: list[str], *, budget: int) -> dict[str, str]:
        self._guard("check_verdicts")
        return {sha: CHECKS[sha] for sha in shas[:budget] if sha in CHECKS}

    def workflow_runs(self, repo: str, *, limit: int, branch: str = "") -> list[dict]:
        self._guard("workflow_runs")
        return [
            {"name": "CI", "status": "completed", "conclusion": "success", "head_branch": "main",
             "head_sha": _sha("9"), "html_url": f"https://github.com/{repo}/actions/runs/1",
             "updated_at": "2026-10-07T09:30:00Z", "event": "push"},
            {"name": "CI", "status": "completed", "conclusion": "failure", "head_branch": "main",
             "head_sha": _sha("8"), "html_url": f"https://github.com/{repo}/actions/runs/2",
             "updated_at": "2026-10-06T09:30:00Z", "event": "pull_request"},
        ]

    def latest_commit(self, repo: str, branch: str = "") -> dict:
        self._guard("latest_commit")
        return {
            "sha": _sha("9"),
            "html_url": f"https://github.com/{repo}/commit/{_sha('9')}",
            "author": {"login": "alok"},
            "commit": {"message": "Merge pull request #131", "author": {"date": "2026-10-07T09:00:00Z"}},
        }

    def get(self, path: str, params: dict | None = None) -> dict:
        self._guard("detail")
        if re.search(r"/pulls/\d+$", path):
            return {"additions": 84, "deletions": 12, "changed_files": 3, "review_comments": 4}
        return {}


def insights_for(repo: TempRepo, client: StubClient, **settings_kwargs) -> GitHubInsights:
    settings = repo.settings
    settings.github.repo = REPO_SLUG
    settings.github.detail_budget = 0
    for key, value in settings_kwargs.items():
        setattr(settings.github, key, value)
    return GitHubInsights(settings, client=client)


# ---------------------------------------------------------------------------
# remote parsing
# ---------------------------------------------------------------------------


class RemoteUrlTests(unittest.TestCase):
    def test_https_ssh_and_scp_forms_agree(self) -> None:
        for url in (
            "https://github.com/acme/rocket.git",
            "https://github.com/acme/rocket",
            "git@github.com:acme/rocket.git",
            "ssh://git@github.com/acme/rocket.git",
            "ssh://git@github.com:22/acme/rocket.git",
            "git://github.com/acme/rocket.git",
        ):
            with self.subTest(url=url):
                parsed = parse_remote_url(url)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["repo"], "acme/rocket")
                self.assertEqual(parsed["host"], "github.com")

    def test_credentials_in_the_url_are_dropped(self) -> None:
        parsed = parse_remote_url("https://alok:ghp_supersecrettoken@github.com/acme/rocket.git")
        self.assertEqual(parsed["repo"], "acme/rocket")
        self.assertNotIn("supersecret", json.dumps(parsed))

    def test_local_paths_and_junk_are_rejected(self) -> None:
        for url in ("", "   ", "/srv/git/rocket", "../rocket", "C:\\repos\\rocket", "https://github.com/onlyone", "not a url"):
            with self.subTest(url=url):
                self.assertIsNone(parse_remote_url(url))

    def test_detect_repo_reads_git_config_and_ignores_other_hosts(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        git_dir = repo.root / ".git"
        git_dir.mkdir(exist_ok=True)
        config = git_dir / "config"

        config.write_text('[remote "origin"]\n\turl = git@github.com:acme/rocket.git\n', encoding="utf-8")
        self.assertEqual(detect_repo(repo.root)["repo"], "acme/rocket")

        config.write_text('[remote "origin"]\n\turl = git@gitlab.com:acme/rocket.git\n', encoding="utf-8")
        self.assertIsNone(detect_repo(repo.root))

        config.unlink()
        self.assertIsNone(detect_repo(repo.root))


# ---------------------------------------------------------------------------
# pure metrics
# ---------------------------------------------------------------------------


class CheckVerdictTests(unittest.TestCase):
    def test_conclusions_collapse_to_one_verdict(self) -> None:
        self.assertEqual(check_verdict([]), "none")
        self.assertEqual(check_verdict([{"status": "completed", "conclusion": "success"}]), "passed")
        self.assertEqual(check_verdict([{"status": "completed", "conclusion": "skipped"},
                                        {"status": "completed", "conclusion": "success"}]), "passed")
        self.assertEqual(check_verdict([{"status": "completed", "conclusion": "success"},
                                        {"status": "completed", "conclusion": "failure"}]), "failed")
        self.assertEqual(check_verdict([{"status": "completed", "conclusion": "timed_out"}]), "failed")
        self.assertEqual(check_verdict([{"status": "in_progress", "conclusion": None}]), "running")
        self.assertEqual(check_verdict([{"status": "completed", "conclusion": None}]), "running")

    def test_status_states_map_to_the_same_vocabulary(self) -> None:
        self.assertEqual(status_verdict("success"), "passed")
        self.assertEqual(status_verdict("failure"), "failed")
        self.assertEqual(status_verdict("error"), "failed")
        self.assertEqual(status_verdict("pending"), "running")
        self.assertEqual(status_verdict(""), "none")


class PullMetricTests(unittest.TestCase):
    def test_quality_metrics(self) -> None:
        stats = summarise_pulls(PULLS, CHECKS, now=NOW)
        self.assertEqual(stats["merged"], 3)
        self.assertEqual(stats["closed_unmerged"], 1)
        self.assertEqual(stats["open"], 2)
        self.assertEqual(stats["draft"], 1)
        self.assertEqual(stats["merge_rate"], 0.75)
        self.assertEqual(stats["reverted"], 1)
        self.assertEqual(stats["revert_rate"], 0.333)
        self.assertEqual(stats["median_merge_hours"], 12.0)
        self.assertEqual(stats["p90_merge_hours"], 40.8)
        self.assertEqual(stats["stale_open"], 1)          # only #5 is older than 7 days
        self.assertEqual(stats["unreviewed_open"], 2)
        self.assertAlmostEqual(stats["oldest_open_days"], 10.0, places=1)

    def test_ci_before_merge_is_measured_only_where_ci_exists(self) -> None:
        stats = summarise_pulls(PULLS, CHECKS, now=NOW)
        self.assertEqual(stats["ci_green_merged"], 2)
        self.assertEqual(stats["ci_red_merged"], 1)
        self.assertEqual(stats["ci_judged"], 3)
        self.assertEqual(stats["ci_green_rate"], 0.667)
        self.assertEqual(stats["ci_running_open"], 1)
        self.assertEqual(stats["ci_failing_open"], 0)

        bare = summarise_pulls(PULLS, {}, now=NOW)
        self.assertEqual(bare["ci_judged"], 0)
        self.assertIsNone(bare["ci_green_rate"])          # not 0% — simply unknown

    def test_merge_rate_is_none_when_nothing_was_decided(self) -> None:
        stats = summarise_pulls([pull for pull in PULLS if pull["state"] == "open"], {}, now=NOW)
        self.assertIsNone(stats["merge_rate"])
        self.assertIsNone(stats["median_merge_hours"])

    def test_revert_detection(self) -> None:
        self.assertTrue(is_revert('Revert "Cache parsed batches"'))
        self.assertTrue(is_revert("revert: drop the shim"))
        self.assertFalse(is_revert("Add a revert-detection helper"))
        self.assertEqual(revert_target('Revert "Cache parsed batches" (#12)'), "Cache parsed batches")


class IssueMetricTests(unittest.TestCase):
    def test_backlog_shape_and_label_ranking(self) -> None:
        stats = summarise_issues(ISSUES, now=NOW)
        self.assertEqual(stats["open"], 2)
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["stale_open"], 1)
        self.assertEqual(stats["unlabelled_open"], 0)
        self.assertEqual(stats["labels"][0], {"name": "bug", "count": 2})
        self.assertEqual([label["name"] for label in stats["labels"]], ["bug", "enhancement"])


class TrendTests(unittest.TestCase):
    def test_buckets_are_iso_weeks_ending_today(self) -> None:
        buckets = week_buckets(8, now=NOW)
        self.assertEqual(len(buckets), 8)
        self.assertEqual(buckets[-1]["start"], "2026-10-05T00:00:00+00:00")   # Monday of the current week
        self.assertEqual(buckets[0]["start"], "2026-08-17T00:00:00+00:00")
        self.assertEqual(buckets[-1]["label"], "05 Oct")
        self.assertEqual(len(week_buckets(1, now=NOW)), 1)

    def test_counts_land_in_the_right_week(self) -> None:
        buckets = trend_series(week_buckets(4, now=NOW), PULLS, ISSUES, CHECKS)
        self.assertEqual([bucket["label"] for bucket in buckets], ["14 Sep", "21 Sep", "28 Sep", "05 Oct"])
        by_label = {bucket["label"]: bucket for bucket in buckets}

        quiet = by_label["14 Sep"]
        self.assertEqual((quiet["opened"], quiet["merged"]), (0, 0))
        self.assertEqual(quiet["issues_opened"], 0)      # issue #10 predates the window

        # #3 opened and merged (as a revert); #5 was opened on the Sunday
        week_two = by_label["21 Sep"]
        self.assertEqual((week_two["opened"], week_two["merged"], week_two["reverted"]), (2, 1, 1))

        # #1, #2, #4 and #6 were opened; #1 and #2 merged, #4 closed unmerged, #2 merged red
        week_three = by_label["28 Sep"]
        self.assertEqual((week_three["opened"], week_three["merged"], week_three["closed_unmerged"]), (4, 2, 1))
        self.assertEqual(week_three["ci_red_merged"], 1)

        # only issue #11 opened in the newest (partial) week
        week_four = by_label["05 Oct"]
        self.assertEqual((week_four["opened"], week_four["merged"]), (0, 0))
        self.assertEqual(week_four["issues_opened"], 1)

    def test_empty_input_gives_zeroed_weeks(self) -> None:
        buckets = trend_series(week_buckets(3, now=NOW), [], [], {})
        self.assertEqual(len(buckets), 3)
        self.assertTrue(all(bucket["merged"] == 0 for bucket in buckets))


class ListAndPulseTests(unittest.TestCase):
    def test_queue_and_merge_entries_are_capped_and_flagged(self) -> None:
        queue = queue_entries(PULLS, CHECKS, now=NOW, limit=1)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["number"], 6)           # most recently updated open PR
        self.assertTrue(queue[0]["draft"])

        merges = merge_entries(PULLS, CHECKS, limit=2)
        self.assertEqual([row["number"] for row in merges], [2, 1])   # newest merge first
        self.assertEqual(merges[0]["checks"], "failed")

        reverts = merge_entries(PULLS, CHECKS, limit=10)
        self.assertTrue(reverts[2]["revert"])
        self.assertEqual(reverts[2]["reverts"], "Cache parsed batches")

    def test_authors_are_ranked_by_merges(self) -> None:
        authors = summarise_authors(PULLS)
        self.assertEqual(authors[0]["login"], "alok")
        self.assertEqual(authors[0]["merged"], 3)
        self.assertEqual(authors[0]["open"], 2)

    def test_workflow_runs_summary(self) -> None:
        runs = StubClient().workflow_runs("acme/rocket", limit=8)
        summary = summarise_workflow_runs(runs, limit=5)
        self.assertEqual((summary["passed"], summary["failed"]), (1, 1))
        self.assertEqual(len(summary["recent"]), 2)


# ---------------------------------------------------------------------------
# client behaviour (still no network)
# ---------------------------------------------------------------------------


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(repo_root=REPO_ROOT)
        self.client = GitHubClient(self.settings)

    def test_absent_ci_is_not_reported_as_running(self) -> None:
        """GitHub answers ``state: pending`` for a commit with no statuses at all."""
        self.client.get = lambda path, params=None: {"state": "pending", "statuses": [], "total_count": 0}
        self.assertEqual(self.client.check_verdict_for("acme/rocket", _sha("1")), "none")

        self.client.get = lambda path, params=None: {"state": "pending", "statuses": [{"state": "pending"}], "total_count": 1}
        self.assertEqual(self.client.check_verdict_for("acme/rocket", _sha("1")), "running")

        self.client.get = lambda path, params=None: {"state": "success", "statuses": [{"state": "success"}], "total_count": 1}
        self.assertEqual(self.client.check_verdict_for("acme/rocket", _sha("1")), "passed")

    def test_check_runs_take_precedence_over_statuses(self) -> None:
        def fake(path: str, params=None):
            if path.endswith("/check-runs"):
                return {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]}
            raise AssertionError("the classic status endpoint should not be consulted")

        self.client.get = fake
        self.assertEqual(self.client.check_verdict_for("acme/rocket", _sha("1")), "failed")

    def test_missing_shas_are_not_fetched(self) -> None:
        self.client.check_verdict_for = lambda repo, sha: "passed"
        self.assertEqual(self.client.check_verdicts("acme/rocket", [], budget=5), {})

    def test_check_budget_is_respected(self) -> None:
        seen: list[str] = []

        def fake(repo: str, sha: str) -> str:
            seen.append(sha)
            return "passed"

        self.client.check_verdict_for = fake
        shas = [_sha(character) for character in "abcdefgh"]
        verdicts = self.client.check_verdicts("acme/rocket", shas, budget=3)
        self.assertEqual(len(seen), 3)
        self.assertEqual(set(verdicts), set(shas[:3]))

    def test_duplicate_shas_are_fetched_once(self) -> None:
        seen: list[str] = []

        def fake(repo: str, sha: str) -> str:
            seen.append(sha)
            return "passed"

        self.client.check_verdict_for = fake
        self.client.check_verdicts("acme/rocket", [_sha("a"), _sha("a"), _sha("b")], budget=5)
        self.assertCountEqual(seen, [_sha("a"), _sha("b")])   # the pool runs concurrently

    def test_http_errors_are_explained_in_plain_language(self) -> None:
        import urllib.error

        def make(status: int, message: str):
            import io
            return urllib.error.HTTPError("https://api.github.com/x", status, message, {}, io.BytesIO(json.dumps({"message": message}).encode()))

        cases = {
            401: "rejected the token",
            404: "not found",
            403: "rate limit",
        }
        for status, fragment in cases.items():
            with self.subTest(status=status):
                message = "API rate limit exceeded" if status == 403 else "nope"
                error = self.client._http_error(make(status, message))
                self.assertIsInstance(error, GitHubError)
                self.assertEqual(error.status, status)
                self.assertIn(fragment, str(error))
                self.assertTrue(error.hint)

    def test_rate_limit_headers_are_recorded(self) -> None:
        self.client._record_rate({"x-ratelimit-remaining": "7", "x-ratelimit-limit": "5000", "x-ratelimit-reset": "1700000000"})
        self.assertEqual(self.client.rate_limit["remaining"], 7)
        self.assertTrue(self.client.rate_limit["reset_at"].endswith("+00:00"))


# ---------------------------------------------------------------------------
# collector: caching, degradation, secrets
# ---------------------------------------------------------------------------


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.client = StubClient()
        self.insights = insights_for(self.repo, self.client)

    def test_happy_path_payload_shape(self) -> None:
        payload = self.insights.load()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["repository"], "acme/rocket")
        self.assertEqual(payload["repo"]["full_name"], "acme/rocket")
        self.assertEqual(payload["pulls"]["merged"], 3)
        self.assertEqual(payload["issues"]["open"], 2)
        self.assertEqual(len(payload["trend"]), self.repo.settings.github.weeks)
        self.assertEqual(payload["ci"]["head"]["sha"], _sha("9")[:7])
        self.assertEqual(payload["authors"][0]["login"], "alok")
        self.assertEqual(payload["settings"]["check_budget"], self.repo.settings.github.check_budget)
        self.assertNotIn("token", json.dumps(payload).lower())

    def test_snapshot_is_written_for_offline_reads(self) -> None:
        self.insights.load()
        self.assertTrue(self.insights.snapshot_path.is_file())
        stored = json.loads(self.insights.snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["repository"], "acme/rocket")

    def test_second_read_is_served_from_cache(self) -> None:
        self.insights.load()
        calls = list(self.client.calls)
        payload = self.insights.load()
        self.assertEqual(self.client.calls, calls)          # no new HTTP work
        self.assertTrue(payload["from_cache"])
        self.assertGreaterEqual(payload["age_seconds"], 0)

    def test_refresh_bypasses_the_cache(self) -> None:
        self.insights.load()
        before = len(self.client.calls)
        self.insights.load(force=True)
        self.assertGreater(len(self.client.calls), before)

    def test_a_failed_sync_serves_the_last_snapshot_flagged_stale(self) -> None:
        self.insights.load()
        self.client.failure = GitHubError("GitHub rate limit reached (403)", status=403, hint="Resets at 04:34 UTC")
        payload = self.insights.load(force=True)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["reason"], "stale_snapshot")
        self.assertIn("rate limit", payload["error"])
        self.assertEqual(payload["pulls"]["merged"], 3)      # last good numbers survive
        self.assertTrue(payload["configured"])

    def test_a_failed_sync_without_a_snapshot_degrades_to_an_explanation(self) -> None:
        client = StubClient(failure=GitHubError("could not reach https://api.github.com", hint="timed out"))
        insights = insights_for(self.repo, client)
        payload = insights.load()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["reason"], "unavailable")
        self.assertIn("could not reach", payload["error"])
        self.assertEqual(payload["pulls"], {})               # nothing invented

    def test_unexpected_errors_never_escape_into_the_request(self) -> None:
        client = StubClient()
        client.repository = lambda repo: (_ for _ in ()).throw(RuntimeError("boom"))
        payload = insights_for(self.repo, client).load()
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["error"])

    def test_no_repository_is_reported_without_any_http_call(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        settings = repo.settings
        settings.github.repo = ""
        client = StubClient()
        payload = GitHubInsights(settings, client=client).load()
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["reason"], "no_repository")
        self.assertEqual(client.calls, [])
        self.assertIn("FIXPILOT_GITHUB_REPO", payload["hint"])

    def test_repository_is_detected_from_the_git_remote(self) -> None:
        git_dir = self.repo.root / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "config").write_text('[remote "origin"]\n\turl = https://github.com/acme/rocket.git\n', encoding="utf-8")
        insights = insights_for(self.repo, self.client)
        insights.settings.github.repo = ""
        self.assertEqual(insights.resolve_repo(), "acme/rocket")
        self.assertEqual(insights.load()["repository"], "acme/rocket")

    def test_anonymous_reads_can_be_switched_off(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        settings = repo.settings
        settings.github.repo = "acme/rocket"
        settings.github.token = ""
        settings.github.anonymous = False
        payload = GitHubInsights(settings, client=StubClient(authenticated=False)).load()
        self.assertEqual(payload["reason"], "no_token")
        self.assertIn("FIXPILOT_GITHUB_TOKEN", payload["hint"])

    def test_the_token_is_never_part_of_the_payload_or_the_settings(self) -> None:
        secret = "github_pat_11ABCDEFG0123456789_secret"
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        settings = repo.settings
        settings.github.repo = "acme/rocket"
        settings.github.token = secret
        payload = GitHubInsights(settings, client=StubClient()).load()
        self.assertNotIn(secret, json.dumps(payload))
        self.assertNotIn(secret, json.dumps(settings.public()))
        self.assertNotIn(secret, json.dumps(settings.github.public()))
        self.assertTrue(settings.github.public()["token_present"])

    def test_weeks_can_be_overridden_per_request(self) -> None:
        payload = self.insights.load(weeks=3)
        self.assertEqual(len(payload["trend"]), 3)
        self.assertEqual(payload["settings"]["weeks"], self.repo.settings.github.weeks)  # configured default is untouched


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------


class GitHubRouteTests(unittest.TestCase):
    """Drives ``FixPilotServer.dispatch`` directly — no socket, no network."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.settings = self.repo.settings
        self.settings.github.repo = "acme/rocket"
        self.settings.github.detail_budget = 0
        self.app = FixPilotServer(FixPilotAgent(self.settings), self.settings)
        self.app.github = GitHubInsights(self.settings, client=StubClient())
        self.assertTrue(self.app.auth_enabled)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def call(self, method: str, path: str, *, body=None, token: str = "", cookie=None):
        return self.app.dispatch(Request(method=method, path=path, body=body or {}, token=token, cookie=cookie or {}))

    def sign_in(self) -> dict:
        response = self.call("POST", "/api/auth/setup", body={"username": "alok", "password": "correct-horse-9"})
        cookie = response.set_cookie.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]
        return {SESSION_COOKIE: cookie}

    def test_requires_a_session(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.call("GET", "/api/github")
        self.assertEqual(caught.exception.status, 401)

    def test_serves_the_insights_once_signed_in(self) -> None:
        payload = json.loads(json.dumps(self.call("GET", "/api/github", cookie=self.sign_in()).payload))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pulls"]["merged"], 3)
        self.assertFalse(payload["settings"]["weeks"] is None)

    def test_weeks_query_is_honoured_and_validated(self) -> None:
        cookie = self.sign_in()
        request = Request(method="GET", path="/api/github", query={"weeks": ["4"]}, cookie=cookie)
        self.assertEqual(len(self.app.dispatch(request).payload["trend"]), 4)

        bad = Request(method="GET", path="/api/github", query={"weeks": ["soon"]}, cookie=cookie)
        with self.assertRaises(ApiError) as caught:
            self.app.dispatch(bad)
        self.assertEqual(caught.exception.status, 400)

    def test_the_mutation_token_and_github_token_never_reach_the_response(self) -> None:
        secret = "github_pat_11ABCDEFG0123456789_secret"
        self.settings.github.token = secret
        cookie = self.sign_in()
        body = json.dumps(self.call("GET", "/api/github", cookie=cookie).payload)
        self.assertNotIn(secret, body)
        self.assertNotIn(self.app.token, body)
        page = self.call("GET", "/", cookie=cookie).raw.decode("utf-8")
        self.assertIn(self.app.token, page)               # the app's own token is inlined on purpose
        self.assertNotIn(secret, page)


# ---------------------------------------------------------------------------
# static contract with the PWA shell
# ---------------------------------------------------------------------------


class WebShellContractTests(unittest.TestCase):
    """The GitHub tab is only useful if its DOM and its script agree."""

    def setUp(self) -> None:
        self.html = (WEB / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "styles.css").read_text(encoding="utf-8")

    def test_every_element_app_js_reaches_for_exists_in_the_page(self) -> None:
        referenced = set(re.findall(r'\$\("([^"]+)"\)', self.js))
        declared = set(re.findall(r'\bid="([^"]+)"', self.html))
        self.assertTrue(referenced)
        self.assertEqual(sorted(referenced - declared), [])

    def test_the_github_tab_is_wired_to_the_github_view(self) -> None:
        self.assertIn('data-view="github"', self.html)
        self.assertIn('id="view-github"', self.html)
        self.assertIn('if (name === "github") loadGitHub(false);', self.js)
        self.assertIn('api("GET", `/api/github${query}`)', self.js)

    def test_the_view_renders_into_elements_the_script_fills(self) -> None:
        for element in ("gh-kpis", "gh-trend", "gh-queue", "gh-merges", "gh-issues-stats", "gh-pulse", "gh-summary"):
            with self.subTest(element=element):
                self.assertIn(f'id="{element}"', self.html)
                self.assertIn(f'$("{element}")', self.js)

    def test_the_trend_bars_have_styles_to_stand_on(self) -> None:
        for selector in (".trend", ".tcol", ".tbar", ".tlabels", ".gh-list"):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)


class SettingsSurfaceTests(unittest.TestCase):
    def test_github_settings_are_public_but_never_the_token(self) -> None:
        settings = Settings(repo_root=REPO_ROOT)
        public = settings.github.public()
        self.assertIn("token_present", public)
        self.assertNotIn("token", public)
        self.assertIsInstance(public["token_present"], bool)

    def test_overview_exposes_the_github_block(self) -> None:
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        settings = repo.settings
        settings.github.repo = "acme/rocket"
        overview = FixPilotAgent(settings).overview()
        self.assertEqual(overview["settings"]["github"]["repo"], "acme/rocket")

    def test_the_data_directory_covers_the_snapshot(self) -> None:
        settings = Settings(repo_root=REPO_ROOT)
        self.assertEqual(settings.github_dir.name, "github")
        self.assertTrue(str(settings.github_dir).startswith(str(settings.data_dir)))


if __name__ == "__main__":
    unittest.main()
