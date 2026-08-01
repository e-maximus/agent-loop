"""Execution environment for a task's shell commands.

When a source declares a `container` block, the agent's Bash/build/test commands
run inside an ephemeral per-task Docker container with the checkout bind-mounted
at /workspace. When it doesn't, the same commands run on the host. File tools
(Read/Write/Edit/Grep/Glob) always operate on the host filesystem (the mount),
so only shell execution differs between the two modes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .config import ContainerConfig
from .exec import RunResult, agent_env, run
from .logging import create_logger

log = create_logger("container")


@dataclass
class ExecEnv:
    """Where a task's shell commands run. `container_id is None` = host mode."""

    repo_path: str  # host path to the checkout
    workdir: str  # cwd for shell commands (/workspace in a container, repo_path on host)
    container_id: str | None = None

    async def shell(self, command: str) -> RunResult:
        """Run a shell command in this environment. Never raises — returns the
        result so the caller (tool executor) can hand output back to the model.

        The command is chosen by the agent, so it never sees the runner's
        environment. In container mode that is free: `docker exec` does not
        forward the client's environment into the container. In host mode it
        takes the allowlist in exec.agent_env(), which is the only thing between
        a `cat .env`-shaped instruction in an issue and the DeepSeek key.
        """
        if self.container_id:
            return await run(
                "docker",
                ["exec", "-w", self.workdir, self.container_id, "bash", "-lc", command],
                throw_on_error=False,
            )
        return await run(
            "bash",
            ["-lc", command],
            cwd=self.repo_path,
            env=agent_env(),
            inherit_env=False,
            throw_on_error=False,
        )


@asynccontextmanager
async def task_container(repo_path: str, cfg: ContainerConfig | None) -> AsyncIterator[ExecEnv]:
    """Yield an ExecEnv for the task. In container mode, start a detached
    container with the checkout mounted and tear it down on exit."""
    if cfg is None:
        yield ExecEnv(repo_path=repo_path, workdir=repo_path, container_id=None)
        return

    res = await run(
        "docker",
        [
            "run",
            "-d",
            "--rm",
            *cfg.run_args,
            "-w",
            "/workspace",
            "--mount",
            f"type=bind,src={repo_path},dst=/workspace",
            cfg.image,
            "sleep",
            "infinity",
        ],
    )
    cid = res.stdout.strip()
    log.info(f"started container {cid[:12]} from {cfg.image}")
    try:
        yield ExecEnv(repo_path=repo_path, workdir="/workspace", container_id=cid)
    finally:
        await run("docker", ["rm", "-f", cid], throw_on_error=False)
        log.info(f"removed container {cid[:12]}")
