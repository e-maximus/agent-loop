"""Lightweight scoped logging with per-task file routing.

Lifecycle logs (startup, poller, merge-watcher) go to a general log file and the
console. Work that runs inside a task is routed to that task's own file via a
contextvar (the Python analogue of the TS AsyncLocalStorage task-log). Files
always get debug-and-up; the console honours LOG_LEVEL.
"""

from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}
_MIN = os.environ.get("LOG_LEVEL", "info").lower()

LOG_DIR = Path("./data/logs").resolve()
LOG_FILE = LOG_DIR / "agent-loop.log"

_general_stream: TextIO | None = None


@dataclass
class TaskLogCtx:
    stream: TextIO
    file: Path


# Active per-task log sink, discoverable without threading the task id through
# every call. Set by run_with_task_log().
_current_task_log: contextvars.ContextVar[TaskLogCtx | None] = contextvars.ContextVar(
    "current_task_log", default=None
)


def current_task_log() -> TaskLogCtx | None:
    return _current_task_log.get()


def _general_file() -> TextIO:
    global _general_stream
    if _general_stream is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _general_stream = LOG_FILE.open("a", encoding="utf-8")
    return _general_stream


def _emit(level: str, scope: str, msg: str, extra: Any = None) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {level.upper():<5} [{scope}] {msg}"
    extra_str = "" if extra is None else " " + _safe_str(extra)

    sink = current_task_log()
    stream = sink.stream if sink else _general_file()
    try:
        stream.write(line + extra_str + "\n")
        stream.flush()
    except Exception:
        pass  # never let logging crash the app

    if _LEVELS.get(level, 1) < _LEVELS.get(_MIN, 1):
        return
    print(line + extra_str)


def _safe_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        import json

        return json.dumps(v, default=str)
    except Exception:
        return str(v)


class Logger:
    def __init__(self, scope: str) -> None:
        self._scope = scope

    def debug(self, msg: str, extra: Any = None) -> None:
        _emit("debug", self._scope, msg, extra)

    def info(self, msg: str, extra: Any = None) -> None:
        _emit("info", self._scope, msg, extra)

    def warn(self, msg: str, extra: Any = None) -> None:
        _emit("warn", self._scope, msg, extra)

    def error(self, msg: str, extra: Any = None) -> None:
        _emit("error", self._scope, msg, extra)


def create_logger(scope: str) -> Logger:
    return Logger(scope)


# ── Per-task log file ──────────────────────────────────────────────────────
TASK_LOG_DIR = Path("./data/logs/tasks").resolve()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def task_log_path(source: str, key: str) -> Path:
    return TASK_LOG_DIR / f"{source}-{key}-{_stamp()}.log"


class task_log_scope:
    """Context manager: route logging inside the block to a dedicated per-task
    file. `key` is a short identifier (e.g. issue12). Exposes the file path."""

    def __init__(self, source: str, key: str) -> None:
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.file = task_log_path(source, key)
        self._stream: TextIO | None = None
        self._token: Any = None

    def __enter__(self) -> Path:
        self._stream = self.file.open("a", encoding="utf-8")
        self._token = _current_task_log.set(TaskLogCtx(stream=self._stream, file=self.file))
        return self.file

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _current_task_log.reset(self._token)
        if self._stream is not None:
            self._stream.close()
