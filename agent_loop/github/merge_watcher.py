"""Watches the open autofix PRs for one repo and drives them to a terminal
state. A task stays `awaiting_review` — NOT done — until its PR is merged.

Per tick, for each managed PR:
  • CI red      → re-run the failed jobs ONCE (covers `cancelled`/flaky). If it
                  is still red after that single re-run, hand off to a human.
  • New human   → hand the comment to a worker task (`mode = pr_reply`), which
    comment      classifies it as a question (answer it) or a change request
                  (rework the PR). CI need not be green for this.
  • CI green    → if auto-merge is on and the blast-radius policy passes, merge
                  (→ task done). Otherwise leave it for the human.

The merge decision is made from GitHub's own Checks state, never from what the
agent claimed. The watcher itself does no heavy LLM work — it only detects and
routes; the queue worker does the reasoning.
"""

from __future__ import annotations

import asyncio

from ..config import GithubSourceConfig
from ..db import TaskMeta, TaskRow, tasks
from ..logging import create_logger
from ..queue import Queue
from .gh import (
    BOT_COMMENT_PREFIX,
    IssueComment,
    comment_issue,
    comment_pr,
    failures_are_transient,
    is_bot_comment,
    merge_pr,
    pr_changed_files,
    pr_checks_state,
    pr_comments,
    pr_failed_check_conclusions,
    pr_labels,
    pr_review_decision,
    rerun_failed_runs,
)
from .policy import evaluate_diff


def _format_feedback(comments: list[IssueComment]) -> str:
    """Render new review comments into rework feedback, preserving each inline
    comment's file/line anchor and hunk so the agent edits the exact spot."""
    blocks: list[str] = []
    for c in comments:
        if c.path:
            loc = f"`{c.path}`" + (f" (line {c.line})" if c.line else "")
            block = f'- Review comment on {loc}:\n  "{c.body.strip()}"'
            if c.diff_hunk:
                block += f"\n  It is anchored to this diff hunk:\n  ```\n{c.diff_hunk}\n  ```"
        else:
            block = f'- Comment: "{c.body.strip()}"'
        blocks.append(block)
    guidance = (
        "Address ONLY what each comment asks, at the exact file/line it is anchored "
        "to. If a comment says to revert or that a change is unnecessary, undo that "
        "specific change. Do not touch unrelated code."
    )
    return "Reviewer left the following comment(s) on the PR:\n\n" + "\n".join(blocks) + f"\n\n{guidance}"


class PrWatcher:
    def __init__(self, cfg: GithubSourceConfig, queue: Queue) -> None:
        self.cfg = cfg
        self.queue = queue
        self.log = create_logger(f"pr-watcher:{cfg.id}")
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        # Runs even when auto-merge is off: it still re-runs red CI and routes
        # human review comments back into rework. Only the final merge is gated.
        self.log.info(
            f"PR watcher every {self.cfg.poll_interval_sec}s "
            f"(auto-merge {'on' if self.cfg.auto_merge else 'off'})"
        )
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log.error("tick failed", str(e))
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _tick(self) -> None:
        for row in tasks.awaiting_review_prs():
            meta = row.meta_dict()
            if meta.get("repo") != self.cfg.repo:  # another repo's watcher owns it
                continue
            pr = meta.get("prNumber")
            if pr is None:
                continue

            state = await pr_checks_state(self.cfg.repo, pr)
            if state == "failure":
                await self._handle_ci_failure(row, meta, pr)
                continue

            # Not failing (green / none / pending): react to a new human comment.
            if await self._handle_new_comment(row, meta, pr):
                continue

            if state == "success" and self.cfg.auto_merge:
                await self._maybe_merge(row, meta, pr)
            elif state == "none" and self.cfg.auto_merge:
                await self._handoff(
                    row, meta, f"🤖 PR #{pr} has no CI checks — cannot auto-merge, leaving it for review."
                )

    # ── CI failure: exactly one automatic re-run, then a human ──────────────
    async def _handle_ci_failure(self, row: TaskRow, meta: TaskMeta, pr: int) -> None:
        repo, branch = self.cfg.repo, meta.get("branch", "")
        if meta.get("ciRerunCount", 0) < 1:
            conclusions = await pr_failed_check_conclusions(repo, pr)
            triggered = await rerun_failed_runs(repo, branch)
            tasks.set_meta(row.id, {**meta, "ciRerunCount": 1})
            kind = "transient (cancelled/timed-out)" if failures_are_transient(conclusions) else "failing"
            tail = (
                "re-running the failed jobs once"
                if triggered
                else "but I could not trigger a re-run automatically"
            )
            self.log.info(f"PR #{pr}: CI {kind} ({','.join(conclusions) or '?'}) — {tail}")
            await comment_pr(
                repo,
                pr,
                f"{BOT_COMMENT_PREFIX}\n\nCI is {kind} ({', '.join(conclusions) or 'unknown'}); {tail}.",
            )
        else:
            self.log.warning(f"PR #{pr}: CI still red after one re-run — handing off")
            await self._handoff(
                row,
                meta,
                f"🤖 CI is still red on PR #{pr} after one automatic re-run — leaving it for manual review.",
            )

    # ── human comment: route to a worker that answers or reworks ────────────
    async def _handle_new_comment(self, row: TaskRow, meta: TaskMeta, pr: int) -> bool:
        repo = self.cfg.repo
        comments = await pr_comments(repo, pr)  # timeline + inline review + reviews
        reviewed_through = meta.get("prReviewedThrough", "")
        new_human = sorted(
            (c for c in comments if not is_bot_comment(c.body) and c.created_at > reviewed_through),
            key=lambda c: c.created_at,
        )
        if not new_human:
            return False

        # Hand ALL new comments to one worker task (not just the latest — that
        # would silently drop the others when the cursor jumps forward), with
        # each comment's file/line anchor preserved so the fix lands in the
        # right place. The worker classifies + acts; source resets the task back
        # to awaiting_review when done.
        feedback = _format_feedback(new_human)
        newest = new_human[-1].created_at
        tasks.set_meta(
            row.id,
            {**meta, "prReviewedThrough": newest, "mode": "pr_reply", "feedback": feedback},
        )
        tasks.set_status(row.id, "queued")
        self.queue.poke()
        self.log.info(f"PR #{pr}: {len(new_human)} new review comment(s) — queued for handling")
        # No ack comment here — the rework/answer posts a single summary comment
        # when it's done, so the agent leaves exactly one comment per round.
        return True

    # ── green + auto-merge: policy gate, then merge ─────────────────────────
    async def _maybe_merge(self, row: TaskRow, meta: TaskMeta, pr: int) -> None:
        repo, issue = self.cfg.repo, meta.get("issue")

        # Approval gate: green CI alone is not enough. Approval is signalled by
        # the configured label (the owner can add it to their own PR — GitHub
        # blocks self-approving reviews) OR by a genuine approving review from a
        # different reviewer.
        if self.cfg.require_approval:
            label = self.cfg.approve_label
            approved = label in await pr_labels(repo, pr)
            if not approved:
                approved = await pr_review_decision(repo, pr) == "approved"
            if not approved:
                self.log.info(
                    f"PR #{pr}: CI green but not approved — add the '{label}' label "
                    "or an approving review to merge"
                )
                return

        files = await pr_changed_files(repo, pr)
        verdict = evaluate_diff(files, self.cfg.policy)
        if not verdict.ok:
            self.log.warning(f"PR #{pr}: policy blocked — {'; '.join(verdict.reasons)}")
            reasons = "\n- ".join(verdict.reasons)
            await self._handoff(
                row,
                meta,
                f"🤖 CI is green, but the auto-merge policy did not pass:\n- {reasons}\n\n"
                f"Leaving PR #{pr} for manual review.",
            )
            return

        self.log.info(f"PR #{pr}: green + policy ok → merging")
        await merge_pr(repo, pr)
        tasks.set_meta(row.id, {**meta, "prState": "merged"})
        tasks.finish_done(row.id, f"Merged PR #{pr} (CI green, policy passed).")
        await comment_issue(repo, issue, f"✅ agent-loop merged PR #{pr} (CI green, policy passed).")

    async def _handoff(self, row: TaskRow, meta: TaskMeta, comment: str) -> None:
        """Mark a PR as needing a human: the task stays awaiting_review (not
        closed — only a merge closes it) but the watcher stops acting on it."""
        tasks.set_meta(row.id, {**meta, "prState": "manual"})
        issue = meta.get("issue")
        if issue is not None:
            await comment_issue(self.cfg.repo, issue, comment)
