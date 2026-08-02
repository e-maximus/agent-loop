"""Compute and apply the release bump for a merged PR.

The version in `pyproject.toml` is what the release watcher deploys, so it has
to move on every merge — but a PR that *edits* it collides with every other open
PR the moment one of them lands. So nobody edits it by hand: the merge does,
once, on `main`, driven by the merged PR's labels.

    python scripts/bump_version.py --labels "release:minor,bug"   # 0.4.0 -> 0.5.0
    python scripts/bump_version.py --level major                  # 0.4.0 -> 1.0.0
    python scripts/bump_version.py --labels "" --dry-run          # print, touch nothing

Patch is the floor: an unlabelled PR still ships. `level_from_labels` and
`next_version` are pure so the decision is testable without a repo — see
[tests/test_bump_version.py](../tests/test_bump_version.py).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

# Anchored to the start of a line so it matches `[project].version` and not the
# `python-version`/`requires-python` pins further down the file.
_VERSION_RE = re.compile(r'^version = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)

MAJOR_LABEL = "release:major"
MINOR_LABEL = "release:minor"


def level_from_labels(labels: str) -> str:
    """Bump level for a set of PR labels, as a comma-separated string.

    Major wins over minor so a PR carrying both is not silently under-released;
    anything else — including no labels at all — is a patch.
    """
    names = {name.strip() for name in labels.split(",")}
    if MAJOR_LABEL in names:
        return "major"
    if MINOR_LABEL in names:
        return "minor"
    return "patch"


def next_version(current: str, level: str) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump level {level!r} — expected major, minor or patch")


def read_version(text: str) -> str:
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError('no `version = "x.y.z"` line in pyproject.toml')
    return ".".join(match.groups())


def replace_version(text: str, version: str) -> str:
    return _VERSION_RE.sub(f'version = "{version}"', text, count=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--labels", help="comma-separated PR labels")
    source.add_argument("--level", choices=["major", "minor", "patch"])
    parser.add_argument("--dry-run", action="store_true", help="print the bump, write nothing")
    args = parser.parse_args(argv)

    level = args.level or level_from_labels(args.labels)
    text = _PYPROJECT.read_text()
    current = read_version(text)
    new = next_version(current, level)

    print(f"{current} -> {new} ({level})", file=sys.stderr)
    if not args.dry_run:
        _PYPROJECT.write_text(replace_version(text, new))
    # stdout is the machine-readable half: the workflow appends it straight to
    # $GITHUB_OUTPUT and tags the release from it.
    print(f"version={new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
