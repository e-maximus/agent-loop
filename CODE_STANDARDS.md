# Code standards

The rules a change in this repo has to satisfy, and *why* each one exists here
specifically. [AGENTS.md](AGENTS.md) says what the project is and how to work in
it; this file says what the code itself must look like.

Read it before writing code. Most entries came from a review that found the same
class of problem in several places at once — they are not style preferences,
they are the failure modes this particular runner has.

## The one fact that drives most of these

**This process runs unattended, deploys itself, and acts on attacker-controlled
input.** A merge into `main` with a version bump reaches a real machine with real
credentials, with nobody watching. So: fail loudly at startup rather than
mysteriously at hour three, keep untrusted text away from anything that executes,
and make every dependency change deliberate.

## 1. No side effects at import time

Importing any module must not read settings, open a database, create a
directory, or exit the process.

- Settings come from `config.get_settings()` (cached); paths from `db_path()` /
  `config_path()`.
- Validation that terminates — a missing API key, an unreadable config — happens
  in `main()`, via `require_api_key()` / `load_sources()`.
- Connections and log files open lazily, on first use.

*Why:* import-time `sys.exit` made the package unusable outside the entry point
and forced tests to order their imports around it. A module that can kill the
process on import cannot be imported by a tool, a script, or a test.

## 2. Untrusted input has exactly three gates, and they are all in code

Issue text, PR comments, and everything a model derives from them are hostile
until proven otherwise.

- **Never build a shell string.** `exec.run()` takes an argv array. There is no
  second way to start a process.
- **File tools go through `_resolve_in()`.** Inside the checkout, and never
  inside `.git/` — publishing runs `git commit` on the host, and git executes
  `.git/hooks/` when it does.
- **Agent-chosen commands get `agent_env()`,** never the process environment.
  Adding a variable to `_AGENT_ENV_ALLOWLIST` requires asking what an attacker
  who reads it would gain.

A diff touching `tools.py`, `exec.py`, `container.py`, or any place issue text
reaches a prompt must extend
[tests/test_path_gate.py](tests/test_path_gate.py) or
[tests/test_agent_env.py](tests/test_agent_env.py) — passing the existing ones is
not evidence about the new path. See [SECURITY.md](SECURITY.md).

## 3. Gates fail closed

Anything parsing a model verdict uses `verdicts.parse_verdict()` with the *safe*
option as `default`. An unreadable answer must block, never allow. Say so in a
comment at the call site, because the next reader's instinct is to "simplify" it
to a substring check — and `PASS` is a substring of `BYPASS`.

## 4. Nothing blocking on the event loop

One process runs the pollers, the PR watcher, and the task worker on one loop. A
synchronous read of a large tree stalls all three.

- Filesystem work inside a tool goes through `asyncio.to_thread`.
- Anything scanning many files needs a bound (`GREP_MAX_FILE_BYTES`,
  `GREP_MAX_MATCHES`) — a regex from a model is not a trusted regex.
- Database calls are the exception: they are single-statement and fast, and the
  connection is guarded by a lock.

## 5. State that crosses a module boundary is typed

`TaskMeta` ([db.py](agent_loop/db.py)), `AutofixState`, `AnswerState` are
`TypedDict`s split into a required half (what the producer always writes) and an
optional half (what nodes add later). Read required keys with `[]`, optional ones
with `.get(default)`.

*Why:* the poller writes what the watcher reads, and the watcher writes what the
source reads. A mistyped key used to be a task that silently stopped being
managed; now it is a type error.

## 6. Schema changes are migrations

Append a step to `_MIGRATIONS` in [db.py](agent_loop/db.py). Never edit or
reorder a shipped step — the deployed machine has already run it. `PRAGMA
user_version` tracks where an existing database sits, because nobody is there to
migrate it by hand.

## 7. Dependencies are bounded and pinned

`pyproject.toml` carries upper bounds; `constraints.txt` pins the exact set the
machine installs. Regenerate it deliberately, in the PR that has actually run
against the new versions:

```bash
.venv/bin/pip freeze --exclude-editable > constraints.txt   # keep the header
```

*Why:* without this, `pip install` during an unattended redeploy resolves
whatever PyPI serves that morning, and a library's release becomes a deploy
nobody made.

## 8. Logging is stdlib, scoped, and never crashes

`create_logger(scope)` returns a wrapper over `logging.getLogger`. Levels are
`debug/info/warning/error` (**not** `warn`). Work inside a task is routed to that
task's file automatically by `task_log_scope` — do not thread a file handle
through call signatures. Library warnings land in the same files on purpose:
on a self-deploying machine, "this API is deprecated" is the message you want to
have seen before the release goes red.

## 9. Comments say why

The house style is a comment that explains the reason, the tradeoff, or the
failure it prevents — not one that restates the line below it. Docstrings on the
modules that carry a decision (the graph, the boundaries, the queue) are where
the design lives; keep them true when you change the code. A stale docstring is
worse than none, because it is believed.

All committed and user-facing text is in **English**.

## 10. Every code change bumps the version

`version` in `pyproject.toml` moves in the same PR as the change. Not "when it
feels worth deploying" — always, for any PR that touches code. Docs-only PRs may
keep it.

**You classify the size yourself**, against the table in
[AGENTS.md](AGENTS.md#versioning--releases): PATCH for a fix or a
behaviour-preserving refactor, MINOR for new capability, a new config field, or
changed pipeline behaviour, MAJOR when an existing `.env` /
`agent-loop.config.yaml` stops working or a human step is needed before the
deploy is safe. Two calls are not yours to make freely: a change to a boundary
(`tools.py`, `exec.py`, `container.py`) is **at least MINOR**, and anything
needing a config edit before restart is **MAJOR** however small the diff.

State the bump and the reason in the PR description — one line, so the next
person reading `git log` can see what shipped and why it was sized that way.

*Why:* the version is what the release watcher compares against the deployed
copy, so it is the only signal that says "this is meant to run". Leaving it
alone used to mean "merge now, ship later", which quietly accumulated unshipped
work whose combined behaviour nobody had ever run as a unit. The cost of the
rule is that merging is now the moment of commitment — see §11, which is what
makes that safe.

## 11. What has to be green

```bash
.venv/bin/python -m ruff check .          # lint
.venv/bin/python -m ruff format --check . # formatting
.venv/bin/pyright                         # types: zero errors, not "fewer"
.venv/bin/python -m pytest -q             # tests
```

CI runs exactly these four. In Claude Code the `checklist` agent runs them plus
the config/example parity checks.

- **Suppress a rule at the call site, with a reason** (`# noqa: BLE001 — the tool
  returns the failure to the model`). A blanket ignore in `pyproject.toml` is for
  rules that are wrong about this codebase as a whole, and each one there carries
  a comment saying why.
- **Tests are fully mocked**: no network, no LLM, no Docker, whole suite under a
  second. Fake the model with a stub returning canned text, fake the shell with
  canned exit codes.
- **Test the decision, not the plumbing** — which node runs next, whether a PR is
  adopted, whether a command was skipped, whether a path was refused.
