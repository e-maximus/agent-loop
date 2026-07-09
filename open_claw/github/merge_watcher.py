"""Polls open autofix PRs for one repo and merges them once CI is green AND the
diff passes the blast-radius policy. The merge decision is made from GitHub's own
Checks state — never from what the agent claimed. Ported from merge-watcher.ts.
"""

from __future__ import annotations

import asyncio

from ..config import GithubSourceConfig
from ..db import tasks
from ..logging import create_logger
from .gh import comment_issue, merge_pr, pr_changed_files, pr_checks_state
from .policy import evaluate_diff


class MergeWatcher:
    def __init__(self, cfg: GithubSourceConfig) -> None:
        self.cfg = cfg
        self.log = create_logger(f"merge-watcher:{cfg.id}")
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self.cfg.auto_merge:
            self.log.info("auto-merge disabled — watcher not started")
            return
        self.log.info(f"auto-merge watcher every {self.cfg.poll_interval_sec}s")
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

    def _set_state(self, task_id: int, meta: dict, state: str) -> None:
        tasks.set_meta(task_id, {**meta, "mergeState": state})

    async def _tick(self) -> None:
        for row in tasks.awaiting_merge():
            meta = row.meta_dict()
            repo, issue, pr = meta.get("repo"), meta.get("issue"), meta.get("prNumber")
            if pr is None or repo != self.cfg.repo:  # another repo's watcher owns it
                continue

            state = await pr_checks_state(repo, pr)
            if state == "pending":
                continue

            if state == "failure":
                self.log.warn(f"PR #{pr}: CI red — leaving for review")
                self._set_state(row.id, meta, "ci_failed")
                await comment_issue(repo, issue, f"🤖 CI is red on PR #{pr} — leaving it for manual review.")
                continue

            if state == "none":
                self.log.warn(f"PR #{pr}: no CI checks found — leaving for review")
                self._set_state(row.id, meta, "manual")
                await comment_issue(
                    repo, issue,
                    f"🤖 PR #{pr} has no CI checks — cannot auto-merge, leaving it for review.",
                )
                continue

            # Green. Now the blast-radius policy on the actual diff.
            files = await pr_changed_files(repo, pr)
            verdict = evaluate_diff(files, self.cfg.policy)
            if not verdict.ok:
                self.log.warn(f"PR #{pr}: policy blocked — {'; '.join(verdict.reasons)}")
                self._set_state(row.id, meta, "manual")
                reasons = "\n- ".join(verdict.reasons)
                await comment_issue(
                    repo, issue,
                    f"🤖 CI is green, but the auto-merge policy did not pass:\n- {reasons}\n\n"
                    f"Leaving PR #{pr} for manual review.",
                )
                continue

            self.log.info(f"PR #{pr}: green + policy ok → merging")
            await merge_pr(repo, pr)
            self._set_state(row.id, meta, "merged")
            await comment_issue(repo, issue, f"✅ open-claw merged PR #{pr} (CI green, policy passed).")
