"""Thin async wrapper over subprocess using argv arrays (no shell).

Task input (issue text, prompts) flows into git/gh arguments, so we never pass
it through a shell where it could be reinterpreted.

Three environments, deliberately not one. `run()` inherits the process
environment, because `gh` and `git` need the credentials that are in it.
`agent_env()` returns a scrubbed copy for commands the *agent* chose to run: on
a source with no container, those run as `bash -lc` on the host, and inheriting
the process environment would put DEEPSEEK_API_KEY and the GitHub token one
`env` call away from a model steered by attacker-controlled issue text.
`git_env()` is the third: the credentials stay, but the checkout is not allowed
to tell git which commands to run.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 10 * 60

# Variables an ordinary build/test command needs. Anything not named here is
# withheld from agent-run shell commands — including everything `.env` loaded.
_AGENT_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TZ",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        # Toolchain roots that are location, not credential.
        "NVM_DIR",
        "NODE_PATH",
        "PNPM_HOME",
        "JAVA_HOME",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "PYENV_ROOT",
        "VIRTUAL_ENV",
        # Marks the run as automated for the tools that check it.
        "CI",
    }
)


# Config git must not take from the checkout, expressed as environment rather
# than as `-c` flags. `-c` only reaches the git process we start ourselves; `gh`
# shells out to git for clone, push and `pr create`, and those children inherit
# the environment but not our command line. GIT_CONFIG_* has the same precedence
# as `-c` (verified: it overrides a repo-local value), so this is the only form
# that covers both.
#
# This layer is defence in depth, not the guarantee: it names keys, and the set
# of config keys that execute a command is open-ended. git_integrity.py is what
# actually holds, by allowlisting the keys that may be present at all.
_GIT_HARDENING: tuple[tuple[str, str], ...] = (
    # A hook left in the checkout — by the agent, or by whoever pushed to the
    # repo — must not run as us.
    ("core.hooksPath", "/dev/null"),
    # Executed on every index refresh, i.e. on `git status`, `add` and `commit`.
    ("core.fsmonitor", "false"),
    # `ext::sh -c …` is a remote URL that executes.
    ("protocol.ext.allow", "never"),
)


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment overlay for a git or gh call: the hardening above, plus
    whatever the caller adds. Applied on top of the process environment, which
    these calls still need — they are the ones that carry credentials."""
    env: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(_GIT_HARDENING))}
    for i, (key, value) in enumerate(_GIT_HARDENING):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env.update(extra or {})
    return env


def agent_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a command the agent chose to run: allowlisted host
    variables only, plus whatever the caller passes explicitly."""
    env = {k: v for k, v in os.environ.items() if k in _AGENT_ENV_ALLOWLIST}
    env.setdefault("CI", "1")
    env.update(extra or {})
    return env


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
    inherit_env: bool = True,
    throw_on_error: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Run a command and capture output. Raises on non-zero exit unless
    throw_on_error is False.

    With `inherit_env=False`, `env` is the complete environment of the child
    rather than an overlay on this process's — that is how agent-run commands
    are kept away from the runner's secrets.
    """
    full_env = {**os.environ, **(env or {})} if inherit_env else dict(env or {})
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
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"`{cmd} {' '.join(args)}` timed out after {timeout_s}s") from None

    code = proc.returncode or 0
    result = RunResult(
        stdout=out_b.decode(errors="replace"), stderr=err_b.decode(errors="replace"), code=code
    )
    if code != 0 and throw_on_error:
        detail = result.stderr or result.stdout
        raise RuntimeError(f"`{cmd} {' '.join(args)}` failed (exit {code}):\n{detail}")
    return result
