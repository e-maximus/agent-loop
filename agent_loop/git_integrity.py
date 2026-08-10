"""The checkout's git config as a boundary: only known-safe keys may be in it.

A git repository executes commands named in its own `.git/config`. Not one
command — a dozen, and the list grows with git releases: `core.fsmonitor` on
every index refresh, `filter.<n>.clean` on `git add` (bound by a `.gitattributes`
that lives in the *worktree*, where the path-gate must allow writes),
`merge.<n>.driver` on `git merge`, `diff.<n>.command` on `git diff`,
`gpg.program` on a signed `git commit`, `credential.helper` on push — which also
hands over the token. Every host-side git call this runner makes is a trigger for
at least one of them.

So `-c core.hooksPath=/dev/null` was never the lock it looked like: it names one
key out of an open set, and `include.path` can pull the rest in from a file in
the worktree. The fix has to be shaped the other way round — a **closed set of
keys that are allowed**, so a key nobody has thought of yet fails by default
rather than passing by default.

That is what this module is. It is deliberately absolute rather than differential
(no before/after snapshot): git legitimately writes to the config while we work
— `push -u` appends a `[branch]` section — so a hash of the file would be a false
alarm on every task, while an allowlist tolerates exactly the keys git itself
writes and nothing else.

**What this does not cover, and why that is fine.** `.git/hooks/` is not checked
here: `git_env()` in exec.py points `core.hooksPath` at /dev/null for every git
process this runner starts, including the ones `gh` spawns internally, so a hook
file in the checkout is never looked up. Checking it as well would fail every
target repo whose `npm ci` installs husky, for no gain.

**Known false positives**, both fail-closed by choice: a repo using git-lfs
(`filter.lfs.*` is a command definition, and we cannot tell it from an attacker's)
and a repo with submodules (`submodule.<n>.url` accepts `ext::`, which executes).
Both stop the task with a comment on the issue rather than being waved through;
if this runner is ever pointed at such a repo, that is a decision to make
deliberately, in this file.
"""

from __future__ import annotations

import re

from .exec import git_env, run


class GitControlTampered(Exception):
    """The checkout's git config carries something we did not put there.

    Carries `repo_path` so the caller can quarantine the checkout: it is reused
    across tasks, so a poisoned one that is merely reported stays poisoned and
    meets the next task.
    """

    def __init__(self, repo_path: str, findings: list[str]) -> None:
        self.repo_path = repo_path
        self.findings = findings
        super().__init__(f"{repo_path}: unexpected git config — {'; '.join(findings)}")


# Keys a fresh `gh repo clone` + `checkout -B` + `push -u` actually produces.
# Taken from a real clone, not from memory: adding one because "it looks
# harmless" is how an allowlist turns back into a denylist.
_SAFE_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.symlinks",
    }
)

_SAFE_PATTERNS = (
    # `remote.<name>.url` is allowed as a key but checked by value below.
    re.compile(r"^remote\.[^.]+\.(url|pushurl|fetch)$"),
    re.compile(r"^branch\.[^.]+\.(remote|pushremote|merge|rebase)$"),
    # Set by the server's repository format (SHA-256 repos, reftable).
    re.compile(r"^extensions\.(objectformat|compatobjectformat|refstorage)$"),
)

# Where a remote may point. An attacker who can set `remote.origin.url` does not
# need a command: `ext::sh -c …` is a URL that executes, and a plain redirect to
# their own host turns the next push into delivery.
_SAFE_REMOTE_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+(\.git)?/?$")


def _is_safe_key(key: str) -> bool:
    return key in _SAFE_KEYS or any(p.match(key) for p in _SAFE_PATTERNS)


def check_config(entries: list[tuple[str, str]]) -> list[str]:
    """The pure half: parsed `key=value` pairs in, human-readable findings out.

    Split from the subprocess so the decision can be tested without a git
    repository — the same reason the graph's routing functions are pure.
    """
    findings: list[str] = []
    for key, value in entries:
        if not _is_safe_key(key):
            findings.append(f"unexpected key `{key}` = `{value[:120]}`")
        elif key.endswith((".url", ".pushurl")) and not _SAFE_REMOTE_URL.match(value):
            findings.append(f"remote `{key}` points somewhere unexpected: `{value[:120]}`")
    return findings


async def assert_safe_config(repo_path: str) -> None:
    """Raise unless the checkout's local git config holds only allowlisted keys.

    Reads the config with git rather than by parsing the file, because git is the
    authority on what it will act upon. Note `--local --list` does *not* expand
    `include.path` — it reports the include key itself, which is not allowlisted,
    so a config hidden in the worktree is caught by the key rather than by its
    contents. `git config` neither refreshes the index nor runs a filter, so
    reading it cannot trigger what we are looking for.
    """
    res = await run(
        "git",
        ["config", "--local", "--list", "-z"],
        cwd=repo_path,
        env=git_env(),
        throw_on_error=False,
    )
    if res.code != 0:
        # No local config to read (not a checkout yet) is not a violation; the
        # caller runs this before git calls that may precede the clone.
        return

    entries: list[tuple[str, str]] = []
    for record in res.stdout.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        entries.append((key.strip().lower(), value))

    findings = check_config(entries)
    if findings:
        raise GitControlTampered(repo_path, findings)
