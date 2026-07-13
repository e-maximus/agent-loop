"""Agent tools (Read/Write/Edit/Bash/Grep/Glob) as LangChain StructuredTools.

Built per task by build_tools(env), closing over the ExecEnv so Bash runs in the
right place (host or container) while file tools operate on the host checkout.
Every file tool enforces the path-gate: paths must stay inside the checkout.
Ported from the TS backends/tools.ts (same names/limits).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .container import ExecEnv

READ_MAX_CHARS = 30_000
RESULT_MAX_CHARS = 20_000
GREP_MAX_MATCHES = 100
GLOB_MAX = 200

_EXCLUDE_DIRS = ("node_modules", ".git")


def _truncate(s: str, max_chars: int = RESULT_MAX_CHARS) -> str:
    if len(s) > max_chars:
        return s[:max_chars] + f"\n… [truncated {len(s) - max_chars} chars]"
    return s


class PathEscape(Exception):
    """A tool was asked to touch a path outside the checkout."""


def _resolve_in(repo_path: str, p: str) -> str:
    """Resolve p against the checkout and refuse to escape it."""
    root = os.path.realpath(repo_path)
    target = p if os.path.isabs(p) else os.path.join(root, p)
    resolved = os.path.realpath(target)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise PathEscape(f"Path {p} escapes the repo checkout.")
    return resolved


def _list_files(base: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), base)
            out.append(rel)
    return out


# ── arg schemas (param names mirror the Claude/TS tool set) ────────────────
class ReadArgs(BaseModel):
    file_path: str = Field(description="Path to the file (absolute or relative to the checkout).")
    offset: int | None = Field(default=None, description="Optional 1-based start line.")
    limit: int | None = Field(default=None, description="Optional number of lines to read.")


class WriteArgs(BaseModel):
    file_path: str
    content: str


class EditArgs(BaseModel):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to run in the checkout.")


class GrepArgs(BaseModel):
    pattern: str
    path: str | None = Field(default=None, description="Optional subdir to scope the search.")


class GlobArgs(BaseModel):
    pattern: str = Field(description='Glob pattern, e.g. "src/**/*.ts".')


def describe_tool(name: str, args: dict) -> str:
    """Compact one-line description for progress logs."""
    if name == "Bash":
        return f"Bash: {str(args.get('command', ''))[:160]}"
    if name in ("Read", "Write", "Edit"):
        return f"{name} {args.get('file_path')}"
    if name == "Grep":
        return f"Grep {args.get('pattern')}"
    if name == "Glob":
        return f"Glob {args.get('pattern')}"
    return f"{name}({', '.join(args.keys())})"


def build_tools(env: ExecEnv, *, include_write: bool = True) -> list[StructuredTool]:
    """Construct the tool set for one task. With include_write=False only
    read/inspect tools are returned (question/investigate handlers)."""
    repo = env.repo_path

    async def _read(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
        try:
            f = _resolve_in(repo, file_path)
            lines = Path(f).read_text(encoding="utf-8", errors="replace").split("\n")
            start = max(1, offset) if offset else 1
            end = start - 1 + limit if limit else len(lines)
            chosen = lines[start - 1 : end]
            numbered = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(chosen))
            return _truncate(numbered, READ_MAX_CHARS)
        except Exception as e:
            return f"Error running Read: {e}"

    async def _write(file_path: str, content: str) -> str:
        try:
            f = _resolve_in(repo, file_path)
            Path(f).parent.mkdir(parents=True, exist_ok=True)
            Path(f).write_text(content, encoding="utf-8")
            return f"Wrote {f}"
        except Exception as e:
            return f"Error running Write: {e}"

    async def _edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        try:
            f = _resolve_in(repo, file_path)
            content = Path(f).read_text(encoding="utf-8")
            count = content.count(old_string)
            if count == 0:
                return f"Error: old_string not found in {f}"
            if count > 1 and not replace_all:
                return f"Error: old_string is not unique in {f} ({count} matches). Pass replace_all or add context."
            updated = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
            Path(f).write_text(updated, encoding="utf-8")
            return f"Edited {f} ({count} replacement{'s' if count > 1 else ''})"
        except Exception as e:
            return f"Error running Edit: {e}"

    async def _bash(command: str) -> str:
        res = await env.shell(command)
        parts = [f"exit code: {res.code}"]
        if res.stdout:
            parts.append(f"stdout:\n{res.stdout}")
        if res.stderr:
            parts.append(f"stderr:\n{res.stderr}")
        return _truncate("\n".join(parts))

    async def _grep(pattern: str, path: str | None = None) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
            base = _resolve_in(repo, path) if path else os.path.realpath(repo)
            matches: list[str] = []
            for rel in _list_files(base):
                if len(matches) >= GREP_MAX_MATCHES:
                    break
                try:
                    text = Path(os.path.join(base, rel)).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for i, line in enumerate(text.split("\n")):
                    if rx.search(line):
                        matches.append(f"{rel}:{i + 1}: {line.strip()[:200]}")
                        if len(matches) >= GREP_MAX_MATCHES:
                            break
            return _truncate("\n".join(matches)) if matches else "No matches."
        except Exception as e:
            return f"Error running Grep: {e}"

    async def _glob(pattern: str) -> str:
        try:
            root = Path(os.path.realpath(repo))
            hits = [
                str(p.relative_to(root))
                for p in root.glob(pattern)
                if not any(part in _EXCLUDE_DIRS for part in p.parts)
            ][:GLOB_MAX]
            return "\n".join(hits) if hits else "No files matched."
        except Exception as e:
            return f"Error running Glob: {e}"

    tools = [
        StructuredTool.from_function(
            coroutine=_read, name="Read",
            description="Read a file from the filesystem. Returns its contents with line numbers.",
            args_schema=ReadArgs,
        ),
        StructuredTool.from_function(
            coroutine=_grep, name="Grep",
            description="Search file contents for a regular expression. Returns matching file:line entries.",
            args_schema=GrepArgs,
        ),
        StructuredTool.from_function(
            coroutine=_glob, name="Glob",
            description='List files matching a glob pattern (e.g. "src/**/*.ts").',
            args_schema=GlobArgs,
        ),
        StructuredTool.from_function(
            coroutine=_bash, name="Bash",
            description="Run a shell command in the working directory and return its output.",
            args_schema=BashArgs,
        ),
    ]
    if include_write:
        tools += [
            StructuredTool.from_function(
                coroutine=_write, name="Write",
                description="Create or overwrite a file with the given content.",
                args_schema=WriteArgs,
            ),
            StructuredTool.from_function(
                coroutine=_edit, name="Edit",
                description="Replace an exact string in a file. old_string must be unique unless replace_all is true.",
                args_schema=EditArgs,
            ),
        ]
    return tools
