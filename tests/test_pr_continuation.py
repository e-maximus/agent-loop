"""An issue whose PR is already open must be continued, not overwritten.

The DB is local and the PR is not: a reset DB, a run that failed at publish, or a
human requeue all produce a first pass on an issue that already has our PR open.
The decision under test is what prepare does with that branch — build on it,
or close the PR and start over — and that publish then pushes onto the PR it was
handed instead of opening a second one.

git and gh are stubbed; nothing here touches a network or a checkout.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_loop.config import GithubSourceConfig
from agent_loop.github import gh
from agent_loop.github import source as source_mod
from agent_loop.github.autofix_graph import build_autofix_graph
from agent_loop.github.gh import PullRequest
from agent_loop.github.source import GithubSource
from tests.conftest import make_async

OPEN_PR = PullRequest(number=85, url="https://github.com/o/r/pull/85")


class DummyQueue:
    def poke(self):
        pass


def make_source():
    cfg = GithubSourceConfig(type="github", id="t", repo="o/r")
    return GithubSource(cfg, DummyQueue(), None)


def stub_prepare(monkeypatch, *, pr, continued):
    """Stub the four gh calls prepare can make; return the recorders."""
    calls = SimpleNamespace(
        find=make_async(pr),
        continue_=make_async(continued),
        close=make_async(None),
        prepare=make_async(None),
    )
    monkeypatch.setattr(source_mod, "find_open_pr", calls.find)
    monkeypatch.setattr(source_mod, "continue_branch", calls.continue_)
    monkeypatch.setattr(source_mod, "close_pr", calls.close)
    monkeypatch.setattr(source_mod, "prepare_branch", calls.prepare)
    return calls


# ── prepare: which branch a first pass starts from ─────────────────────────
async def test_no_open_pr_starts_from_base(monkeypatch):
    calls = stub_prepare(monkeypatch, pr=None, continued=False)
    got = await make_source()._prepare_first_pass("o/r", 84, "/repo", "main", "issue-84")

    assert got is None
    assert len(calls.prepare.calls) == 1
    assert calls.continue_.calls == [] and calls.close.calls == []


async def test_open_pr_that_merges_cleanly_is_continued(monkeypatch):
    calls = stub_prepare(monkeypatch, pr=OPEN_PR, continued=True)
    got = await make_source()._prepare_first_pass("o/r", 84, "/repo", "main", "issue-84")

    assert got == OPEN_PR
    # The point of the whole file: the branch is NOT reset onto base, so the
    # commits already on the PR survive the second pass.
    assert calls.prepare.calls == []
    assert calls.close.calls == []
    assert calls.continue_.calls[0][0] == ("/repo", "issue-84", "main")


async def test_unusable_pr_is_closed_and_the_branch_restarts(monkeypatch):
    calls = stub_prepare(monkeypatch, pr=OPEN_PR, continued=False)
    got = await make_source()._prepare_first_pass("o/r", 84, "/repo", "main", "issue-84")

    # No PR handed back → publish opens a new one for the fresh branch.
    assert got is None
    assert calls.close.calls[0][0][:2] == ("o/r", 85)
    assert "Closing this PR" in calls.close.calls[0][0][2]
    assert len(calls.prepare.calls) == 1


async def test_a_fork_pr_is_left_alone(monkeypatch):
    fork_pr = PullRequest(number=99, url="u", cross_repository=True)
    calls = stub_prepare(monkeypatch, pr=fork_pr, continued=True)
    got = await make_source()._prepare_first_pass("o/r", 84, "/repo", "main", "issue-84")

    # Somebody else's branch that happens to share our name: never pushed to,
    # never closed.
    assert got is None
    assert calls.continue_.calls == [] and calls.close.calls == []
    assert len(calls.prepare.calls) == 1


# ── continue_branch: when a branch can be built on ─────────────────────────
CLEAN_CONFIG_Z = "core.bare\nfalse\0remote.origin.url\nhttps://github.com/o/r.git\0"


def stub_git(monkeypatch, codes, *, envs: list | None = None):
    """Fake git; `codes` maps a subcommand to its exit code (default 0).

    Also stubs the config read `_git` now does before every call — otherwise
    these tests would shell out to a real git in a directory that does not
    exist, which is the kind of accidental unmocking the suite exists to avoid.
    """
    ran: list[list[str]] = []

    async def _run(cmd, args, *, cwd=None, env=None, throw_on_error=True):
        assert cmd == "git"
        ran.append(args)
        if envs is not None:
            envs.append(env)
        return SimpleNamespace(code=codes.get(args[0], 0), stdout="", stderr="")

    async def _config_run(cmd, args, **kwargs):
        return SimpleNamespace(code=0, stdout=CLEAN_CONFIG_Z, stderr="")

    monkeypatch.setattr(gh, "run", _run)
    monkeypatch.setattr("agent_loop.git_integrity.run", _config_run)
    return ran


async def test_continue_branch_merges_base_in(monkeypatch):
    ran = stub_git(monkeypatch, {})
    assert await gh.continue_branch("/repo", "issue-84", "main") is True
    assert ran == [
        ["fetch", "origin", "issue-84"],
        ["checkout", "-B", "issue-84", "origin/issue-84"],
        ["merge", "--no-edit", "origin/main"],
    ]


async def test_continue_branch_gives_up_when_the_branch_is_gone(monkeypatch):
    ran = stub_git(monkeypatch, {"fetch": 1})
    assert await gh.continue_branch("/repo", "issue-84", "main") is False
    # Nothing was checked out: the caller's reset onto base is still valid.
    assert ran == [["fetch", "origin", "issue-84"]]


async def test_continue_branch_aborts_a_conflicting_merge(monkeypatch):
    ran = stub_git(monkeypatch, {"merge": 1})
    assert await gh.continue_branch("/repo", "issue-84", "main") is False
    # A half-merged tree would be handed to the agent as if it were the repo.
    assert ran[-1] == ["merge", "--abort"]


async def test_every_call_here_is_hardened_not_just_the_merge(monkeypatch):
    """The agent writes into this checkout, and these three commands run on the
    host with credentials. The merge used to be the only one carrying the hook
    flag, so a `post-checkout` planted by an earlier task on the same checkout
    executed on the `checkout -B` two lines above it. Assert on all of them."""
    envs: list[dict | None] = []
    ran = stub_git(monkeypatch, {}, envs=envs)

    await gh.continue_branch("/repo", "issue-84", "main")

    assert [a[0] for a in ran] == ["fetch", "checkout", "merge"]
    for env in envs:
        assert env is not None
        pairs = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        }
        assert pairs["core.hooksPath"] == "/dev/null"
        assert pairs["core.fsmonitor"] == "false"


# ── publish: pushes onto the PR it was handed ──────────────────────────────
class FakeEnv:
    repo_path = "/repo"

    async def shell(self, cmd: str):
        return SimpleNamespace(code=0, stdout="", stderr="")


def stub_publish(monkeypatch):
    calls = SimpleNamespace(
        open=make_async(PullRequest(number=999, url="new")),
        update=make_async(None),
        comment_pr=make_async(None),
        comment_issue=make_async(None),
        commit=make_async(None),
        push=make_async(None),
    )
    monkeypatch.setattr(gh, "open_pr", calls.open)
    monkeypatch.setattr(gh, "update_pr", calls.update)
    monkeypatch.setattr(gh, "comment_pr", calls.comment_pr)
    monkeypatch.setattr(gh, "comment_issue", calls.comment_issue)
    monkeypatch.setattr(gh, "commit_all", calls.commit)
    monkeypatch.setattr(gh, "push", calls.push)
    return calls


def publish_node():
    cfg = GithubSourceConfig(type="github", id="t", repo="o/r")
    return build_autofix_graph(None, FakeEnv(), cfg).nodes["publish"].bound


def state(**kw):
    return {
        "repo": "o/r",
        "issue": 84,
        "kind": "bug",
        "title": "labels are off by one",
        "base": "main",
        "branch": "issue-84",
        "url": "u",
        "body": "",
        "pr_summary": "fixed the day arithmetic",
        **kw,
    }


async def test_publish_pushes_onto_the_continued_pr(monkeypatch):
    calls = stub_publish(monkeypatch)
    out = await publish_node().ainvoke(state(made_changes=True, pr_number=85, pr_url="pr-85-url"))

    # A second PR for one issue is the failure this prevents.
    assert calls.open.calls == []
    assert len(calls.push.calls) == 1
    assert calls.update.calls[0][0] == ("o/r", 85)
    assert out["pr_number"] == 85 and out["pr_url"] == "pr-85-url"


async def test_publish_reports_no_changes_on_the_continued_pr(monkeypatch):
    calls = stub_publish(monkeypatch)
    out = await publish_node().ainvoke(
        state(made_changes=False, pr_number=85, pr_url="pr-85-url", diff_text="nothing to do")
    )

    # Nothing to add, but the PR is still open and still ours to watch — so it
    # comes back in the state and the task parks in awaiting_review.
    assert calls.comment_issue.calls == []
    assert calls.comment_pr.calls[0][0][:2] == ("o/r", 85)
    assert out["pr_number"] == 85
    assert calls.commit.calls == [] and calls.push.calls == []


async def test_publish_still_opens_a_pr_for_a_fresh_branch(monkeypatch):
    calls = stub_publish(monkeypatch)
    out = await publish_node().ainvoke(state(made_changes=True))

    assert len(calls.open.calls) == 1
    assert out["pr_number"] == 999
    assert calls.comment_issue.calls, "a new PR is announced on the issue"
