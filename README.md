# Automation

Internal repository for the Saga Soft Organisation.

Used for scripts and automated jobs.

## Lock Threads

A workflow and script to lock closed issues and pull requests that has been inactive for a given amount of time.
The tool consists of the following files:

* `.github/workflows/lock-thread.yml`: The workflow job running as a cron job.
* `lock_threads.py`: The Python script running the actual API call. This script requires the `gh` executable.
* `lock_threads.yaml`: The configuration for the lock threads job.

Repos to run this job against can be added to the `lock_threads.yaml` file, and the inactive days count set and an
optional comment to post on the issue or pull request. The job is performed by the
[Saga Soft Bot](https://github.com/saga-soft-bot) account.
