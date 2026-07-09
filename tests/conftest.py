"""Test bootstrap: point the app at a throwaway SQLite DB and a dummy API key
BEFORE any open_claw import (config validates the key at import time, and db.py
opens its connection at import time). Every test starts from an empty tasks
table so the global queries (e.g. awaiting_review_prs) are deterministic.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="open-claw-test-"), "test.db")
os.environ.setdefault("OPEN_CLAW_CONFIG", "/dev/null")

import pytest  # noqa: E402

from open_claw import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    db._db.execute("DELETE FROM tasks")
    db._db.commit()
    yield


def make_async(return_value):
    """A stand-in async function that ignores its args and returns a value.
    Records its calls on `.calls` for assertions."""
    async def _fn(*args, **kwargs):
        _fn.calls.append((args, kwargs))
        return return_value
    _fn.calls = []
    return _fn
