"""Schema versioning and the claim, both of which fail silently when wrong.

The release watcher rolls new code onto a machine with an existing database and
nobody is there to migrate it by hand, so `user_version` has to be right. And
`claim_next` has to be atomic, because the concurrency knob exists.
"""

from __future__ import annotations

import sqlite3
import threading

from agent_loop.config import get_settings
from agent_loop.db import _MIGRATIONS, Database, _migrate, tasks


def test_a_fresh_database_is_stamped_at_the_latest_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fresh.db"))
    get_settings.cache_clear()
    db = Database()
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)
    finally:
        db.close()
        get_settings.cache_clear()


def test_migrating_an_existing_database_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    try:
        _migrate(conn)
        conn.execute("INSERT INTO tasks (source, prompt, cwd) VALUES ('github', 'keep me', '/x')")
        conn.commit()

        # Re-running the ladder (a redeploy of the same version) must not
        # re-apply a step or lose a row.
        _migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)
        assert conn.execute("SELECT prompt FROM tasks").fetchone()["prompt"] == "keep me"
    finally:
        conn.close()


def test_claim_next_hands_a_task_to_exactly_one_caller():
    """Two threads racing on one queued row. A SELECT-then-UPDATE claim lets both
    win; the single RETURNING statement cannot."""
    tasks.create("github", "only one", "/tmp", {"issue": 1})

    claimed = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        row = tasks.claim_next()
        if row is not None:
            claimed.append(row.id)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 1, f"task claimed {len(claimed)} times"


def test_claim_next_returns_none_on_an_empty_queue():
    assert tasks.claim_next() is None


def test_claim_next_marks_the_row_running():
    row = tasks.create("github", "work", "/tmp", {"issue": 2})
    claimed = tasks.claim_next()
    assert claimed is not None
    assert claimed.status == "running"
    stored = tasks.get(row.id)
    assert stored is not None and stored.status == "running"


def test_reset_orphans_requeues_rather_than_failing():
    """An interrupted task is not a failed one: 'failed' is terminal and would
    never be retried, which is the opposite of what a deploy mid-task needs."""
    row = tasks.create("github", "interrupted", "/tmp", {"issue": 3})
    tasks.set_status(row.id, "running")

    assert tasks.reset_orphans() == 1
    stored = tasks.get(row.id)
    assert stored is not None and stored.status == "queued"
