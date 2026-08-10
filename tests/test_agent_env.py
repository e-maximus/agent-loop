"""What the agent's own shell commands can see.

Without a container, `Bash` runs on the host as the same user as the runner. If
it inherited the runner's environment, "print the environment" — a plausible
instruction to plant in an issue body — would hand back the DeepSeek key and the
GitHub token. So agent-run commands get an allowlist, and only host-side work
that genuinely needs credentials (`gh`, `git`) keeps them.
"""

from __future__ import annotations

from agent_loop.config import ContainerConfig
from agent_loop.container import ExecEnv, task_container
from agent_loop.exec import agent_env


def test_secrets_are_withheld_from_agent_commands(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = agent_env()

    assert "sk-live-secret" not in env.values()
    assert "DEEPSEEK_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin", "a build still needs a toolchain"


def test_the_allowlist_keeps_what_a_build_needs(monkeypatch):
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("NVM_DIR", "/home/agent/.nvm")
    env = agent_env()
    assert env["HOME"] == "/home/agent"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["NVM_DIR"] == "/home/agent/.nvm"


def test_ci_is_marked_without_overriding_a_real_one(monkeypatch):
    """Build tools branch on CI, so it is always set — but a host that already
    defines it (GitHub Actions uses "true") keeps its own value. Asserting the
    literal "1" made this test pass locally and fail in CI, which is exactly the
    environment dependence a mocked suite is supposed to avoid."""
    monkeypatch.delenv("CI", raising=False)
    assert agent_env()["CI"] == "1"

    monkeypatch.setenv("CI", "true")
    assert agent_env()["CI"] == "true"


async def test_host_shell_runs_with_the_scrubbed_environment(monkeypatch):
    """The wiring, not just the helper: ExecEnv.shell must pass the scrubbed env
    AND turn inheritance off — passing it as an overlay would change nothing."""
    seen = {}

    async def fake_run(cmd, args, **kwargs):
        seen.update(cmd=cmd, args=args, **kwargs)
        return type("R", (), {"stdout": "", "stderr": "", "code": 0})()

    monkeypatch.setattr("agent_loop.container.run", fake_run)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret")

    await ExecEnv(repo_path="/repo", workdir="/repo").shell("npm test")

    assert seen["inherit_env"] is False
    assert "DEEPSEEK_API_KEY" not in seen["env"]


async def test_the_container_gets_git_read_only(monkeypatch):
    """The path-gate does not cover the container's shell, and a writable `.git`
    in the mount is host code execution: the agent writes a command into
    `.git/config`, and the next host-side `git status` — `has_changes`, in the
    same task — runs it. Read-only keeps `git diff HEAD`, which critic, security
    and summarize all run inside the container, working."""
    seen = {}

    async def fake_run(cmd, args, **kwargs):
        seen.setdefault("calls", []).append((cmd, args))
        return type("R", (), {"stdout": "cid123", "stderr": "", "code": 0})()

    monkeypatch.setattr("agent_loop.container.run", fake_run)
    async with task_container("/repo", ContainerConfig(image="img")):
        pass

    run_args = next(args for cmd, args in seen["calls"] if args[:1] == ["run"])
    mounts = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--mount"]
    assert "type=bind,src=/repo,dst=/workspace" in mounts
    assert "type=bind,src=/repo/.git,dst=/workspace/.git,readonly" in mounts


async def test_container_mode_does_not_forward_the_host_environment(monkeypatch):
    """`docker exec` starts the process from the container's own environment, so
    there is nothing to scrub — but the command must still be the agent's."""
    seen = {}

    async def fake_run(cmd, args, **kwargs):
        seen.update(cmd=cmd, args=args, **kwargs)
        return type("R", (), {"stdout": "", "stderr": "", "code": 0})()

    monkeypatch.setattr("agent_loop.container.run", fake_run)
    await ExecEnv(repo_path="/repo", workdir="/workspace", container_id="abc").shell("npm test")

    assert seen["cmd"] == "docker"
    assert seen["args"][:2] == ["exec", "-w"]
    assert seen.get("env") is None
