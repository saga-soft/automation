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
import os
import shutil
import subprocess
import sys

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "lock_threads.yaml"
REPORT_PATH = Path(__file__).parent / "lock_threads_report.md"
REF_NOW_TIME = datetime.now(UTC)

MAX_API_CALLS_PER_RUN = 70  # Rate limit is 5000 per hour, 83 per minute
SEARCH_PAGE_SIZE = 100
MAX_SEARCH_PAGES = 3

LOCK_REASON = "resolved"
KIND_TABLE = (("issue", "issues"), ("pr", "prs"))
KIND_LABELS = {"issue": "Issues", "pr": "Pull Requests"}
KIND_URL_PATH = {"issue": "issues", "pr": "pull"}
RATE_LIMIT_MARKERS = ("rate limit exceeded", "secondary rate limit")


class StopEarly(RuntimeError):
    """Base for conditions that stop a run early without counting as a failure."""


class RateLimitExceeded(StopEarly):
    """Raised when the GitHub API reports a primary or secondary rate limit."""


class CallBudgetExceeded(StopEarly):
    """Raised when this run's own MAX_API_CALLS_PER_RUN budget is used up."""


@dataclass
class ThreadConfig:
    """Settings for one thread kind (issue or pr) within a repo group."""

    name: str
    kind: str  # "issue" or "pr"
    inactive_days: int
    comment: str | None = None
    look_forward_days: int = 0


@dataclass
class Candidate:
    """A closed, unlocked thread found by search, paired with its config."""

    repo: str
    number: int
    updated_at: datetime
    config: ThreadConfig
    queued_in: int = 0


def stop_reason(exc: StopEarly) -> str:
    """Return the report-facing phrase describing why a run stopped early."""
    if isinstance(exc, RateLimitExceeded):
        return "Hit a GitHub API rate limit"
    return f"Reached this run's {MAX_API_CALLS_PER_RUN}-call API budget"


def cutoff_timestamp(days: int) -> str:
    """Return the ISO timestamp `days` before now, for use in a search query."""
    dt = REF_NOW_TIME - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(timestamp: str) -> datetime:
    """Parse a GitHub API ISO 8601 UTC timestamp into an aware datetime."""
    return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def search_issue_like(client: GitHubClient, repo: str, kind: str, cutoff: str) -> list[tuple[int, datetime]]:
    """Search for closed, unlocked issues or PRs in repo updated before cutoff.

    GitHub's `is:unlocked` search qualifier can lag reality (it has reported
    already-locked threads as unlocked), so results are re-checked against
    the `locked` field below. Paginating past the first page keeps those
    stale entries from crowding genuine candidates out of the results.
    """
    gh_kind = "pr" if kind == "pr" else "issue"
    query = f"repo:{repo} updated:<{cutoff} is:closed is:unlocked is:{gh_kind}"
    found: list[tuple[int, datetime]] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        result = client.get(
            "/search/issues",
            {"q": query, "sort": "updated", "order": "asc", "per_page": SEARCH_PAGE_SIZE, "page": page},
        )
        items = result.get("items", [])
        found.extend((item["number"], parse_timestamp(item["updated_at"])) for item in items if not item.get("locked"))
        if len(items) < SEARCH_PAGE_SIZE:
            break
    return found


def candidate_label(candidate: Candidate) -> str:
    """Return the plain-text label identifying a candidate thread."""
    return f"{candidate.repo} {candidate.config.kind} #{candidate.number}"


def repo_link(repo: str) -> str:
    """Return the repo name as a markdown link to its GitHub page."""
    return f"[{repo}](https://github.com/{repo})"


def candidate_link(candidate: Candidate) -> str:
    """Return the candidate's label as a markdown link to its GitHub thread."""
    return (
        f"[{candidate_label(candidate)}]"
        f"(https://github.com/{candidate.repo}/{KIND_URL_PATH[candidate.config.kind]}/{candidate.number})"
    )


def format_timestamp(dt: datetime) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM:SS'."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def days_since(dt: datetime) -> int:
    """Return the number of whole days between dt and now."""
    return (REF_NOW_TIME - dt).days


def days(days: int) -> str:
    """Format a days count."""
    return f"{days} day" + ("s" if days != 1 else "")


class GitHubClient:
    """Thin wrapper around `gh api`, reusing gh's own authentication."""

    def __init__(self) -> None:
        self.call_count = 0

    def _request(self, method: str, path: str, fields: dict | None = None) -> dict:
        if self.call_count >= MAX_API_CALLS_PER_RUN:
            raise CallBudgetExceeded(f"Reached the {MAX_API_CALLS_PER_RUN}-call budget for this run")
        self.call_count += 1
        args = ["gh", "api", path, "-X", method, "-H", "Accept: application/vnd.github+json"]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = result.stderr.strip()
            if any(marker in message.lower() for marker in RATE_LIMIT_MARKERS):
                raise RateLimitExceeded(message)
            raise RuntimeError(f"gh api {method} {path} failed: {message}")
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


class LockThreadsWorker:
    """The worker class to process the lock threads jobs."""

    def __init__(self, config: Path, client: GitHubClient, dry_run: bool) -> None:
        self._client = client
        self._dry_run = dry_run

        self._repo_conf: dict[str, list[ThreadConfig]] = defaultdict(list)
        self._candidates: list[Candidate] = []
        self._results: list[tuple[Candidate, str, str | None]] = []

        self._error = ""
        self._stop_note: str | None = None

        # Load Config
        with config.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
            raw_conf = data.get("repo", [])
            if not raw_conf:
                self._error = "Config has no 'repo' entries"
                return

        self._build_thread_configs(raw_conf)

    @property
    def repo_count(self) -> int:
        """Return the number of repos."""
        return len(self._repo_conf)

    @property
    def candidate_count(self) -> int:
        """Return the number of candidates."""
        return len(self._candidates)

    @property
    def error(self) -> str:
        """Return the error message if any."""
        return self._error

    ##
    #  Methods
    ##

    def gather_candidates(self) -> None:
        """Search every configured repo/kind and store the candidates to process.

        Candidates are grouped per repo (oldest first within each repo), then
        interleaved round-robin across repos so each repo gets a fair share of
        the queue instead of one repo's backlog crowding out the others.

        A thread config's `look_forward_days` widens the search to also pick up
        threads that aren't old enough to lock yet but will cross `inactive_days`
        within that many days; those are stored as `queued` candidates rather
        than ones ready to lock.

        If a rate limit or this run's call budget is hit partway through,
        searching stops, whatever was already found is kept, and the stop
        note is set for the report.
        """
        by_repo: dict[str, list[Candidate]] = {}
        for repo, configs in self._repo_conf.items():
            repo_candidates: list[Candidate] = []
            for tc in configs:
                search_days = max(tc.inactive_days - tc.look_forward_days, 0)
                try:
                    found = search_issue_like(self._client, repo, tc.kind, cutoff_timestamp(search_days))
                except StopEarly as exc:
                    self._stop_note = stop_reason(exc)
                    print(f"Stopping search early ({self._stop_note}) at {repo} [{tc.kind}]: {exc}", file=sys.stderr)
                    break

                repo_candidates.extend(
                    Candidate(repo, number, updated_at, tc, queued_in=max(0, tc.inactive_days - days_since(updated_at)))
                    for number, updated_at in found
                )

            repo_candidates.sort(key=lambda c: c.updated_at)  # oldest / most overdue first, within this repo
            by_repo[repo] = repo_candidates

            if self._stop_note:
                break

        rounds = zip_longest(*by_repo.values())
        self._candidates = [candidate for round_ in rounds for candidate in round_ if isinstance(candidate, Candidate)]

    def process_candidates(self) -> list[str]:
        """Comment on and lock the gathered candidates, and return any error messages."""
        # If searching already used up the run's call budget (or hit a rate
        # limit), don't attempt any locks/comments — they'd just fail too.
        to_process = [] if self._stop_note else self._candidates

        errors = []
        self._results = []
        for candidate in to_process:
            if candidate.queued_in > 0:
                self._results.append((candidate, f"Locking in {days(candidate.queued_in)}", None))
                continue
            try:
                self._process_candidate(candidate)
                self._results.append((candidate, "Would lock" if self._dry_run else "Locked", None))
            except StopEarly as exc:
                self._stop_note = stop_reason(exc)
                print(
                    f"Stopping run early ({self._stop_note}) after {len(self._results)} thread(s): {exc}",
                    file=sys.stderr,
                )
                return errors
            except Exception as exc:
                errors.append(f"{candidate.repo} {candidate.config.kind} #{candidate.number}: {exc}")
                self._results.append((candidate, "Error", str(exc)))
                print(
                    f"ERROR locking {candidate.repo} {candidate.config.kind} #{candidate.number}: {exc}",
                    file=sys.stderr,
                )

        return errors

    def build_report(self) -> str:
        """Build a markdown report summarizing what was (or would be) locked."""
        lines = ["## Lock Threads Report", ""]
        lines.append(f"**Mode:** {'Dry Run' if self._dry_run else 'Live'}  ")
        lines.append(f"**Time:** {format_timestamp(REF_NOW_TIME)} (UTC)  ")
        lines.append(f"**Found:** {len(self._candidates)} inactive thread(s) across {len(self._repo_conf)} repo(s)  ")

        skipped = len(self._candidates) - len(self._results)
        if skipped > 0:
            lines.append(f"**Deferred:** {skipped} thread(s) to the next run  ")
        if self._stop_note:
            lines.append(
                f"**Note:** Stopped early: {self._stop_note}. Remaining threads are deferred to the next run.  "
            )
        lines.append("")

        if not self._results:
            lines.append("No threads processed.")
            return "\n".join(lines) + "\n"

        lines.append("### Checking Repos")
        lines.append("")
        lines.append("| Repo | Target | Max Age | Forward |")
        lines.append("|---|---|---|---|")
        for repo, configs in self._repo_conf.items():
            lines.extend(
                f"| {repo_link(repo)} | {KIND_LABELS.get(tc.kind, 'ERR')}"
                f" | {days(tc.inactive_days)} | {days(tc.look_forward_days)} |"
                for tc in configs
            )
        lines.append("")

        lines.append("### Results")
        lines.append("")
        lines.append("| Thread | Last Updated (UTC) | Age | Comment | Status |")
        lines.append("|---|---|---|---|---|")
        for candidate, status, detail in self._results:
            comment = "Yes" if candidate.config.comment else "No"
            status_text = f"{status}: {detail}" if detail else status
            lines.append(
                f"| {candidate_link(candidate)} | {format_timestamp(candidate.updated_at)} "
                f"| {days(days_since(candidate.updated_at))} | {comment} | {status_text} |"
            )
        lines.append("")

        return "\n".join(lines) + "\n"

    ##
    #  Internal Functions
    ##

    def _build_thread_configs(self, raw_conf: list[Any]) -> None:
        """Build the per-kind thread configs for every configured repo."""
        seen: set[str] = set()
        for repo_table in raw_conf:
            repo = repo_table["name"]
            if repo in seen:
                raise ValueError(f"{repo}: duplicate repo entry")
            seen.add(repo)
            for kind, key in KIND_TABLE:
                table = repo_table.get(key)
                if not table:
                    continue
                inactive_days = table.get("inactive_days")
                if inactive_days is None:
                    raise ValueError(f"{repo} [{key}]: missing 'inactive_days'")
                self._repo_conf[repo].append(
                    ThreadConfig(
                        name=repo,
                        kind=kind,
                        inactive_days=inactive_days,
                        comment=table.get("comment"),
                        look_forward_days=table.get("look_forward_days", 0),
                    )
                )

    def _process_candidate(self, candidate: Candidate) -> None:
        """Comment (if configured) and lock a single candidate thread."""
        label = candidate_label(candidate)
        if self._dry_run:
            print(f"[dry-run] Would lock {label}")
            return

        if candidate.config.comment:
            self._client.post(
                f"/repos/{candidate.repo}/issues/{candidate.number}/comments", {"body": candidate.config.comment}
            )
        self._client.put(f"/repos/{candidate.repo}/issues/{candidate.number}/lock", {"lock_reason": LOCK_REASON})

        print(f"Locked {label}")


def main() -> None:
    """Gather inactive threads across all configured repos and lock the top ones."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Search and report, but don't comment or lock")
    args = parser.parse_args()

    # Check that the gh command is available and can authenticate
    if shutil.which("gh") is None:
        sys.exit("gh CLI not found; install it from https://cli.github.com")
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit("gh CLI is not authenticated; run `gh auth login` (or set GH_TOKEN)")

    # Set up the worker
    client = GitHubClient()
    worker = LockThreadsWorker(CONFIG_PATH, client, bool(args.dry_run))
    if error := worker.error:
        parser.error(error)

    # Gather candidates for locking
    worker.gather_candidates()
    print(f"Found {worker.candidate_count} inactive thread(s) across {worker.repo_count} repo(s)")

    errors = worker.process_candidates()
    report = worker.build_report()

    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\nWrote report to {REPORT_PATH}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report)

    if errors:
        print(f"\n{len(errors)} error(s) occurred", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
