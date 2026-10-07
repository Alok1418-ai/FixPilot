"""Read-only integrations that feed FixPilot's dashboards.

Today: :mod:`~fixpilot.integrations.github` — pull requests, issues, CI check
runs and delivery cadence for the repository FixPilot is serving.

Everything here follows the same rules as the rest of the product: standard
library only, no writes to the remote, secrets never leave the server, and a
network failure degrades to cached data instead of a broken screen.
"""

from .github import GitHubClient, GitHubError, GitHubInsights, parse_remote_url

__all__ = ["GitHubClient", "GitHubError", "GitHubInsights", "parse_remote_url"]
