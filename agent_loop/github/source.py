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
from typing import Any

from langchain_core.language_models import BaseChatModel

from langgraph.errors import GraphRecursionError

from ..config import GithubSourceConfig, settings
from ..container import task_container
from ..db import TaskRow, tasks
from ..logging import create_logger, task_log_scope
from ..queue import Queue
from ..version import trace_config
from .answer_graph import build_answer_graph, render_thread
from .autofix_graph import build_autofix_graph
from . import prompts
from .gh import (
    BOT_COMMENT_PREFIX,
    checkout_existing_branch,
    comment_issue,
    comment_pr,
    default_branch,
    ensure_clone,
    is_authenticated,
    is_bot_comment,
    issue_author_association,
    list_issue_comments,
    pr_comments,
    prepare_branch,
)
from .merge_watcher import PrWatcher
from .poller import GithubPoller
from .verdicts import parse_verdict

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
        self.watcher = PrWatcher(cfg, queue)

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
                if meta.get("mode") == "pr_reply":
                    return await self._pr_reply(task, meta)
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

    def _trace(self, task: TaskRow, meta: dict, stage: str) -> dict[str, Any]:
        """LangSmith config for a call that starts a trace: build identity plus
        the task it belongs to, so a run is findable by issue rather than by
        timestamp. Children of a graph run inherit it."""
        return trace_config(
            stage=stage,
            source_id=self.id,
            repo=meta.get("repo"),
            issue=meta.get("issue"),
            task_id=task.id,
            kind=meta.get("kind"),
        )

    async def _triage(self, task: TaskRow, meta: dict) -> tuple[bool, str]:
        """Intake security gate: decide whether the request is safe to work on at
        all, from the issue text + the author's trust level — before any clone.
        Returns (allowed, verdict_text)."""
        repo, issue = meta["repo"], meta["issue"]
        assoc = meta.get("authorAssociation") or await issue_author_association(repo, issue)
        author = meta.get("author", "unknown")
        user = (
            f"Issue #{issue} in {repo}\nAuthor: {author} (authorAssociation: {assoc})\n\n"
            f"Title: {task.prompt}\n\n{meta.get('body') or '(no body)'}"
        )
        resp = await self.llm.ainvoke(
            [("system", prompts.triage_prompt(assoc)), ("user", user)],
            config=self._trace(task, meta, "triage"),
        )
        text = resp.content.strip() if isinstance(resp.content, str) else str(resp.content)
        # Unparseable → REJECT. This gate decides whether to run untrusted work at
        # all, so a verdict we cannot read must not count as permission.
        allowed = parse_verdict(text, "VERDICT", ("ALLOW", "REJECT"), default="REJECT") == "ALLOW"
        return allowed, text

    async def _autofix(self, task: TaskRow, meta: dict) -> str | None:
        repo, issue, kind = meta["repo"], meta["issue"], meta["kind"]
        mode = meta.get("mode", "")
        log.info(f"autofix {kind} #{issue} in {repo}{' (rework)' if mode == 'rework' else ''}")

        # ── intake security gate (first pass only; rework already passed it) ──
        if mode != "rework":
            allowed, verdict = await self._triage(task, meta)
            if not allowed:
                reason = verdict.split("\n", 1)[-1].strip() or verdict
                await comment_issue(
                    repo, issue,
                    f"{BOT_COMMENT_PREFIX}\n\nThis request was declined by the security intake policy: {reason}",
                )
                tasks.set_status(task.id, "rejected")
                log.warn(f"#{issue}: intake gate REJECTED — {reason[:200]}")
                return f"Rejected by intake gate: {reason[:200]}"

        repo_path = await ensure_clone(repo, self.cfg.clone_dir)
        base = await default_branch(repo)
        branch = meta.get("branch") or f"issue-{issue}"
        if mode == "rework":
            await checkout_existing_branch(repo_path, branch)
        else:
            await prepare_branch(repo_path, branch)

        guidance = read_repo_guidance(repo_path)
        if guidance:
            log.info(f"#{issue}: loaded repo instructions ({len(guidance)} chars)")

        state: dict = {
            "repo": repo, "issue": issue, "kind": kind,
            "title": task.prompt, "body": meta.get("body", ""), "url": meta.get("url", ""),
            "base": base, "branch": branch, "repo_path": repo_path,
        }
        if mode == "rework":
            feedback = meta.get("feedback", "")
            state.update(
                mode="rework",
                pr_number=meta.get("prNumber", 0),
                pr_url=meta.get("prUrl", ""),
                plan=f"Address this reviewer feedback on the existing PR:\n{feedback}",
                critic_feedback=feedback,
            )

        async with task_container(repo_path, self.cfg.container) as env:
            graph = build_autofix_graph(self.llm, env, self.cfg, guidance)
            out = await graph.ainvoke(
                state,
                config={
                    "recursion_limit": _GRAPH_RECURSION,
                    **self._trace(task, meta, "rework" if mode == "rework" else "autofix"),
                },
            )

        if out.get("pr_number"):
            # Park the task in awaiting_review with a fresh CI-rerun budget; the
            # PR watcher takes it from here and only merge closes it.
            tasks.set_meta(
                task.id,
                {
                    **meta, "branch": branch,
                    "prNumber": out["pr_number"], "prUrl": out.get("pr_url"),
                    "prState": "open", "ciRerunCount": 0, "mode": "", "feedback": "",
                },
            )
            tasks.set_status(task.id, "awaiting_review")
        return out.get("result")

    async def _pr_reply(self, task: TaskRow, meta: dict) -> str | None:
        """Handle a human comment on an open PR: classify it, then either answer
        (question) or rework the PR (change request). Set by the PR watcher via
        `mode = pr_reply` + `feedback = <comment>`."""
        repo, issue, pr = meta["repo"], meta["issue"], meta.get("prNumber")
        comment = meta.get("feedback", "")
        user = (
            f"PR #{pr} for issue #{issue} in {repo}\n\n"
            f"Issue title: {task.prompt}\n\n{meta.get('body') or ''}\n\n"
            f"--- the human's comment ---\n{comment}"
        )
        resp = await self.llm.ainvoke(
            [("system", prompts.classify_comment_prompt()), ("user", user)],
            config=self._trace(task, meta, "classify_comment"),
        )
        text = resp.content.strip() if isinstance(resp.content, str) else str(resp.content)
        # Unparseable → NONE: do nothing rather than rework a PR on a guess.
        intent = parse_verdict(
            text, "INTENT", ("QUESTION", "CHANGE_REQUEST", "NONE"), default="NONE"
        )
        log.info(f"#{issue}: PR comment classified as {intent}")

        if intent == "CHANGE_REQUEST":
            # Rework the PR in place; _autofix parks it back in awaiting_review.
            return await self._autofix(task, {**meta, "mode": "rework"})

        if intent == "QUESTION":
            answer = await self._answer_pr(task, meta)
            await comment_pr(repo, pr, f"{BOT_COMMENT_PREFIX}\n\n{answer[:60000]}")
            result = f"Answered comment on PR #{pr}."
        else:
            result = f"No action needed for comment on PR #{pr}."

        # Back to waiting; the comment cursor was already advanced by the watcher.
        tasks.set_meta(task.id, {**meta, "mode": "", "feedback": "", "prState": "open"})
        tasks.set_status(task.id, "awaiting_review")
        return result

    async def _answer_pr(self, task: TaskRow, meta: dict) -> str:
        """Answer a question comment grounded in the PR branch code."""
        repo, issue = meta["repo"], meta["issue"]
        branch = meta.get("branch") or f"issue-{issue}"
        repo_path = await ensure_clone(repo, self.cfg.clone_dir)
        await checkout_existing_branch(repo_path, branch)
        comments = await pr_comments(repo, meta.get("prNumber"))
        thread = render_thread(comments)
        guidance = read_repo_guidance(repo_path)
        async with task_container(repo_path, self.cfg.container) as env:
            graph = build_answer_graph(self.llm, env, self.cfg, guidance, auto_post=False)
            out = await graph.ainvoke(
                {
                    "repo": repo, "issue": issue, "title": task.prompt,
                    "body": meta.get("body", ""), "url": meta.get("prUrl", ""), "thread": thread,
                },
                config={
                    "recursion_limit": _GRAPH_RECURSION,
                    **self._trace(task, meta, "answer_pr"),
                },
            )
        return out.get("answer") or "(no answer produced)"

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
                config={
                    "recursion_limit": _GRAPH_RECURSION,
                    **self._trace(task, meta, "answer_issue"),
                },
            )

        # Advance the conversation cursor to the newest human comment we answered.
        human_times = [c.created_at for c in comments if not is_bot_comment(c.body)]
        latest_human = max(human_times, default="")
        tasks.set_meta(task.id, {**meta, "answeredThrough": latest_human})
        return out.get("result")
