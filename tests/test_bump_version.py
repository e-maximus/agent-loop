"""The release bump: which level a merged PR's labels ask for, and what that
does to the version the watcher deploys.

Worth covering because nothing else checks it — the workflow runs once, on
`main`, after the PR is already merged, so a wrong answer ships before anyone
reads the log.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# scripts/ is not a package, so load the module by path rather than importing it.
_SPEC = importlib.util.spec_from_file_location(
    "bump_version", Path(__file__).resolve().parent.parent / "scripts" / "bump_version.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bump_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bump_version)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ("", "patch"),
        ("bug,documentation", "patch"),
        ("release:minor", "minor"),
        ("bug,release:minor,enhancement", "minor"),
        ("release:major", "major"),
        # Both labels: release the larger one. Under-releasing is the failure
        # that reaches the machine quietly.
        ("release:minor,release:major", "major"),
        # Whitespace survives `join(labels.*.name, ',')` on labels with spaces.
        ("good first issue, release:minor", "minor"),
    ],
)
def test_level_from_labels(labels: str, expected: str):
    assert bump_version.level_from_labels(labels) == expected


@pytest.mark.parametrize(
    ("current", "level", "expected"),
    [
        ("0.4.0", "patch", "0.4.1"),
        ("0.4.9", "minor", "0.5.0"),
        ("0.4.9", "major", "1.0.0"),
        # Minor and major zero the parts below them — the watcher compares
        # versions for inequality, so a stale patch digit would be a silent
        # downgrade after the next bump.
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
    ],
)
def test_next_version(current: str, level: str, expected: str):
    assert bump_version.next_version(current, level) == expected


def test_unknown_level_is_an_error():
    with pytest.raises(ValueError, match="unknown bump level"):
        bump_version.next_version("1.0.0", "moderate")


def test_reads_and_replaces_only_the_project_version():
    # `requires-python` and the pyright/ruff pins live in the same file and must
    # not be mistaken for the release version.
    text = (
        "[project]\n"
        'name = "agent-loop"\n'
        'version = "0.4.0"\n'
        'requires-python = ">=3.12"\n\n'
        "[tool.pyright]\n"
        'pythonVersion = "3.12"\n'
    )
    assert bump_version.read_version(text) == "0.4.0"

    rewritten = bump_version.replace_version(text, "0.5.0")
    assert 'version = "0.5.0"' in rewritten
    assert 'pythonVersion = "3.12"' in rewritten
    assert 'requires-python = ">=3.12"' in rewritten


def test_missing_version_line_is_an_error():
    with pytest.raises(ValueError, match="no `version"):
        bump_version.read_version('[project]\nname = "agent-loop"\n')


def test_the_real_pyproject_is_readable():
    # Guards the regex against a formatting change in the file it edits.
    assert bump_version.read_version(bump_version._PYPROJECT.read_text()).count(".") == 2
