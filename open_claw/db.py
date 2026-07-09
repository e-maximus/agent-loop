"""SQLite-backed task state and dedup queries.

Single machine, so no Redis: tasks are persisted here and the in-process queue
pumps them through. Task state survives restarts (queued tasks are picked up
again; tasks left 'running' after a crash are marked failed on boot).

Ported from the TS db.ts. The `chat_id` column (Telegram) has been dropped.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import DB_PATH

TaskStatus = Literal["queued", "running", "done", "failed", "cancelled"]
GithubKind = Literal["bug", "feature", "question"]


@dataclass
class TaskRow:
    id: int
    source: str
    prompt: str
    cwd: str
    status: str
    meta: str | None
    result: str | None
    error: str | None
    created_at: str
    updated_at: str

    def meta_dict(self) -> dict[str, Any]:
        return json.loads(self.meta) if self.meta else {}


def _row(r: sqlite3.Row | None) -> TaskRow | None:
    if r is None:
        return None
    return TaskRow(
        id=r["id"],
        source=r["source"],
        prompt=r["prompt"],
        cwd=r["cwd"],
        status=r["status"],
        meta=r["meta"],
        result=r["result"],
        error=r["error"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode = WAL")

_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source     TEXT NOT NULL,
        prompt     TEXT NOT NULL,
        cwd        TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'queued',
        meta       TEXT,
        result     TEXT,
        error      TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    """
)
_db.commit()


class _Tasks:
    def create(self, source: str, prompt: str, cwd: str, meta: Any | None = None) -> TaskRow:
        cur = _db.execute(
            "INSERT INTO tasks (source, prompt, cwd, meta) VALUES (?, ?, ?, ?)",
            (source, prompt, cwd, json.dumps(meta) if meta is not None else None),
        )
        _db.commit()
        row = self.get(int(cur.lastrowid))
        assert row is not None
        return row

    def get(self, task_id: int) -> TaskRow | None:
        return _row(_db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def set_meta(self, task_id: int, meta: Any) -> None:
        _db.execute(
            "UPDATE tasks SET meta = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(meta), task_id),
        )
        _db.commit()

    def is_github_issue_taken(self, repo: str, issue: int) -> bool:
        """Has this issue already been picked up? Bug/feature issues are one-shot:
        a 'failed' attempt counts as taken too, so a crash is NOT retried in a
        loop — it is left for a human (see the failure comment on the issue)."""
        r = _db.execute(
            """SELECT id FROM tasks WHERE source = 'github'
                 AND status IN ('queued','running','done','failed')
                 AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
               LIMIT 1""",
            (repo, issue),
        ).fetchone()
        return r is not None

    def has_github_issue_in_flight(self, repo: str, issue: int) -> bool:
        r = _db.execute(
            """SELECT id FROM tasks WHERE source = 'github'
                 AND status IN ('queued','running')
                 AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
               LIMIT 1""",
            (repo, issue),
        ).fetchone()
        return r is not None

    def latest_github_issue_task(self, repo: str, issue: int) -> TaskRow | None:
        return _row(
            _db.execute(
                """SELECT * FROM tasks WHERE source = 'github'
                     AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
                   ORDER BY id DESC LIMIT 1""",
                (repo, issue),
            ).fetchone()
        )

    def awaiting_merge(self) -> list[TaskRow]:
        """PRs opened by autofix that still need a merge decision."""
        rows = _db.execute(
            """SELECT * FROM tasks WHERE source = 'github'
                 AND json_extract(meta, '$.prNumber') IS NOT NULL
                 AND (json_extract(meta, '$.mergeState') IS NULL
                      OR json_extract(meta, '$.mergeState') = 'pending')"""
        ).fetchall()
        return [r for r in (_row(x) for x in rows) if r]

    def claim_next(self) -> TaskRow | None:
        r = _db.execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        row = _row(r)
        if row is None:
            return None
        self.set_status(row.id, "running")
        row.status = "running"
        return row

    def set_status(self, task_id: int, status: TaskStatus) -> None:
        _db.execute(
            "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )
        _db.commit()

    def finish_done(self, task_id: int, result: str | None) -> None:
        _db.execute(
            "UPDATE tasks SET status = 'done', result = ?, updated_at = datetime('now') WHERE id = ?",
            (result, task_id),
        )
        _db.commit()

    def finish_failed(self, task_id: int, error: str) -> None:
        _db.execute(
            "UPDATE tasks SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?",
            (error, task_id),
        )
        _db.commit()

    def reset_orphans(self) -> int:
        """On startup, any task left 'running' means the process died mid-flight.
        Put it back on the queue to be retried from the start — an interruption is
        not a failure, so it must not be treated as a terminal 'failed' attempt
        (which is never re-run)."""
        cur = _db.execute(
            "UPDATE tasks SET status = 'queued', "
            "updated_at = datetime('now') WHERE status = 'running'"
        )
        _db.commit()
        return cur.rowcount


tasks = _Tasks()
