"""The build identity attached to every LangSmith trace."""

from __future__ import annotations

from agent_loop.version import AGENT_COMMIT, AGENT_VERSION, trace_config


def test_version_comes_from_package_metadata():
    # Installed with `pip install -e .`, so this is pyproject's version verbatim.
    assert AGENT_VERSION not in ("", "0.0.0+unknown")


def test_trace_config_carries_build_identity():
    cfg = trace_config()
    assert cfg["metadata"] == {"version": AGENT_VERSION, "commit": AGENT_COMMIT}
    assert cfg["tags"] == [f"agent-loop@{AGENT_VERSION}"]


def test_trace_config_merges_task_context():
    cfg = trace_config(issue=42, source_id="goals-app")
    assert cfg["metadata"]["issue"] == 42
    assert cfg["metadata"]["source_id"] == "goals-app"
    assert cfg["metadata"]["version"] == AGENT_VERSION


def test_trace_config_drops_empty_context():
    # Absent context (a task with no PR yet) must not become a null column in
    # the LangSmith filters.
    cfg = trace_config(issue=None, repo="", kind="bug")
    assert "issue" not in cfg["metadata"]
    assert "repo" not in cfg["metadata"]
    assert cfg["metadata"]["kind"] == "bug"
