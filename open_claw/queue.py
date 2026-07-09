"""In-process asyncio queue backed by SQLite.

Single machine, so no Redis: tasks are persisted in the `tasks` table and this
class pumps them through a bounded worker pool. Survives restarts (queued tasks
are picked up again; interrupted 'running' ones are marked failed on boot).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .db import TaskRow, tasks
from .logging import create_logger

log = create_logger("queue")

TaskRunner = Callable[[TaskRow], Awaitable[str | None]]

_IDLE_POLL_S = 5.0  # safety re-poll so a missed poke never stalls the queue


class Queue:
    def __init__(self, runner: TaskRunner, concurrency: int = 1) -> None:
        self._runner = runner
        self._concurrency = concurrency
        self._wake = asyncio.Event()
        self._stopped = False
        self._workers: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._stopped = False
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self._concurrency)]

    def poke(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _worker(self) -> None:
        while not self._stopped:
            self._wake.clear()
            task = tasks.claim_next()
            if task is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=_IDLE_POLL_S)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._execute(task)

    async def _execute(self, task: TaskRow) -> None:
        log.info(f"▶ task #{task.id} ({task.source}) started")
        try:
            result = await self._runner(task)
            # The runner may have parked the task in a non-terminal state it owns
            # (e.g. 'awaiting_review' while a PR waits for review, or 'rejected'
            # by the intake gate). Only close it if it is still 'running'.
            current = tasks.get(task.id)
            if current is not None and current.status == "running":
                tasks.finish_done(task.id, result)
            log.info(f"✓ task #{task.id} finished ({(tasks.get(task.id) or task).status})")
        except Exception as err:  # noqa: BLE001 — a task failing must not kill the worker
            import traceback

            message = "".join(traceback.format_exception(err))
            tasks.finish_failed(task.id, message)
            log.error(f"✗ task #{task.id} failed", message)
        finally:
            self.poke()
