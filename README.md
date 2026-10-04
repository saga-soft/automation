# Automation

Internal repository for the Saga Soft Organisation.

Used for scripts and automated jobs.

## Lock Threads

A workflow and script to lock closed issues and pull requests that have been inactive for a given amount of time.
The tool consists of the following files:

* `.github/workflows/lock-thread.yml`: The workflow job running as a cron job.
* `lock_threads.py`: The Python script running the actual API call. This script requires the `gh` executable.
* `lock_threads.yaml`: The configuration for the lock threads job.

Repos to run this job against can be added to the `lock_threads.yaml` file, and the inactive days count set and an
optional comment to post on the issue or pull request. An optional `look_forward_days` can also be set to surface
threads that aren't old enough to lock yet but will become inactive within that many days. These are listed in the
report with the status "Queued" instead of being locked. The job is performed by the
[Saga Soft Bot](https://github.com/saga-soft-bot) account.


## Reusable Workflows

### Commit Policy

A reusable workflow that fails a pull request if any of its commits has an AI agent or bot author, committer, or
attribution trailer. The `dependabot` and `github-actions` bots are allowed. An optional `policy-url` input adds a
link to the error message. Call it as a separate job:

```yaml
jobs:
  commitPolicy:
    name: Commit Policy
    uses: saga-soft/automation/.github/workflows/commit-policy.yml@main
    permissions:
      contents: read
      pull-requests: read
    with:
      policy-url: https://github.com/saga-soft/<repo>/blob/main/AI_POLICY.md
```
