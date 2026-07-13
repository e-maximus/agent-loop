"""Thin async wrapper over subprocess using argv arrays (no shell).

Task input (issue text, prompts) flows into git/gh arguments, so we never pass
it through a shell where it could be reinterpreted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 10 * 60


@dataclass
class RunResult:
    stdout: str
    stderr: str
    code: int


async def run(
    cmd: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    throw_on_error: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Run a command and capture output. Raises on non-zero exit unless
    throw_on_error is False."""
    import os

    full_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        cmd,
        *args,
        cwd=cwd,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"`{cmd} {' '.join(args)}` timed out after {timeout_s}s")

    code = proc.returncode or 0
    result = RunResult(stdout=out_b.decode(errors="replace"), stderr=err_b.decode(errors="replace"), code=code)
    if code != 0 and throw_on_error:
        detail = result.stderr or result.stdout
        raise RuntimeError(f"`{cmd} {' '.join(args)}` failed (exit {code}):\n{detail}")
    return result
