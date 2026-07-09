"""A GitHub source: polls one repo, routes labelled issues to answer/autofix, and
(optionally) auto-merges green PRs that pass the blast-radius policy.

Ties the pieces together: the poller enqueues, the queue calls run(task), and
run() drives the appropriate LangGraph graph inside the task's execution
environment (host or Docker container). Prepare (clone/branch) and publish
(git/gh) run on the host; the container only wraps the agent's shell work.
"""

from __future__ import annotations

import os
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from langgraph.errors import GraphRecursionError

from ..config import GithubSourceConfig, settings
from ..container import task_container
from ..db import TaskRow, tasks
from ..logging import create_logger, task_log_scope
from ..queue import Queue
from .answer_graph import build_answer_graph, render_thread
from .autofix_graph import build_autofix_graph
from .gh import (
    BOT_COMMENT_PREFIX,
    comment_issue,
    default_branch,
    ensure_clone,
    is_authenticated,
    is_bot_comment,
    list_issue_comments,
    prepare_branch,
)
from .merge_watcher import MergeWatcher
from .poller import GithubPoller

log = create_logger("github")

_GRAPH_RECURSION = 120  # super-step budget for the outer pipeline (bounded loops)
_GUIDANCE_MAX_CHARS = 8000  # cap injected repo instructions to keep context sane


def read_repo_guidance(repo_path: str) -> str:
    """Read AGENTS.md / CLAUDE.md from the checkout root so the agent always sees
    the repo's own project instructions before starting. Returns a formatted
    block (or '' if none). AGENTS.md is the primary convention; CLAUDE.md is
    included too when present."""
    blocks: list[str] = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            text = Path(repo_path, name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            blocks.append(
                f"--- {name} (repository instructions — read and follow these) ---\n"
                f"{text[:_GUIDANCE_MAX_CHARS]}"
            )
    return "\n\n".join(blocks)


class GithubSource:
    type = "github"

    def __init__(self, cfg: GithubSourceConfig, queue: Queue, llm: BaseChatModel) -> None:
        cfg.clone_dir = os.path.abspath(cfg.clone_dir)
        self.cfg = cfg
        self.id = cfg.id
        self.llm = llm
        self.poller = GithubPoller(cfg, queue)
        self.watcher = MergeWatcher(cfg)

    async def start(self) -> None:
        if not await is_authenticated():
            log.warn(f"[{self.id}] gh is not authenticated — run `gh auth login`. Source disabled.")
            return
        self.poller.start()
        self.watcher.start()

    def stop(self) -> None:
        self.poller.stop()
        self.watcher.stop()

    async def run(self, task: TaskRow) -> str | None:
        meta = task.meta_dict()
        with task_log_scope("github", f"issue{meta.get('issue')}") as logfile:
            log.info(f"task #{task.id} → {meta.get('kind')} #{meta.get('issue')} (log: {logfile})")
            try:
                if meta.get("kind") == "question":
                    return await self._answer(task, meta)
                return await self._autofix(task, meta)
            except Exception as err:  # noqa: BLE001 — re-raised after signalling
                await self._signal_failure(meta, err)
                raise

    async def _signal_failure(self, meta: dict, err: Exception) -> None:
        """A failed bug/feature autofix is terminal — the poller will not retry it
        (`is_github_issue_taken` counts 'failed'). Leave a note on the issue so a
        human notices and takes over instead of the failure passing silently."""
        repo, issue, kind = meta.get("repo"), meta.get("issue"), meta.get("kind")
        if not repo or not issue or kind == "question":
            return
        if isinstance(err, GraphRecursionError):
            reason = (
                f"the agent hit its step budget ({settings.agent_max_turns} turns) "
                "without finishing — it may be stuck in a loop or the task may be "
                "too large to do in one pass."
            )
        else:
            reason = f"`{type(err).__name__}: {err}`"
        body = (
            f"{BOT_COMMENT_PREFIX} automated fix attempt **failed** and will not be "
            f"retried automatically — {reason}\n\nA human needs to take a look."
        )
        await comment_issue(repo, issue, body)
        log.warn(f"#{issue}: signalled failure on the issue thread")

    async def _autofix(self, task: TaskRow, meta: dict) -> str | None:
        repo, issue, kind = meta["repo"], meta["issue"], meta["kind"]
        log.info(f"autofix {kind} #{issue} in {repo}")
        repo_path = await ensure_clone(repo, self.cfg.clone_dir)
        base = await default_branch(repo)
        branch = f"issue-{issue}"
        await prepare_branch(repo_path, branch)

        guidance = read_repo_guidance(repo_path)
        if guidance:
            log.info(f"#{issue}: loaded repo instructions ({len(guidance)} chars)")

        async with task_container(repo_path, self.cfg.container) as env:
            graph = build_autofix_graph(self.llm, env, self.cfg, guidance)
            out = await graph.ainvoke(
                {
                    "repo": repo, "issue": issue, "kind": kind,
                    "title": task.prompt, "body": meta.get("body", ""), "url": meta.get("url", ""),
                    "base": base, "branch": branch, "repo_path": repo_path,
                },
                config={"recursion_limit": _GRAPH_RECURSION},
            )

        if out.get("pr_number"):
            tasks.set_meta(
                task.id,
                {**meta, "branch": branch, "prNumber": out["pr_number"], "prUrl": out.get("pr_url")},
            )
        return out.get("result")

    async def _answer(self, task: TaskRow, meta: dict) -> str | None:
        repo, issue = meta["repo"], meta["issue"]
        repo_path = await ensure_clone(repo, self.cfg.clone_dir)
        comments = await list_issue_comments(repo, issue)
        thread = render_thread(comments)
        guidance = read_repo_guidance(repo_path)

        async with task_container(repo_path, self.cfg.container) as env:
            graph = build_answer_graph(self.llm, env, self.cfg, guidance)
            out = await graph.ainvoke(
                {
                    "repo": repo, "issue": issue, "title": task.prompt,
                    "body": meta.get("body", ""), "url": meta.get("url", ""), "thread": thread,
                },
                config={"recursion_limit": _GRAPH_RECURSION},
            )

        # Advance the conversation cursor to the newest human comment we answered.
        human_times = [c.created_at for c in comments if not is_bot_comment(c.body)]
        latest_human = max(human_times, default="")
        tasks.set_meta(task.id, {**meta, "answeredThrough": latest_human})
        return out.get("result")
