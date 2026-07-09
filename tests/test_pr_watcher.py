"""The PR watcher decision tree, fully mocked — no network, no LLM, no Docker.

All GitHub/git calls the watcher makes are module-level functions imported into
`merge_watcher`, so we monkeypatch them there and assert on the resulting task
state (SQLite) + which GitHub calls were made.
"""

from __future__ import annotations

import pytest

from open_claw.config import GithubSourceConfig
from open_claw.db import tasks
from open_claw.github import merge_watcher as mw
from open_claw.github.gh import ChangedFile, IssueComment
from tests.conftest import make_async


class DummyQueue:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


def make_cfg(auto_merge=True, require_approval=True):
    return GithubSourceConfig(
        type="github", id="t", repo="o/r",
        autoMerge=auto_merge, requireApproval=require_approval,
    )


def make_pr_task(**meta_overrides):
    meta = {
        "sourceId": "t", "repo": "o/r", "issue": 1, "kind": "bug",
        "prNumber": 7, "branch": "issue-1", "prState": "open", "ciRerunCount": 0,
    }
    meta.update(meta_overrides)
    row = tasks.create("github", "title", "/cwd", meta)
    tasks.set_status(row.id, "awaiting_review")
    return row.id


def patch(mp, **fns):
    """Monkeypatch a batch of gh functions in the merge_watcher namespace."""
    for name, fn in fns.items():
        mp.setattr(mw, name, fn)


async def test_ci_red_triggers_one_rerun(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(), DummyQueue())
    rerun = make_async(True)
    comment_pr = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("failure"),
        pr_failed_check_conclusions=make_async(["CANCELLED"]),
        rerun_failed_runs=rerun,
        comment_pr=comment_pr,
    )

    await watcher._tick()

    meta = tasks.get(tid).meta_dict()
    assert meta["ciRerunCount"] == 1
    assert meta.get("prState") == "open"          # still active, not handed off
    assert tasks.get(tid).status == "awaiting_review"
    assert len(rerun.calls) == 1                    # exactly one rerun
    assert len(comment_pr.calls) == 1


async def test_ci_red_after_rerun_hands_off_to_human(monkeypatch):
    tid = make_pr_task(ciRerunCount=1)              # already re-ran once
    watcher = mw.PrWatcher(make_cfg(), DummyQueue())
    rerun = make_async(True)
    comment_issue = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("failure"),
        pr_failed_check_conclusions=make_async(["FAILURE"]),
        rerun_failed_runs=rerun,
        comment_issue=comment_issue,
    )

    await watcher._tick()

    meta = tasks.get(tid).meta_dict()
    assert meta["prState"] == "manual"             # handed off
    assert len(rerun.calls) == 0                    # no second rerun
    assert len(comment_issue.calls) == 1
    # Handed-off PRs drop out of the watcher's active set.
    assert tasks.awaiting_review_prs() == []


async def test_new_human_comment_queues_rework(monkeypatch):
    tid = make_pr_task(prReviewedThrough="2020-01-01T00:00:00Z")
    q = DummyQueue()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), q)
    comment_pr = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([
            IssueComment(author="alice", body="Please rename the button", created_at="2024-06-01T10:00:00Z"),
        ]),
        comment_pr=comment_pr,
    )

    await watcher._tick()

    row = tasks.get(tid)
    meta = row.meta_dict()
    assert row.status == "queued"                   # handed to a worker
    assert meta["mode"] == "pr_reply"
    assert "Please rename the button" in meta["feedback"]
    assert meta["prReviewedThrough"] == "2024-06-01T10:00:00Z"
    assert q.pokes == 1
    assert len(comment_pr.calls) == 0              # no ack — one summary comment per round


async def test_all_new_comments_handled_with_anchors(monkeypatch):
    # Several new comments at once: none is dropped, and inline anchors survive.
    tid = make_pr_task(prReviewedThrough="2020-01-01T00:00:00Z")
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([
            IssueComment(author="a", body="revert these lines", created_at="2024-06-01T10:00:00Z",
                         path="e2e/version.spec.ts", line=8, diff_hunk="@@ -1 +8 @@"),
            IssueComment(author="a", body="rename just the test name", created_at="2024-06-01T11:00:00Z",
                         path="e2e/version.spec.ts", line=3),
        ]),
        comment_pr=make_async(None),
    )

    await watcher._tick()

    fb = tasks.get(tid).meta_dict()["feedback"]
    assert "revert these lines" in fb and "rename just the test name" in fb   # both kept
    assert "e2e/version.spec.ts" in fb and "line 3" in fb and "line 8" in fb  # anchored
    # cursor advanced to the newest of the batch
    assert tasks.get(tid).meta_dict()["prReviewedThrough"] == "2024-06-01T11:00:00Z"


async def test_bot_comment_is_ignored(monkeypatch):
    tid = make_pr_task(prReviewedThrough="")
    q = DummyQueue()
    watcher = mw.PrWatcher(make_cfg(auto_merge=False), q)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([
            IssueComment(author="bot", body="🤖 **open-claw:** opened a PR", created_at="2024-06-01T10:00:00Z"),
        ]),
    )

    await watcher._tick()

    # Our own comment must not look like human review → no rework, still waiting.
    row = tasks.get(tid)
    assert row.status == "awaiting_review"
    assert row.meta_dict().get("mode", "") != "pr_reply"
    assert q.pokes == 0


async def test_label_approves_and_merges(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_labels=make_async(["auto-merge"]),           # owner labelled → approved
        pr_changed_files=make_async([ChangedFile(file="src/app.ts", added=3, deleted=1)]),
        merge_pr=merge,
        comment_issue=make_async(None),
    )

    await watcher._tick()

    row = tasks.get(tid)
    assert row.status == "done"
    assert row.meta_dict()["prState"] == "merged"
    assert len(merge.calls) == 1


async def test_review_approval_also_merges(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_labels=make_async([]),                       # no label…
        pr_review_decision=make_async("approved"),      # …but a reviewer approved
        pr_changed_files=make_async([ChangedFile(file="src/app.ts", added=3, deleted=1)]),
        merge_pr=merge,
        comment_issue=make_async(None),
    )

    await watcher._tick()
    assert tasks.get(tid).status == "done"
    assert len(merge.calls) == 1


async def test_green_but_not_approved_waits(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_labels=make_async([]),                       # no label
        pr_review_decision=make_async("none"),          # nobody approved yet
        merge_pr=merge,
    )

    await watcher._tick()

    # Green CI is not enough without approval → keep waiting, no handoff.
    assert len(merge.calls) == 0
    assert tasks.get(tid).status == "awaiting_review"
    assert tasks.get(tid).meta_dict().get("prState") == "open"


async def test_approval_not_required_merges_on_ci(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True, require_approval=False), DummyQueue())
    merge = make_async(None)
    # pr_review_decision must NOT be consulted when approval isn't required.
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_changed_files=make_async([ChangedFile(file="src/app.ts", added=1, deleted=0)]),
        merge_pr=merge,
        comment_issue=make_async(None),
    )

    await watcher._tick()
    assert tasks.get(tid).status == "done"
    assert len(merge.calls) == 1


async def test_changes_requested_blocks_merge(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_labels=make_async([]),
        pr_review_decision=make_async("changes_requested"),
        merge_pr=merge,
    )

    await watcher._tick()
    assert len(merge.calls) == 0
    assert tasks.get(tid).status == "awaiting_review"


async def test_green_but_policy_blocks_hands_off(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=True), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        pr_labels=make_async(["auto-merge"]),            # approved…
        # …but outside allowedGlobs (default src/public/docs) → policy fails.
        pr_changed_files=make_async([ChangedFile(file="ci/deploy.yml", added=5, deleted=0)]),
        merge_pr=merge,
        comment_issue=make_async(None),
    )

    await watcher._tick()

    assert len(merge.calls) == 0
    assert tasks.get(tid).meta_dict()["prState"] == "manual"


async def test_green_no_automerge_waits(monkeypatch):
    tid = make_pr_task()
    watcher = mw.PrWatcher(make_cfg(auto_merge=False), DummyQueue())
    merge = make_async(None)
    patch(
        monkeypatch,
        pr_checks_state=make_async("success"),
        pr_comments=make_async([]),
        merge_pr=merge,
    )

    await watcher._tick()

    # auto-merge off: no merge, task keeps waiting for a human.
    assert len(merge.calls) == 0
    assert tasks.get(tid).status == "awaiting_review"
