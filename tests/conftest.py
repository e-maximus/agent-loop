"""Test bootstrap: point the app at a throwaway SQLite DB and a dummy API key.

Importing agent_loop has no side effects — no connection is opened and no
setting is read until something asks — so this only has to run before the first
*use*, not before the first import. Every test still starts from an empty tasks
table so the global queries (e.g. awaiting_review_prs) are deterministic.

Tracing is forced off here. The file tools are real LangChain StructuredTools,
so a test that invokes one directly is a traceable run with no parent — with a
developer's `.env` supplying LANGSMITH_TRACING, the suite posts a root trace per
tool call into the same project the agent reports to. Tests stay offline.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agent_loop.config import get_settings
from agent_loop.db import tasks

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="agent-loop-test-"), "test.db")
os.environ.setdefault("AGENT_LOOP_CONFIG", "/dev/null")
get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_db():
    tasks.conn.execute("DELETE FROM tasks")
    tasks.conn.commit()
    yield


def make_async(return_value):
    """A stand-in async function that ignores its args and returns a value.
    Records its calls on `.calls` for assertions."""

    async def _fn(*args, **kwargs):
        _fn.calls.append((args, kwargs))
        return return_value

    _fn.calls = []
    return _fn
