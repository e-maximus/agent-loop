"""Scoped logging with per-task file routing, built on the stdlib.

Lifecycle logs (startup, poller, merge-watcher) go to a rotating general log file
and the console. Work that runs inside a task is routed to that task's own file
via a contextvar (the Python analogue of the TS AsyncLocalStorage task-log).
Files always get debug-and-up; the console honours LOG_LEVEL.

It is stdlib `logging` rather than a hand-rolled writer for one reason that
matters here: the libraries this runner is built on (LangChain, httpx) log their
own deprecation and retry warnings, and a private logger sends those nowhere. On
a machine that deploys itself, the warning that an API changed is exactly the
one you want to have seen before the release goes red.

Configuration is lazy — nothing is created until the first record is emitted, so
importing a module never makes a directory.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

LOG_DIR = Path("./data/logs").resolve()
LOG_FILE = LOG_DIR / "agent-loop.log"
TASK_LOG_DIR = LOG_DIR / "tasks"

# Keep a bounded amount on disk. scripts/prune-logs.sh handles the per-task
# files; the general log rotates itself so it cannot grow without limit between
# runs of that script.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

_ROOT_NAME = "agent_loop"


@dataclass
class TaskLogCtx:
    stream: TextIO
    file: Path


# Active per-task log sink, discoverable without threading the task id through
# every call. Set by task_log_scope().
_current_task_log: contextvars.ContextVar[TaskLogCtx | None] = contextvars.ContextVar(
    "current_task_log", default=None
)


def current_task_log() -> TaskLogCtx | None:
    return _current_task_log.get()


class _UtcFormatter(logging.Formatter):
    """`<iso-utc> LEVEL [scope] message` — the format the task logs and
    scripts/agent-loopctl already read."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        scope = getattr(record, "scope", record.name.removeprefix(f"{_ROOT_NAME}."))
        line = f"{ts} {record.levelname:<5} [{scope}] {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_FORMATTER = _UtcFormatter()


class _TaskFileHandler(logging.Handler):
    """Routes a record to the task log file that is active on this context.

    Records emitted outside a task fall through to the general handler; records
    inside one go only to the task file, so a task's log reads as one story.
    """

    def emit(self, record: logging.LogRecord) -> None:
        sink = current_task_log()
        if sink is None:
            return
        try:
            sink.stream.write(_FORMATTER.format(record) + "\n")
            sink.stream.flush()
        except Exception:  # noqa: BLE001 — logging must never crash the app
            self.handleError(record)


class _GeneralFileHandler(RotatingFileHandler):
    """The general log: skipped while a task log is active, and it creates its
    directory on first write rather than at import."""

    def _open(self):  # type: ignore[override]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return super()._open()

    def emit(self, record: logging.LogRecord) -> None:
        if current_task_log() is not None:
            return
        super().emit(record)


_configured = False


def _configure() -> None:
    """Attach handlers to the root logger once.

    Handlers go on the *root* so that library logs (LangChain, httpx, urllib3)
    land in the same files; only the level differs — our own logger is verbose,
    everything else is warnings-and-up.
    """
    global _configured  # noqa: PLW0603 — process-wide handler setup runs once
    if _configured:
        return
    _configured = True

    console_level = os.environ.get("LOG_LEVEL", "info").upper()
    if console_level == "WARN":
        console_level = "WARNING"

    console = logging.StreamHandler()
    console.setFormatter(_FORMATTER)
    console.setLevel(getattr(logging, console_level, logging.INFO))

    general = _GeneralFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8", delay=True
    )
    general.setFormatter(_FORMATTER)
    general.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # libraries: warnings and up
    for handler in (console, general, _TaskFileHandler()):
        root.addHandler(handler)

    logging.getLogger(_ROOT_NAME).setLevel(logging.DEBUG)  # our own: everything
    logging.captureWarnings(True)


def _safe_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except (TypeError, ValueError):
        return str(v)


class Logger:
    """Thin scope-carrying wrapper. `extra` is appended to the message rather
    than passed through as LogRecord attributes: call sites use it for a payload
    to read, not for structured fields to filter on."""

    def __init__(self, scope: str) -> None:
        self._scope = scope
        self._log = logging.getLogger(f"{_ROOT_NAME}.{scope}")

    def _emit(self, level: int, msg: str, extra: Any) -> None:
        _configure()
        if extra is not None:
            msg = f"{msg} {_safe_str(extra)}"
        self._log.log(level, msg, extra={"scope": self._scope})

    def debug(self, msg: str, extra: Any = None) -> None:
        self._emit(logging.DEBUG, msg, extra)

    def info(self, msg: str, extra: Any = None) -> None:
        self._emit(logging.INFO, msg, extra)

    def warning(self, msg: str, extra: Any = None) -> None:
        self._emit(logging.WARNING, msg, extra)

    def error(self, msg: str, extra: Any = None) -> None:
        self._emit(logging.ERROR, msg, extra)


def create_logger(scope: str) -> Logger:
    return Logger(scope)


# ── Per-task log file ──────────────────────────────────────────────────────
def _stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")


def task_log_path(source: str, key: str) -> Path:
    return TASK_LOG_DIR / f"{source}-{key}-{_stamp()}.log"


class task_log_scope:
    """Context manager: route logging inside the block to a dedicated per-task
    file. `key` is a short identifier (e.g. issue12). Exposes the file path."""

    def __init__(self, source: str, key: str) -> None:
        self.file = task_log_path(source, key)
        self._stream: TextIO | None = None
        self._token: Any = None

    def __enter__(self) -> Path:
        _configure()
        TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._stream = self.file.open("a", encoding="utf-8")
        self._token = _current_task_log.set(TaskLogCtx(stream=self._stream, file=self.file))
        return self.file

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _current_task_log.reset(self._token)
        if self._stream is not None:
            self._stream.close()
