"""Wrappers over the `gh` and `git` CLIs. Ported from gh.ts.

argv arrays only (no shell), so issue/prompt text can never be reinterpreted as
shell when it flows into command arguments.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..exec import run
from ..logging import create_logger

log = create_logger("github")

# Prefix open-claw prepends to every comment it posts. Used to tell our own
# replies apart from human comments (bot and repo owner may share a login).
BOT_COMMENT_PREFIX = "🤖 **open-claw:**"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)


@dataclass
class ChangedFile:
    file: str
    added: int
    deleted: int


@dataclass
class IssueComment:
    author: str
    body: str
    created_at: str  # ISO 8601


@dataclass
class PullRequest:
    number: int
    url: str


def is_bot_comment(body: str) -> bool:
    return body.lstrip().startswith("🤖")


def _repo_dir_name(repo: str) -> str:
    return repo.replace("/", "__")


async def is_authenticated() -> bool:
    res = await run("gh", ["auth", "status"], throw_on_error=False)
    return res.code == 0


async def default_branch(repo: str) -> str:
    res = await run(
        "gh",
        ["repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
    )
    return res.stdout.strip() or "main"


async def list_open_issues(repo: str) -> list[Issue]:
    res = await run(
        "gh",
        ["issue", "list", "--repo", repo, "--state", "open",
         "--json", "number,title,body,url,labels", "--limit", "100"],
    )
    parsed = json.loads(res.stdout or "[]")
    return [
        Issue(
            number=i["number"],
            title=i["title"],
            body=i.get("body") or "",
            url=i["url"],
            labels=[l["name"] for l in (i.get("labels") or [])],
        )
        for i in parsed
    ]


async def list_issue_comments(repo: str, issue: int) -> list[IssueComment]:
    res = await run(
        "gh", ["api", f"repos/{repo}/issues/{issue}/comments", "--paginate"],
        throw_on_error=False,
    )
    if res.code != 0:
        return []
    parsed = json.loads(res.stdout or "[]")
    return [
        IssueComment(
            author=(c.get("user") or {}).get("login", "unknown"),
            body=c.get("body") or "",
            created_at=c.get("created_at") or "",
        )
        for c in parsed
    ]


async def ensure_clone(repo: str, clone_dir: str) -> str:
    """Clone if missing, else fetch and hard-reset to latest default branch.
    Returns the local checkout path. Idempotent."""
    Path(clone_dir).mkdir(parents=True, exist_ok=True)
    dest = os.path.join(clone_dir, _repo_dir_name(repo))
    base = await default_branch(repo)

    if not os.path.exists(os.path.join(dest, ".git")):
        log.info(f"cloning {repo} → {dest}")
        await run("gh", ["repo", "clone", repo, dest])
    await run("gh", ["auth", "setup-git"], throw_on_error=False)
    await run("git", ["fetch", "origin", base], cwd=dest)
    await run("git", ["checkout", base], cwd=dest)
    await run("git", ["reset", "--hard", f"origin/{base}"], cwd=dest)
    await run("git", ["clean", "-fd"], cwd=dest)
    return dest


async def prepare_branch(repo_path: str, branch: str) -> None:
    await run("git", ["checkout", "-B", branch], cwd=repo_path)


async def has_changes(repo_path: str) -> bool:
    res = await run("git", ["status", "--porcelain"], cwd=repo_path)
    return len(res.stdout.strip()) > 0


async def commit_all(repo_path: str, message: str) -> None:
    await run("git", ["add", "-A"], cwd=repo_path)
    await run("git", ["commit", "-m", message], cwd=repo_path)


async def push(repo_path: str, branch: str) -> None:
    await run("git", ["push", "-u", "origin", branch, "--force-with-lease"], cwd=repo_path)


async def open_pr(repo_path: str, *, base: str, title: str, body: str) -> PullRequest:
    res = await run(
        "gh", ["pr", "create", "--base", base, "--title", title, "--body", body],
        cwd=repo_path,
    )
    url = res.stdout.strip().split("\n")[-1] if res.stdout.strip() else ""
    number = int(url.split("/")[-1]) if url.split("/")[-1].isdigit() else 0
    return PullRequest(number=number, url=url)


# ── merge-watcher side ─────────────────────────────────────────────────────
ChecksState = str  # 'pending' | 'success' | 'failure' | 'none'


async def pr_checks_state(repo: str, pr_number: int) -> ChecksState:
    """Roll up the PR's CI checks into a single state."""
    res = await run(
        "gh", ["pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"],
        throw_on_error=False,
    )
    if res.code != 0:
        return "pending"
    data = json.loads(res.stdout or "{}")
    checks = data.get("statusCheckRollup") or []
    if not checks:
        return "none"

    any_pending = False
    for c in checks:
        status = c.get("status")  # QUEUED | IN_PROGRESS | COMPLETED (CheckRun)
        conclusion = (c.get("conclusion") or c.get("state") or "").upper()
        if status and status != "COMPLETED":
            any_pending = True
        elif conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            return "failure"
        elif conclusion in ("PENDING", "EXPECTED"):
            any_pending = True
    return "pending" if any_pending else "success"


async def pr_changed_files(repo: str, pr_number: int) -> list[ChangedFile]:
    res = await run("gh", ["pr", "view", str(pr_number), "--repo", repo, "--json", "files"])
    data = json.loads(res.stdout or "{}")
    return [
        ChangedFile(file=f["path"], added=f.get("additions", 0), deleted=f.get("deletions", 0))
        for f in (data.get("files") or [])
    ]


async def merge_pr(repo: str, pr_number: int) -> None:
    await run(
        "gh", ["pr", "merge", str(pr_number), "--repo", repo, "--squash", "--delete-branch"]
    )


async def comment_issue(repo: str, issue: int, body: str) -> None:
    await run(
        "gh", ["issue", "comment", str(issue), "--repo", repo, "--body", body],
        throw_on_error=False,
    )
