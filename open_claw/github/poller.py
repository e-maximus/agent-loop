"""Polls one configured repo for open issues and enqueues a task for each new
one that carries a routing label. Bug/feature issues are one-shot; question
issues form a continuing thread (dedup by an `answeredThrough` cursor).

Ported from poller.ts; setInterval → an asyncio loop.
"""

from __future__ import annotations

import asyncio

from ..config import GithubSourceConfig
from ..db import tasks
from ..logging import create_logger
from ..queue import Queue
from .gh import Issue, is_bot_comment, list_issue_comments, list_open_issues


class GithubPoller:
    def __init__(self, cfg: GithubSourceConfig, queue: Queue) -> None:
        self.cfg = cfg
        self.queue = queue
        self.log = create_logger(f"gh-poller:{cfg.id}")
        self._task: asyncio.Task[None] | None = None

    def _classify(self, issue: Issue) -> str | None:
        """Priority: bug > feature > question."""
        labels = set(issue.labels)
        lb = self.cfg.labels
        if lb.bug in labels:
            return "bug"
        if lb.feature in labels:
            return "feature"
        if lb.question in labels:
            return "question"
        return None

    def start(self) -> None:
        lb = self.cfg.labels
        self.log.info(
            f"polling {self.cfg.repo} every {self.cfg.poll_interval_sec}s "
            f"(labels: {lb.bug}→fix, {lb.feature}→feature, {lb.question}→answer)"
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
                self.log.error("poll failed", str(e))
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _tick(self) -> None:
        repo = self.cfg.repo
        issues = await list_open_issues(repo)
        enqueued = 0
        for issue in issues:
            kind = self._classify(issue)
            if not kind:
                continue
            did = (
                await self._consider_question(repo, issue)
                if kind == "question"
                else self._consider_fixable(repo, issue, kind)
            )
            if did:
                enqueued += 1
        if enqueued:
            self.queue.poke()

    def _consider_fixable(self, repo: str, issue: Issue, kind: str) -> bool:
        """Bug/feature issues are one-shot: enqueue once, never again."""
        if tasks.is_github_issue_taken(repo, issue.number):
            return False
        self._enqueue(repo, issue, kind)
        return True

    async def _consider_question(self, repo: str, issue: Issue) -> bool:
        """Answer the initial question, then re-answer when a human comments
        after our last reply (cursor `answeredThrough`)."""
        if tasks.has_github_issue_in_flight(repo, issue.number):
            return False

        prev = tasks.latest_github_issue_task(repo, issue.number)
        if prev is None:
            self._enqueue(repo, issue, "question")
            return True

        answered_through = prev.meta_dict().get("answeredThrough", "")
        comments = await list_issue_comments(repo, issue.number)
        human_times = [c.created_at for c in comments if not is_bot_comment(c.body)]
        latest_human = max(human_times, default="")
        if latest_human and latest_human > answered_through:
            self._enqueue(repo, issue, "question")
            return True
        return False

    def _enqueue(self, repo: str, issue: Issue, kind: str) -> None:
        tasks.create(
            "github",
            issue.title,
            self.cfg.clone_dir,
            {
                "sourceId": self.cfg.id,
                "repo": repo,
                "issue": issue.number,
                "kind": kind,
                "url": issue.url,
                "body": issue.body,
                "author": issue.author,
                # authorAssociation is not in the list API — the triage gate
                # fetches it per-issue when it actually needs it.
            },
        )
        self.log.info(f"enqueued {kind} for #{issue.number}: {issue.title}")
