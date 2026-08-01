"""Wrappers over the `gh` and `git` CLIs. Ported from gh.ts.

argv arrays only (no shell), so issue/prompt text can never be reinterpreted as
shell when it flows into command arguments.

Every git invocation here runs on the **host**, with credentials, against a
checkout the agent has been writing to. So they all go through `_git()`, which
disables hooks: `git commit` would otherwise execute `.git/hooks/pre-commit`
from that checkout. tools.py refuses to write into `.git/`; this is the second
lock on the same door, because a repo can also arrive with hooks already in it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..exec import run
from ..logging import create_logger

log = create_logger("github")

# Prefix agent-loop prepends to every comment it posts. Used to tell our own
# replies apart from human comments (bot and repo owner may share a login).
BOT_COMMENT_PREFIX = "🤖 **agent-loop:**"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)
    author: str = ""


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
    # Set for inline PR review comments so the agent knows which file/line the
    # feedback is anchored to (empty for timeline/issue comments).
    path: str = ""
    line: int | None = None
    diff_hunk: str = ""


@dataclass
class PullRequest:
    number: int
    url: str
    # True when open_pr adopted a PR that already existed for the branch rather
    # than creating one — the caller updates its body instead of leaving stale text.
    existing: bool = False


async def _git(args: list[str], *, cwd: str, throw_on_error: bool = True):
    """git, with hooks from the target checkout disabled.

    `core.hooksPath=/dev/null` is inherited by every hook lookup for this
    invocation, so a `.git/hooks/pre-commit` left in the tree — by the agent, or
    by whoever pushed to the repo — cannot run as us.
    """
    return await run("git", ["-c", "core.hooksPath=/dev/null", *args], cwd=cwd, throw_on_error=throw_on_error)


def is_bot_comment(body: str) -> bool:
    return body.lstrip().startswith("🤖")


def issue_branch(issue: int) -> str:
    """The branch an issue's work lives on. One naming rule, because the poller
    looks a PR up by it before the task that would create it exists."""
    return f"issue-{issue}"


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
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,body,url,labels,author",
            "--limit",
            "100",
        ],
    )
    parsed = json.loads(res.stdout or "[]")
    return [
        Issue(
            number=i["number"],
            title=i["title"],
            body=i.get("body") or "",
            url=i["url"],
            labels=[lbl["name"] for lbl in (i.get("labels") or [])],
            author=(i.get("author") or {}).get("login", ""),
        )
        for i in parsed
    ]


async def issue_author_association(repo: str, issue: int) -> str:
    """Author's relationship to the repo (OWNER/MEMBER/COLLABORATOR/NONE/…).
    `gh issue list --json` does not expose it, so fetch it per issue only when
    the triage gate actually needs it. Falls back to NONE (the strictest case)."""
    res = await run(
        "gh",
        ["api", f"repos/{repo}/issues/{issue}", "--jq", ".author_association"],
        throw_on_error=False,
    )
    return (res.stdout.strip() or "NONE") if res.code == 0 else "NONE"


async def list_issue_comments(repo: str, issue: int) -> list[IssueComment]:
    res = await run(
        "gh",
        ["api", f"repos/{repo}/issues/{issue}/comments", "--paginate"],
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
    await _git(["fetch", "origin", base], cwd=dest)
    await _git(["checkout", base], cwd=dest)
    await _git(["reset", "--hard", f"origin/{base}"], cwd=dest)
    await _git(["clean", "-fd"], cwd=dest)
    return dest


async def prepare_branch(repo_path: str, branch: str) -> None:
    await _git(["checkout", "-B", branch], cwd=repo_path)


async def checkout_existing_branch(repo_path: str, branch: str) -> None:
    """Check out an existing PR branch from origin for rework, preserving its
    commits (unlike prepare_branch, which resets the branch onto base)."""
    await _git(["fetch", "origin", branch], cwd=repo_path)
    await _git(["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_path)


async def has_changes(repo_path: str) -> bool:
    res = await _git(["status", "--porcelain"], cwd=repo_path)
    return len(res.stdout.strip()) > 0


async def commit_all(repo_path: str, message: str) -> None:
    await _git(["add", "-A"], cwd=repo_path)
    await _git(["commit", "-m", message], cwd=repo_path)


async def push(repo_path: str, branch: str) -> None:
    await _git(["push", "-u", "origin", branch, "--force-with-lease"], cwd=repo_path)


async def find_open_pr(repo: str, branch: str) -> PullRequest | None:
    """The open PR whose head is `branch`, if there is one. GitHub — not the
    task DB — is the source of truth for whether an issue is already handled:
    the DB can be reset or lost, the PR cannot."""
    res = await run(
        "gh",
        ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url"],
        throw_on_error=False,
    )
    if res.code != 0:
        return None
    try:
        items = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for it in items:
        if it.get("url"):
            return PullRequest(number=int(it.get("number") or 0), url=it["url"])
    return None


async def open_pr(
    repo_path: str, *, base: str, title: str, body: str, repo: str = "", head: str = ""
) -> PullRequest:
    res = await run(
        "gh",
        ["pr", "create", "--base", base, "--title", title, "--body", body],
        cwd=repo_path,
        throw_on_error=False,
    )
    if res.code == 0:
        url = res.stdout.strip().split("\n")[-1] if res.stdout.strip() else ""
        number = int(url.split("/")[-1]) if url.split("/")[-1].isdigit() else 0
        return PullRequest(number=number, url=url)

    # A PR for this branch may already exist — the same issue re-run after the
    # task DB was reset, or a crash between push and create. The push above
    # already put this work on the branch, so the existing PR now carries it:
    # adopt it instead of throwing away a completed run.
    if repo and head:
        existing = await find_open_pr(repo, head)
        if existing:
            log.info(f"PR for {head} already exists — reusing {existing.url}")
            return PullRequest(number=existing.number, url=existing.url, existing=True)
    detail = (res.stderr or res.stdout).strip()
    raise RuntimeError(f"`gh pr create` failed (exit {res.code}):\n{detail}")


async def update_pr(repo: str, pr_number: int, *, title: str, body: str) -> None:
    """Refresh an adopted PR's title/body so it describes the run that just
    pushed to it, not the one that opened it."""
    await run(
        "gh",
        ["pr", "edit", str(pr_number), "--repo", repo, "--title", title, "--body", body],
        throw_on_error=False,
    )


# ── merge-watcher side ─────────────────────────────────────────────────────
ChecksState = str  # 'pending' | 'success' | 'failure' | 'none'


async def pr_checks_state(repo: str, pr_number: int) -> ChecksState:
    """Roll up the PR's CI checks into a single state."""
    res = await run(
        "gh",
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"],
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


_TRANSIENT_CONCLUSIONS = frozenset({"CANCELLED", "TIMED_OUT", "STALE", "ACTION_REQUIRED"})


async def pr_failed_check_conclusions(repo: str, pr_number: int) -> list[str]:
    """Upper-cased conclusions of the PR's failing checks (FAILURE, CANCELLED, …).
    Lets the watcher tell a transient failure (cancelled/timed-out — worth one
    rerun) apart from a genuine build/test failure."""
    res = await run(
        "gh",
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"],
        throw_on_error=False,
    )
    if res.code != 0:
        return []
    checks = (json.loads(res.stdout or "{}").get("statusCheckRollup")) or []
    bad: list[str] = []
    for c in checks:
        conclusion = (c.get("conclusion") or c.get("state") or "").upper()
        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            bad.append(conclusion)
    return bad


def failures_are_transient(conclusions: list[str]) -> bool:
    """True when every failing check is a transient (cancelled/timed-out) one —
    i.e. a rerun has a real chance of going green without any code change."""
    return bool(conclusions) and all(c in _TRANSIENT_CONCLUSIONS for c in conclusions)


async def rerun_failed_runs(repo: str, branch: str) -> bool:
    """Re-run the failed jobs of the most recent workflow run on `branch`.
    Returns True if a rerun was triggered. Used at most once per PR."""
    res = await run(
        "gh",
        [
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            branch,
            "--limit",
            "20",
            "--json",
            "databaseId,conclusion,status,headSha",
        ],
        throw_on_error=False,
    )
    if res.code != 0:
        return False
    runs = json.loads(res.stdout or "[]")
    # Newest first; act on the latest commit's runs only.
    head_sha = next((r.get("headSha") for r in runs), None)
    triggered = False
    for r in runs:
        if r.get("headSha") != head_sha:
            continue
        conclusion = (r.get("conclusion") or "").upper()
        if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "STARTUP_FAILURE"):
            rr = await run(
                "gh",
                ["run", "rerun", str(r["databaseId"]), "--repo", repo, "--failed"],
                throw_on_error=False,
            )
            triggered = triggered or rr.code == 0
    return triggered


async def pr_comments(repo: str, pr_number: int) -> list[IssueComment]:
    """All human-visible comments on a PR, across the three channels GitHub
    splits them into: the conversation timeline (issues/:n/comments), inline
    review comments (pulls/:n/comments), and review summaries
    (pulls/:n/reviews). Merged and sorted oldest-first. Empty-body entries (e.g.
    a bare 'Commented' review) are dropped."""
    out: list[IssueComment] = []

    async def _collect(api_path: str, ts_field: str, inline: bool = False) -> None:
        res = await run("gh", ["api", api_path, "--paginate"], throw_on_error=False)
        if res.code != 0:
            return
        for c in json.loads(res.stdout or "[]"):
            body = c.get("body") or ""
            if not body.strip():
                continue
            out.append(
                IssueComment(
                    author=(c.get("user") or {}).get("login", "unknown"),
                    body=body,
                    created_at=c.get(ts_field) or "",
                    # Inline review comments carry the file/line + hunk they anchor
                    # to; keep it so rework edits the exact spot, not a guess.
                    path=c.get("path", "") if inline else "",
                    line=(c.get("line") or c.get("original_line")) if inline else None,
                    diff_hunk=c.get("diff_hunk", "") if inline else "",
                )
            )

    await _collect(f"repos/{repo}/issues/{pr_number}/comments", "created_at")
    await _collect(f"repos/{repo}/pulls/{pr_number}/comments", "created_at", inline=True)
    await _collect(f"repos/{repo}/pulls/{pr_number}/reviews", "submitted_at")
    out.sort(key=lambda c: c.created_at)
    return out


async def comment_pr(repo: str, pr_number: int, body: str) -> None:
    await run(
        "gh",
        ["pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        throw_on_error=False,
    )


async def pr_review_decision(repo: str, pr_number: int) -> str:
    """'approved' | 'changes_requested' | 'none', from the latest review per
    reviewer (GitHub's own review-gating rule: a reviewer's most recent review
    supersedes their earlier ones). 'changes_requested' wins over 'approved' so
    an outstanding block is never merged over."""
    res = await run(
        "gh",
        ["api", f"repos/{repo}/pulls/{pr_number}/reviews", "--paginate"],
        throw_on_error=False,
    )
    if res.code != 0:
        return "none"
    latest: dict[str, str] = {}
    for r in json.loads(res.stdout or "[]"):
        state = (r.get("state") or "").upper()
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(r.get("user") or {}).get("login", "")] = state
    states = set(latest.values())
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    return "none"


async def pr_labels(repo: str, pr_number: int) -> list[str]:
    res = await run(
        "gh",
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "labels"],
        throw_on_error=False,
    )
    if res.code != 0:
        return []
    data = json.loads(res.stdout or "{}")
    return [lbl["name"] for lbl in (data.get("labels") or [])]


async def pr_changed_files(repo: str, pr_number: int) -> list[ChangedFile]:
    res = await run("gh", ["pr", "view", str(pr_number), "--repo", repo, "--json", "files"])
    data = json.loads(res.stdout or "{}")
    return [
        ChangedFile(file=f["path"], added=f.get("additions", 0), deleted=f.get("deletions", 0))
        for f in (data.get("files") or [])
    ]


async def merge_pr(repo: str, pr_number: int) -> None:
    await run("gh", ["pr", "merge", str(pr_number), "--repo", repo, "--squash", "--delete-branch"])


async def comment_issue(repo: str, issue: int, body: str) -> None:
    await run(
        "gh",
        ["issue", "comment", str(issue), "--repo", repo, "--body", body],
        throw_on_error=False,
    )
