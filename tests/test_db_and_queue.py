"""Task lifecycle: the new statuses count toward dedup, the awaiting-review
query only returns actively-managed PRs, and the queue does not clobber a status
the runner deliberately set."""

from __future__ import annotations

from open_claw.db import tasks
from open_claw.queue import Queue


def _mk(issue, status, **meta):
    m = {"sourceId": "t", "repo": "o/r", "issue": issue, "kind": "bug", **meta}
    row = tasks.create("github", "t", "/cwd", m)
    tasks.set_status(row.id, status)
    return row.id


def test_awaiting_review_counts_as_taken():
    _mk(1, "awaiting_review", prNumber=5)
    assert tasks.is_github_issue_taken("o/r", 1) is True


def test_rejected_counts_as_taken_not_in_flight():
    _mk(2, "rejected")
    assert tasks.is_github_issue_taken("o/r", 2) is True
    assert tasks.has_github_issue_in_flight("o/r", 2) is False


def test_awaiting_review_is_in_flight():
    _mk(3, "awaiting_review", prNumber=6)
    assert tasks.has_github_issue_in_flight("o/r", 3) is True


def test_awaiting_review_prs_excludes_manual_and_non_pr():
    keep = _mk(10, "awaiting_review", prNumber=7, prState="open")
    _mk(11, "awaiting_review", prNumber=8, prState="manual")   # handed off
    _mk(12, "awaiting_review")                                  # no PR yet
    _mk(13, "done", prNumber=9)                                 # already closed
    ids = {r.id for r in tasks.awaiting_review_prs()}
    assert ids == {keep}


async def test_queue_does_not_clobber_awaiting_review():
    row = tasks.create("github", "t", "/cwd", {"repo": "o/r", "issue": 20})
    tasks.set_status(row.id, "running")

    async def runner(task):
        # Runner parks the task itself, mimicking source._autofix opening a PR.
        tasks.set_status(task.id, "awaiting_review")
        return "PR opened"

    await Queue(runner)._execute(row)
    assert tasks.get(row.id).status == "awaiting_review"   # not overwritten to 'done'


async def test_queue_closes_normal_task():
    row = tasks.create("github", "t", "/cwd", {"repo": "o/r", "issue": 21})
    tasks.set_status(row.id, "running")

    async def runner(task):
        return "answered"

    await Queue(runner)._execute(row)
    assert tasks.get(row.id).status == "done"
