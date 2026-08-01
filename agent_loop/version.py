"""Build identity: which release of agent-loop this process is running.

Attached to every LangSmith trace. The release watcher rolls the prod checkout
forward unattended, so "which code produced this run?" cannot be answered from
the timestamp alone — the trace has to carry it.
"""

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    """Version from the installed package metadata — i.e. straight from
    pyproject.toml, since we are installed with `pip install -e .`."""
    try:
        return _pkg_version("agent-loop")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _read_commit() -> str:
    """The deployed commit: what the watcher last rolled out, falling back to
    the checkout's HEAD for a dev run."""
    stamp = _ROOT / "data" / "deployed.sha"
    try:
        sha = stamp.read_text().strip()
        if sha:
            return sha[:12]
    except OSError:
        pass

    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_ROOT, capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    return "unknown"


AGENT_VERSION = _read_version()
AGENT_COMMIT = _read_commit()
BUILD = f"{AGENT_VERSION}+{AGENT_COMMIT}"


def trace_config(**extra: Any) -> dict[str, Any]:
    """Run config for a traced LLM/graph call: build identity plus whatever task
    context the caller has (source id, issue, task id, kind).

    Metadata on a root run is inherited by its children, so tagging the graph
    invocation covers every node, tool call and LLM call inside it. Calls made
    outside a graph are their own roots and must pass this themselves.
    """
    metadata: dict[str, Any] = {"version": AGENT_VERSION, "commit": AGENT_COMMIT}
    metadata.update({k: v for k, v in extra.items() if v not in (None, "")})
    return {"metadata": metadata, "tags": [f"agent-loop@{AGENT_VERSION}"]}
