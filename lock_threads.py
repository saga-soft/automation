#!/usr/bin/env python3
"""Lock inactive closed issues and PRs across repos, configured via lock_threads.yaml.

Authenticates through the gh CLI, so it works locally under `gh auth login`
as well as in CI, where GH_TOKEN is set on the environment.

Usage:
    lock_threads.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "lock_threads.yaml"
MAX_ACTIONS_PER_RUN = 50
LOCK_REASON = "resolved"
KIND_TABLE = (("issue", "issues"), ("pr", "prs"))


@dataclass
class ThreadConfig:
    """Settings for one thread kind (issue or pr) within a repo group."""

    kind: str  # "issue" or "pr"
    inactive_days: int
    comment: str | None = None


@dataclass
class Candidate:
    """A closed, unlocked thread found by search, paired with its config."""

    repo: str
    number: int
    updated_at: str
    config: ThreadConfig


class GitHubClient:
    """Thin wrapper around `gh api`, reusing gh's own authentication."""

    def _request(self, method: str, path: str, fields: dict | None = None) -> dict:
        args = ["gh", "api", path, "-X", method, "-H", "Accept: application/vnd.github+json"]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"gh api {method} {path} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def get(self, path: str, params: dict) -> dict:
        """Send a GET request via gh api and return the parsed JSON response."""
        return self._request("GET", path, params)

    def post(self, path: str, payload: dict) -> dict:
        """Send a POST request via gh api and return the parsed JSON response."""
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict) -> dict:
        """Send a PUT request via gh api and return the parsed JSON response."""
        return self._request("PUT", path, payload)


def load_config(path: Path) -> dict:
    """Load and parse the YAML config file at path."""
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build_thread_configs(repo_table: dict) -> list[ThreadConfig]:
    """Build the per-kind thread configs declared for one repo group."""
    repo = repo_table["name"]
    configs = []
    for kind, key in KIND_TABLE:
        table = repo_table.get(key)
        if not table:
            continue
        inactive_days = table.get("inactive_days")
        if inactive_days is None:
            raise ValueError(f"{repo} [{key}]: missing 'inactive_days'")
        configs.append(
            ThreadConfig(
                kind=kind,
                inactive_days=inactive_days,
                comment=table.get("comment"),
            )
        )
    return configs


def cutoff_timestamp(days: int) -> str:
    """Return the ISO timestamp `days` before now, for use in a search query."""
    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def search_issue_like(client: GitHubClient, repo: str, kind: str, cutoff: str) -> list[tuple[int, str]]:
    """Search for closed, unlocked issues or PRs in repo updated before cutoff."""
    gh_kind = "pr" if kind == "pr" else "issue"
    query = f"repo:{repo} updated:<{cutoff} is:closed is:unlocked is:{gh_kind}"
    result = client.get("/search/issues", {"q": query, "sort": "updated", "order": "asc", "per_page": 50})
    return [(item["number"], item["updated_at"]) for item in result.get("items", []) if not item.get("locked")]


def gather_candidates(client: GitHubClient, repos_cfg: list[dict]) -> list[Candidate]:
    """Search every configured repo/kind and return candidates, oldest first."""
    candidates: list[Candidate] = []
    for repo_table in repos_cfg:
        repo = repo_table["name"]
        for tc in build_thread_configs(repo_table):
            cutoff = cutoff_timestamp(tc.inactive_days)
            for number, updated_at in search_issue_like(client, repo, tc.kind, cutoff):
                candidates.append(Candidate(repo, number, updated_at, tc))
    candidates.sort(key=lambda c: c.updated_at)  # oldest / most overdue first
    return candidates


def process_candidate(client: GitHubClient, candidate: Candidate, dry_run: bool) -> None:
    """Comment (if configured) and lock a single candidate thread."""
    label = f"{candidate.repo} {candidate.config.kind} #{candidate.number}"
    if dry_run:
        print(f"[dry-run] Would lock {label}")
        return

    if candidate.config.comment:
        client.post(f"/repos/{candidate.repo}/issues/{candidate.number}/comments", {"body": candidate.config.comment})
    client.put(f"/repos/{candidate.repo}/issues/{candidate.number}/lock", {"lock_reason": LOCK_REASON})

    print(f"Locked {label}")


def ensure_gh_available() -> None:
    """Exit with a clear message if the gh CLI is missing or unauthenticated."""
    if shutil.which("gh") is None:
        sys.exit("gh CLI not found; install it from https://cli.github.com")
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit("gh CLI is not authenticated; run `gh auth login` (or set GH_TOKEN)")


def main() -> None:
    """Gather inactive threads across all configured repos and lock the top ones."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Search and report, but don't comment or lock")
    args = parser.parse_args()

    ensure_gh_available()

    config = load_config(CONFIG_PATH)
    repos_cfg = config.get("repo", [])
    if not repos_cfg:
        parser.error("config has no 'repo' entries")

    client = GitHubClient()
    candidates = gather_candidates(client, repos_cfg)
    print(f"Found {len(candidates)} inactive thread(s) across {len(repos_cfg)} repo(s)")

    to_process = candidates[:MAX_ACTIONS_PER_RUN]
    skipped = len(candidates) - len(to_process)
    if skipped > 0:
        print(
            f"Capping this run to the {MAX_ACTIONS_PER_RUN} most overdue threads ({skipped} deferred to the next run)"
        )

    errors = []
    for entry in to_process:
        try:
            process_candidate(client, entry, args.dry_run)
        except Exception as exc:
            errors.append(f"{entry.repo} {entry.config.kind} #{entry.number}: {exc}")
            print(f"ERROR locking {entry.repo} {entry.config.kind} #{entry.number}: {exc}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} error(s) occurred", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
