# Working in this repo

Guidance for agents making code changes here. Read this before you start, then
read **[CODE_STANDARDS.md](CODE_STANDARDS.md)** — this file says what the project
is and how to work in it; that one says what the code must look like (no
import-time side effects, how untrusted input is gated, typed cross-module
state, migrations, dependency pinning, and the four checks that must be green).
Both are binding.

## What this is

agent-loop is an autonomous GitHub issue **auto-fix** runner that executes on one
local machine. It polls repos, and for each labelled issue it screens the request,
investigates, plans, implements, tests, verifies, security-reviews the diff and
opens a PR — then watches that PR until it merges. Python + LangGraph, one
asyncio process, SQLite for state, `gh` for everything GitHub.

**This repo is the runner, not the code it writes.** A change here changes how
the agent behaves on every repo it works on, and the deployed copy on this
machine picks it up unattended. That is the weight to hold when editing.

## Project shape

- **The pipeline** — [github/autofix_graph.py](agent_loop/github/autofix_graph.py)
  is a LangGraph `StateGraph`; its docstring carries the current diagram, and it
  is the file that decides what the agent does. Each node sits at the right
  granularity: a tool-calling agent (`create_agent`) where exploration is needed
  (`investigate`, `implement`, `write_tests`), one focused LLM call where the
  input is already gathered (`plan`, `critic`, `diagnose`, `security`,
  `summarize`), and plain deterministic code where there is no judgement to make
  (`baseline`, `verify`). Routing lives in **pure functions** of the state
  (`after_verify`, `after_diagnose`, `after_critic`, `after_security`) — keep it
  that way, it is what makes the pipeline's decisions testable without a graph.
  [github/answer_graph.py](agent_loop/github/answer_graph.py) is the much smaller
  question-answering path.
- **The nodes that cost money** — `implement` and `critic` run on
  `DEEPSEEK_MODEL_STRONG`, everything else on `DEEPSEEK_MODEL`
  ([llm.py](agent_loop/llm.py)). A fix cycle re-runs the entire verify suite, so
  work that is weak in those two nodes is not cheaper.
- **The task lifecycle** — [github/poller.py](agent_loop/github/poller.py)
  enqueues, [queue.py](agent_loop/queue.py) dispatches one at a time,
  [github/source.py](agent_loop/github/source.py) runs a task, and
  [github/merge_watcher.py](agent_loop/github/merge_watcher.py) owns the PR
  afterwards. A task is **not** done when the PR opens — it is `awaiting_review`
  until the PR merges ([db.py](agent_loop/db.py)).
- **The boundaries.** Two of them, and they are the security model:
  - The **path-gate** ([tools.py](agent_loop/tools.py)) — every file tool
    resolves its path inside the checkout and refuses to escape it.
  - The **container** ([container.py](agent_loop/container.py)) — when a source
    declares one, the agent's shell work runs there, with the checkout
    bind-mounted; file tools still act on the host copy. Without it, the
    path-gate is the only boundary.
  Everything needing `gh` credentials (prepare, the intake gate, publish) runs on
  the **host**, never in the container. [exec.py](agent_loop/exec.py) uses argv
  arrays and no shell, because issue text reaches these arguments.
- **Trust** — issue text and PR comments are attacker-controlled input. The
  intake gate judges the *request* before any clone; the `security` node judges
  the *diff*. Neither is decorative: they are what makes an unattended runner
  defensible. When you add a place where untrusted text reaches a prompt, say so
  in the prompt.
- **Config** — global secrets and agent settings come from `.env`
  ([config.py](agent_loop/config.py)); the repos it works on are declared in
  `agent-loop.config.yaml`, which references secrets as `${VAR}` and never holds
  them. Add a source field to `GithubSourceConfig` **and** to
  `agent-loop.config.example.yaml` — the example file is the documentation.

Trust GitHub's own Checks API over the agent's account of itself. The PR watcher
already works this way, and `summarize` is told the verify outcome for the same
reason: an agent asked how its change went will tell you it went well.

All committed and user-facing text is in **English** — code, comments, log lines,
prompts, and anything the agent posts to GitHub.

## Before you finish a change — the checklist

1. **Write/update tests.** They live in [tests/](tests/) and are fully mocked —
   no network, no LLM, no Docker, and the whole suite runs in under a second.
   Keep it that way: fake the model with a stub returning canned text, fake the
   shell with a stub returning canned exit codes. What is worth covering is the
   decision (which node runs next, whether a PR is adopted, whether a command was
   skipped), not the plumbing around it.
2. **Run what CI runs** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) —
   all four, and `pyright` must be at zero, not merely lower:
   ```bash
   .venv/bin/python -m ruff check .
   .venv/bin/python -m ruff format --check .
   .venv/bin/pyright
   .venv/bin/python -m pytest -q
   ```
   In Claude Code the `checklist` agent runs this plus the config and hygiene
   checks, and reports a compact summary.
3. **If the diff touches the boundaries** — `tools.py`, `exec.py`,
   `container.py`, or anything that builds a prompt from issue/PR text — re-read
   the *Boundaries* and *Trust* notes above plus [SECURITY.md](SECURITY.md), and
   extend [tests/test_path_gate.py](tests/test_path_gate.py) or
   [tests/test_agent_env.py](tests/test_agent_env.py): passing the existing cases
   is not evidence about the path you added. In Claude Code the
   `sandbox-boundary` agent audits a diff for exactly this.
4. **If you added or renamed a config field**, update
   `agent-loop.config.example.yaml` and `.env.example` in the same PR.
5. **If you changed a dependency**, regenerate `constraints.txt` in the same PR —
   that file, not `pyproject.toml`, is what the machine installs.

## Branching & PRs

- **Start every task from a fresh `main`**: `git checkout main`, `git pull origin
  main`, then `git checkout -b <name>`. Branching off a stale base is what
  produces the conflicts you fight later.
- `main` is protected — no direct pushes. `scripts/install-hooks.sh` installs a
  pre-push hook that says so before the network round-trip. Open a PR; CI must be
  green before merge.
- **Before opening a PR, sync with `main` and confirm it is conflict-free**:
  `git fetch origin`, `git rebase origin/main`, and check `gh pr view <n> --json
  mergeable,mergeStateStatus`.
- Write a clear PR title and description — see [CONTRIBUTORS.md](CONTRIBUTORS.md).

## Versioning & releases

Semantic Versioning: **PATCH** = fixes, **MINOR** = new capability in the
pipeline or config, **MAJOR** = a change that breaks existing `.env` /
`agent-loop.config.yaml` files.

**Merging does not deploy. Bumping the version does.** The release watcher
([scripts/release-watch.sh](scripts/release-watch.sh)) fetches `main` every five
minutes and exits unless `version` in `pyproject.toml` differs from what is
deployed. So `main` can carry ordinary work — docs, refactors, tests — without
rolling anything out to this machine.

That makes the version bump a deliberate act, and the one thing this repo does
opposite to most: **you do edit `version` in `pyproject.toml` by hand**, in the
PR whose merge should ship. Bump it when the change is worth deploying; leave it
alone when it is not. If you are unsure, leave it — an unshipped merge costs
nothing, and the next bump carries it along.

What a bump sets in motion, unattended: the watcher waits up to 15 minutes for
the agent to go idle (`queue.stop()` cancels the in-flight task rather than
draining it), resets the prod checkout to `main`, reinstalls, runs `pytest`,
restarts, and checks the process is still up 30 seconds later. Anything red rolls
back. A red `main` is therefore not a nuisance — it is a bad release rolling out
to a machine that will retry it.

```bash
scripts/agent-loopctl status     # version, state, running tasks
scripts/agent-loopctl logs | watch-logs | restart | deploy
```

The prod checkout (`~/agent-loop-prod`) is separate from your dev clone, because
the watcher runs `git reset --hard` in it.

## Git artifacts

Commit messages describe **why**, then what. The history here is written to be
read later — a bare `fix: bug` is worth less than the two sentences saying what
was actually wrong.

Do not confuse this repo's attribution with the `gitAttribution` source option:
that flag controls what the agent writes in *other people's* repos. Here, follow
the existing history.
