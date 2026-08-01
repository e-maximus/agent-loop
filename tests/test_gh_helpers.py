"""Pure/deterministic gh.py helpers: the CI roll-up and transient detection.
The only external call (`run`) is mocked with canned CLI JSON."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_loop.github import gh


def fake_run(stdout="", code=0):
    async def _run(*args, **kwargs):
        return SimpleNamespace(code=code, stdout=stdout, stderr="")

    return _run


def rollup(*checks):
    return json.dumps({"statusCheckRollup": list(checks)})


def test_failures_are_transient():
    assert gh.failures_are_transient(["CANCELLED"]) is True
    assert gh.failures_are_transient(["CANCELLED", "TIMED_OUT"]) is True
    assert gh.failures_are_transient(["CANCELLED", "FAILURE"]) is False
    assert gh.failures_are_transient([]) is False  # nothing failed ≠ transient


async def test_pr_checks_state_success(monkeypatch):
    monkeypatch.setattr(
        gh,
        "run",
        fake_run(
            rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
            )
        ),
    )
    assert await gh.pr_checks_state("o/r", 1) == "success"


async def test_pr_checks_state_failure_on_cancelled(monkeypatch):
    monkeypatch.setattr(
        gh,
        "run",
        fake_run(
            rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "CANCELLED"},
            )
        ),
    )
    assert await gh.pr_checks_state("o/r", 1) == "failure"


async def test_pr_checks_state_pending(monkeypatch):
    monkeypatch.setattr(
        gh,
        "run",
        fake_run(
            rollup(
                {"status": "IN_PROGRESS", "conclusion": None},
            )
        ),
    )
    assert await gh.pr_checks_state("o/r", 1) == "pending"


async def test_pr_checks_state_none_when_no_checks(monkeypatch):
    monkeypatch.setattr(gh, "run", fake_run(rollup()))
    assert await gh.pr_checks_state("o/r", 1) == "none"


async def test_pr_failed_check_conclusions(monkeypatch):
    monkeypatch.setattr(
        gh,
        "run",
        fake_run(
            rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
                {"status": "COMPLETED", "conclusion": "CANCELLED"},
            )
        ),
    )
    assert await gh.pr_failed_check_conclusions("o/r", 1) == ["FAILURE", "CANCELLED"]


def test_is_bot_comment():
    assert gh.is_bot_comment("🤖 **agent-loop:** hi") is True
    assert gh.is_bot_comment("  🤖 leading space") is True
    assert gh.is_bot_comment("a human comment") is False
