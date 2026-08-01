"""SQLite-backed task state and dedup queries.

Single machine, so no Redis: tasks are persisted here and the in-process queue
pumps them through. Task state survives restarts: tasks still marked 'running'
after a crash are put back on the queue by `reset_orphans()` — an interruption is
not a failure.

The connection is opened lazily on first use, not at import: importing this
module must not create directories or touch the disk. Schema changes go through
the `_MIGRATIONS` ladder keyed on `PRAGMA user_version`, because the release
watcher rolls this code onto a machine with an existing database and nobody is
there to run a migration by hand.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from .config import db_path

TaskStatus = Literal[
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    # PR opened and waiting on human review / CI / merge (not yet closed).
    "awaiting_review",
    # Intake triage gate refused the task as unsafe — terminal, never retried.
    "rejected",
]

# Statuses that mean "this issue is already being handled" so the poller must
# not enqueue it again. awaiting_review (PR in flight) and rejected are terminal
# for polling purposes alongside the classic queued/running/done/failed.
_TAKEN_STATUSES = ("queued", "running", "done", "failed", "awaiting_review", "rejected")
_IN_FLIGHT_STATUSES = ("queued", "running", "awaiting_review")
GithubKind = Literal["bug", "feature", "question"]


class _TaskMetaRequired(TypedDict):
    """What the poller always writes when it enqueues. Split out so that reading
    `meta["repo"]` is a plain access rather than something every call site has to
    defend against — these four exist for the lifetime of the task."""

    sourceId: str
    repo: str
    issue: int
    kind: str  # bug | feature | question


class TaskMeta(_TaskMetaRequired, total=False):
    """The JSON blob on a task row.

    Every key here is read in one module and written in another (the poller
    writes what the watcher reads, the watcher writes what the source reads), so
    a typo is a task that silently stops being managed rather than an error.
    Declaring the shape is what makes that a type error instead.
    """

    url: str
    body: str
    author: str
    authorAssociation: str
    # Set once work starts on the issue.
    branch: str
    prNumber: int
    prUrl: str
    # open | merged | manual (handed off to a human; the watcher stops acting)
    prState: str
    # How many times CI was automatically re-run for this PR (budget: 1).
    ciRerunCount: int
    # Cursors: the newest comment already answered / already turned into rework.
    answeredThrough: str
    prReviewedThrough: str
    # Set by the watcher to route a comment back through the worker.
    mode: str  # "" | rework | pr_reply
    feedback: str


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

    def meta_dict(self) -> TaskMeta:
        # The column is JSON we wrote ourselves; the cast records that the
        # shape is asserted here and checked nowhere else.
        return cast("TaskMeta", json.loads(self.meta)) if self.meta else cast("TaskMeta", {})


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


# ── schema ────────────────────────────────────────────────────────────────
# Append-only ladder: each entry migrates from its index to index+1, and
# PRAGMA user_version records where an existing database sits. Never edit or
# reorder a shipped entry — the deployed machine has already run it.
_MIGRATIONS: tuple[str, ...] = (
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
    """,
)


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for step in range(version, len(_MIGRATIONS)):
        conn.executescript(_MIGRATIONS[step])
        # PRAGMA does not take a bound parameter; step+1 is an int we produced.
        conn.execute(f"PRAGMA user_version = {step + 1}")
        conn.commit()


class Database:
    """Lazily-opened connection plus the task queries.

    `check_same_thread=False` lets a worker offloaded to a thread reach the same
    connection, so every write takes `_lock`: sqlite3 serialises statements but
    not the read-then-write pairs this class performs.
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                path = db_path()
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA foreign_keys = ON")
                _migrate(conn)
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── task queries ──────────────────────────────────────────────────────
    def create(self, source: str, prompt: str, cwd: str, meta: Any | None = None) -> TaskRow:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO tasks (source, prompt, cwd, meta) VALUES (?, ?, ?, ?)",
                (source, prompt, cwd, json.dumps(meta) if meta is not None else None),
            )
            self.conn.commit()
            row = self.get(int(cur.lastrowid or 0))
        if row is None:  # pragma: no cover — the row was just inserted
            raise RuntimeError("task row vanished immediately after insert")
        return row

    def get(self, task_id: int) -> TaskRow | None:
        return _row(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def set_meta(self, task_id: int, meta: Any) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET meta = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(meta), task_id),
            )
            self.conn.commit()

    def is_github_issue_taken(self, repo: str, issue: int) -> bool:
        """Has this issue already been picked up? Bug/feature issues are one-shot:
        a 'failed' attempt counts as taken too, so a crash is NOT retried in a
        loop — it is left for a human (see the failure comment on the issue)."""
        placeholders = ",".join("?" * len(_TAKEN_STATUSES))
        # S608: the only interpolation is `placeholders`, a run of "?" we
        # generated from len() — every value is still bound.
        r = self.conn.execute(
            f"""SELECT id FROM tasks WHERE source = 'github'
                 AND status IN ({placeholders})
                 AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
               LIMIT 1""",  # noqa: S608
            (*_TAKEN_STATUSES, repo, issue),
        ).fetchone()
        return r is not None

    def has_github_issue_in_flight(self, repo: str, issue: int) -> bool:
        placeholders = ",".join("?" * len(_IN_FLIGHT_STATUSES))
        # S608: the only interpolation is `placeholders`, a run of "?" we
        # generated from len() — every value is still bound.
        r = self.conn.execute(
            f"""SELECT id FROM tasks WHERE source = 'github'
                 AND status IN ({placeholders})
                 AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
               LIMIT 1""",  # noqa: S608
            (*_IN_FLIGHT_STATUSES, repo, issue),
        ).fetchone()
        return r is not None

    def latest_github_issue_task(self, repo: str, issue: int) -> TaskRow | None:
        return _row(
            self.conn.execute(
                """SELECT * FROM tasks WHERE source = 'github'
                     AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
                   ORDER BY id DESC LIMIT 1""",
                (repo, issue),
            ).fetchone()
        )

    def awaiting_review_prs(self) -> list[TaskRow]:
        """Open PRs the watcher still actively manages: task is awaiting_review,
        a PR exists, and it has not been handed off to a human (`prState` =
        'manual') — those stay awaiting_review but the watcher stops acting."""
        rows = self.conn.execute(
            """SELECT * FROM tasks WHERE source = 'github'
                 AND status = 'awaiting_review'
                 AND json_extract(meta, '$.prNumber') IS NOT NULL
                 AND (json_extract(meta, '$.prState') IS NULL
                      OR json_extract(meta, '$.prState') != 'manual')"""
        ).fetchall()
        return [r for r in (_row(x) for x in rows) if r]

    def claim_next(self) -> TaskRow | None:
        """Take the oldest queued task, atomically.

        A SELECT followed by an UPDATE would let two workers claim the same row;
        one statement with RETURNING cannot. The queue runs at concurrency 1
        today, but the knob exists and must not be a trap.
        """
        with self._lock:
            r = self.conn.execute(
                """UPDATE tasks SET status = 'running', updated_at = datetime('now')
                    WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
                                 ORDER BY id ASC LIMIT 1)
                RETURNING *"""
            ).fetchone()
            self.conn.commit()
        return _row(r)

    def set_status(self, task_id: int, status: TaskStatus) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, task_id),
            )
            self.conn.commit()

    def finish_done(self, task_id: int, result: str | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status = 'done', result = ?, updated_at = datetime('now') WHERE id = ?",
                (result, task_id),
            )
            self.conn.commit()

    def finish_failed(self, task_id: int, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?",
                (error, task_id),
            )
            self.conn.commit()

    def reset_orphans(self) -> int:
        """On startup, any task left 'running' means the process died mid-flight.
        Put it back on the queue to be retried from the start — an interruption is
        not a failure, so it must not be treated as a terminal 'failed' attempt
        (which is never re-run)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status = 'queued', updated_at = datetime('now') WHERE status = 'running'"
            )
            self.conn.commit()
        return cur.rowcount


tasks = Database()
