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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "lock_threads.yaml"
REPORT_PATH = Path(__file__).parent / "lock_threads_report.md"
# GitHub's secondary/abuse rate limit is scoped to the calling token across all
# repos it touches, not per repo, so this budget is shared by every GET/POST/PUT
# call the whole run makes (searches, comments, and locks alike).
MAX_API_CALLS_PER_RUN = 70
SEARCH_PAGE_SIZE = 100
# 6 repo/kind searches x 3 pages = 18 calls/run, well under the Search API's
# 30 requests/minute limit even if every search needs to page fully.
MAX_SEARCH_PAGES = 3
LOCK_REASON = "resolved"
KIND_TABLE = (("issue", "issues"), ("pr", "prs"))
KIND_URL_PATH = {"issue": "issues", "pr": "pull"}
RATE_LIMIT_MARKERS = ("rate limit exceeded", "secondary rate limit")


class StopEarly(RuntimeError):
    """Base for conditions that stop a run early without counting as a failure."""


class RateLimitExceeded(StopEarly):
    """Raised when the GitHub API reports a primary or secondary rate limit."""


class CallBudgetExceeded(StopEarly):
    """Raised when this run's own MAX_API_CALLS_PER_RUN budget is used up."""


def stop_reason(exc: StopEarly) -> str:
    """Return the report-facing phrase describing why a run stopped early."""
    if isinstance(exc, RateLimitExceeded):
        return "hit a GitHub API rate limit"
    return f"reached this run's {MAX_API_CALLS_PER_RUN}-call API budget"


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
    updated_at: datetime
    config: ThreadConfig


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
        found.extend(
            (item["number"], parse_timestamp(item["updated_at"])) for item in items if not item.get("locked")
        )
        if len(items) < SEARCH_PAGE_SIZE:
            break
    return found


def interleave_by_repo(by_repo: dict[str, list[Candidate]]) -> list[Candidate]:
    """Round-robin each repo's oldest-first list into one fair processing order.

    Takes one candidate from each repo in turn (each repo's own list already
    sorted oldest first) so a single repo's backlog can't hog every queue
    slot and starve the others, until every list is exhausted.
    """
    rounds = zip_longest(*by_repo.values())
    return [candidate for round_ in rounds for candidate in round_ if candidate is not None]


def gather_candidates(client: GitHubClient, repos_cfg: list[dict]) -> tuple[list[Candidate], str | None]:
    """Search every configured repo/kind and return candidates to process.

    Candidates are grouped per repo (oldest first within each repo), then
    interleaved round-robin across repos so each repo gets a fair share of
    the queue instead of one repo's backlog crowding out the others.

    Returns (candidates, note): if a rate limit or this run's call budget is
    hit partway through, searching stops and whatever was already found is
    returned, with `note` set to a report-facing description of why.
    """
    by_repo: dict[str, list[Candidate]] = {}
    note: str | None = None
    for repo_table in repos_cfg:
        repo = repo_table["name"]
        repo_candidates: list[Candidate] = []
        for tc in build_thread_configs(repo_table):
            cutoff = cutoff_timestamp(tc.inactive_days)
            try:
                found = search_issue_like(client, repo, tc.kind, cutoff)
            except StopEarly as exc:
                note = stop_reason(exc)
                print(f"Stopping search early ({note}) at {repo} [{tc.kind}]: {exc}", file=sys.stderr)
                break

            repo_candidates.extend(Candidate(repo, number, updated_at, tc) for number, updated_at in found)

        repo_candidates.sort(key=lambda c: c.updated_at)  # oldest / most overdue first, within this repo
        by_repo[repo] = repo_candidates

        if note:
            break

    return interleave_by_repo(by_repo), note


def candidate_label(candidate: Candidate) -> str:
    """Return the plain-text label identifying a candidate thread."""
    return f"{candidate.repo} {candidate.config.kind} #{candidate.number}"


def candidate_url(candidate: Candidate) -> str:
    """Return the GitHub URL for a candidate thread."""
    return f"https://github.com/{candidate.repo}/{KIND_URL_PATH[candidate.config.kind]}/{candidate.number}"


def candidate_link(candidate: Candidate) -> str:
    """Return the candidate's label as a markdown link to its GitHub thread."""
    return f"[{candidate_label(candidate)}]({candidate_url(candidate)})"


def process_candidate(client: GitHubClient, candidate: Candidate, dry_run: bool) -> None:
    """Comment (if configured) and lock a single candidate thread."""
    label = candidate_label(candidate)
    if dry_run:
        print(f"[dry-run] Would lock {label}")
        return

    if candidate.config.comment:
        client.post(f"/repos/{candidate.repo}/issues/{candidate.number}/comments", {"body": candidate.config.comment})
    client.put(f"/repos/{candidate.repo}/issues/{candidate.number}/lock", {"lock_reason": LOCK_REASON})

    print(f"Locked {label}")


def format_timestamp(dt: datetime) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM:SS'."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def days_since(dt: datetime) -> int:
    """Return the number of whole days between dt and now."""
    return (datetime.now(UTC) - dt).days


def build_report(
    dry_run: bool,
    total_found: int,
    repo_count: int,
    skipped: int,
    results: list[tuple[Candidate, str, str | None]],
    stop_note: str | None = None,
) -> str:
    """Build a markdown report summarizing what was (or would be) locked."""
    lines = ["## Lock Threads Report", ""]
    lines.append(f"**Mode:** {'Dry Run' if dry_run else 'Live'}  ")
    lines.append(f"**Time:** {format_timestamp(datetime.now(UTC))} (UTC)  ")
    lines.append(f"**Found:** {total_found} inactive thread(s) across {repo_count} repo(s)  ")
    if skipped > 0:
        lines.append(f"**Deferred:** {skipped} thread(s) to the next run  ")
    if stop_note:
        lines.append(f"**Note:** Stopped early: {stop_note}. Remaining threads are deferred to the next run.  ")
    lines.append("")

    if not results:
        lines.append("No threads processed.")
        return "\n".join(lines) + "\n"

    lines.append("| Thread | Last Updated (UTC) | Days | Comment | Status |")
    lines.append("|---|---|---|---|---|")
    for candidate, status, detail in results:
        comment = "Yes" if candidate.config.comment else "No"
        status_text = f"{status}: {detail}" if detail else status
        lines.append(
            f"| {candidate_link(candidate)} | {format_timestamp(candidate.updated_at)} "
            f"| {days_since(candidate.updated_at)} | {comment} | {status_text} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


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
    candidates, stop_note = gather_candidates(client, repos_cfg)
    print(f"Found {len(candidates)} inactive thread(s) across {len(repos_cfg)} repo(s)")

    # If searching already used up the run's call budget (or hit a rate
    # limit), don't attempt any locks/comments — they'd just fail too.
    to_process = [] if stop_note else candidates

    errors = []
    results: list[tuple[Candidate, str, str | None]] = []
    for entry in to_process:
        try:
            process_candidate(client, entry, args.dry_run)
            results.append((entry, "Would lock" if args.dry_run else "Locked", None))
        except StopEarly as exc:
            stop_note = stop_reason(exc)
            print(f"Stopping run early ({stop_note}) after {len(results)} thread(s): {exc}", file=sys.stderr)
            break
        except Exception as exc:
            errors.append(f"{entry.repo} {entry.config.kind} #{entry.number}: {exc}")
            results.append((entry, "Error", str(exc)))
            print(f"ERROR locking {entry.repo} {entry.config.kind} #{entry.number}: {exc}", file=sys.stderr)

    skipped = len(candidates) - len(results)
    report = build_report(args.dry_run, len(candidates), len(repos_cfg), skipped, results, stop_note)

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
