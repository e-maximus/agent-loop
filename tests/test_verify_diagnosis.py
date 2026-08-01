"""Keeping a red build honest: log condensing, PR adoption, and the poller's
GitHub-side dedup. These are the pieces that decide whether a failure the agent
cannot fix costs one cheap call or three full fix cycles."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_loop.config import GithubSourceConfig
from agent_loop.db import tasks
from agent_loop.github import gh
from agent_loop.github import poller as poller_mod
from agent_loop.github.autofix_graph import (
    MAX_FIX_CYCLES,
    after_diagnose,
    after_verify,
    condense,
)
from agent_loop.github.poller import GithubPoller
from tests.conftest import make_async


# ── condense ───────────────────────────────────────────────────────────────
def test_baseline_verify_is_on_by_default_and_configurable():
    # The YAML key is the contract; the field name is not.
    assert GithubSourceConfig(type="github", id="t", repo="o/r").baseline_verify is True
    assert (
        GithubSourceConfig(type="github", id="t", repo="o/r", baselineVerify=False).baseline_verify is False
    )


def test_condense_keeps_the_error_a_repeated_warning_would_evict():
    # The real shape of the failure that started this: one assertion drowned by
    # a dev-server warning logged on every page load.
    noise = "\n".join(["[WebServer] Clerk: loaded with development keys"] * 500)
    text = f"Error: expected 'Yesterday' to equal 'Today'\n{noise}"
    out = condense(text, 200)
    assert "expected 'Yesterday'" in out
    assert "(× 500)" in out


def test_condense_counts_repeats_and_preserves_first_seen_order():
    out = condense("a\nb\na\nb\na\nc\n", 1000)
    assert out.splitlines() == ["a  (× 3)", "b  (× 2)", "c"]


def test_condense_drops_blank_lines_and_takes_the_tail():
    assert condense("x\n\n\ny\n", 1000).splitlines() == ["x", "y"]
    assert condense("aaaa\nbbbb\n", 4) == "bbbb"


# ── routing after a red build ──────────────────────────────────────────────
def state(**kw):
    return {"issue": 84, "build_ok": False, **kw}


def test_green_build_skips_diagnosis():
    assert after_verify(state(build_ok=True)) == "critic"
    assert after_verify(state()) == "diagnose"


def test_environment_failure_never_costs_a_fix_cycle():
    # The #84 case: e2e red because Clerk JS could not load in the container.
    # Cycles here are ~6 minutes each and cannot possibly help.
    assert after_diagnose(state(failure_cause="ENVIRONMENT", implement_runs=0)) == "summarize"


def test_code_failure_still_gets_its_bounded_fix_cycles():
    assert after_diagnose(state(failure_cause="CODE", implement_runs=0)) == "implement"
    assert after_diagnose(state(failure_cause="CODE", implement_runs=MAX_FIX_CYCLES)) == "summarize"


def test_unreadable_diagnosis_falls_back_to_fixing_the_code():
    # A verdict we cannot read must not be what excuses the agent from its diff.
    assert after_diagnose(state(implement_runs=0)) == "implement"


def test_flaky_failure_is_retried_once_then_escalated():
    assert after_diagnose(state(failure_cause="FLAKY", verify_retries=1)) == "verify"
    assert after_diagnose(state(failure_cause="FLAKY", verify_retries=2)) == "summarize"


# ── open_pr / find_open_pr ─────────────────────────────────────────────────
def fake_run(*results):
    """Return canned RunResults in order, recording the argv of each call."""
    queue = list(results)

    async def _run(cmd, args, **kwargs):
        _run.calls.append([cmd, *args])
        r = queue.pop(0)
        if r.code != 0 and kwargs.get("throw_on_error", True):
            raise RuntimeError("unexpected throw")
        return r

    _run.calls = []
    return _run


def result(code=0, stdout="", stderr=""):
    return SimpleNamespace(code=code, stdout=stdout, stderr=stderr)


PR_URL = "https://github.com/o/r/pull/85"
EXISTS = result(
    code=1, stderr=f'a pull request for branch "issue-84" into branch "main" already exists:\n{PR_URL}'
)
PR_LIST = result(stdout=json.dumps([{"number": 85, "url": PR_URL}]))


async def test_open_pr_returns_the_created_pr(monkeypatch):
    monkeypatch.setattr(gh, "run", fake_run(result(stdout=PR_URL)))
    pr = await gh.open_pr("/repo", base="main", title="t", body="b", repo="o/r", head="issue-84")
    assert (pr.number, pr.url, pr.existing) == (85, PR_URL, False)


async def test_open_pr_adopts_an_existing_pr_for_the_branch(monkeypatch):
    # A re-run of the same issue after the task DB was reset: the branch was
    # just pushed, so the open PR now carries this work — losing the whole run
    # to `gh pr create` exiting 1 is the bug this guards.
    monkeypatch.setattr(gh, "run", fake_run(EXISTS, PR_LIST))
    pr = await gh.open_pr("/repo", base="main", title="t", body="b", repo="o/r", head="issue-84")
    assert (pr.number, pr.url, pr.existing) == (85, PR_URL, True)


async def test_open_pr_raises_when_there_is_no_pr_to_adopt(monkeypatch):
    monkeypatch.setattr(gh, "run", fake_run(result(code=1, stderr="boom"), result(stdout="[]")))
    with pytest.raises(RuntimeError, match="boom"):
        await gh.open_pr("/repo", base="main", title="t", body="b", repo="o/r", head="issue-84")


async def test_find_open_pr_survives_a_broken_gh_response(monkeypatch):
    monkeypatch.setattr(gh, "run", fake_run(result(stdout="not json")))
    assert await gh.find_open_pr("o/r", "issue-84") is None


# ── poller dedup ───────────────────────────────────────────────────────────
def make_poller():
    cfg = GithubSourceConfig(type="github", id="t", repo="o/r")
    return GithubPoller(cfg, SimpleNamespace(poke=lambda: None))


def issue(number=84):
    return gh.Issue(number=number, title="t", body="b", url="u", labels=["bug"], author="a")


async def test_poller_skips_an_issue_that_already_has_an_open_pr(monkeypatch):
    monkeypatch.setattr(poller_mod, "find_open_pr", make_async(gh.PullRequest(85, PR_URL)))
    p = make_poller()

    assert await p._consider_fixable("o/r", issue(), "bug") is False
    assert tasks.is_github_issue_taken("o/r", 84) is False  # nothing was enqueued

    # Cached: a permanently-handled issue must not cost an API call per poll.
    poller_mod.find_open_pr = None  # would explode if called again
    assert await p._consider_fixable("o/r", issue(), "bug") is False


async def test_poller_skips_an_issue_we_already_commented_on(monkeypatch):
    monkeypatch.setattr(poller_mod, "find_open_pr", make_async(None))
    bot = gh.IssueComment(author="a", body="🤖 **agent-loop:** rejected", created_at="2026-01-01")
    monkeypatch.setattr(poller_mod, "list_issue_comments", make_async([bot]))
    assert await make_poller()._consider_fixable("o/r", issue(), "bug") is False


async def test_poller_enqueues_an_issue_github_shows_nothing_for(monkeypatch):
    monkeypatch.setattr(poller_mod, "find_open_pr", make_async(None))
    monkeypatch.setattr(poller_mod, "list_issue_comments", make_async([]))
    assert await make_poller()._consider_fixable("o/r", issue(), "bug") is True
    assert tasks.is_github_issue_taken("o/r", 84) is True
