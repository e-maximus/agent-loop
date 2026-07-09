# open-claw

An autonomous GitHub issue **auto-fix** runner on your **local machine**, built
on **Python + LangGraph**. It polls your repos, and for each labelled issue it
investigates, plans, implements a fix, writes e2e tests, verifies the build, and
opens a PR — optionally auto-merging once CI is green. The agent's work runs in
an isolated **Docker** container (opt-in).

> Rewritten from the original TypeScript/Telegram version. Telegram task ingress
> has been removed; the only source type is GitHub.

## Architecture

Everything runs as a single asyncio process:

```
GitHub poller ──enqueue──▶ SQLite queue ──▶ GithubSource.run(task) ──▶ LangGraph
   ▲                                                                       │
   └──────────────── issue comments / PR / auto-merge ◀────────────────────┘
```

The auto-fix pipeline is a LangGraph `StateGraph`. Each step is at the right
granularity — an autonomous ReAct agent where exploration is needed, a single
LLM call where the input is already gathered, and plain deterministic code where
there is no judgement to make:

```
investigate ─▶ plan ─▶ implement ─┬─(no changes)─────────────────▶ publish
 (agent,RO)   (LLM)   (agent)      └─(changes)─▶ write_tests ─▶ verify
                                                   (agent)     (deterministic:
       ┌──────── implement (≤3 cycles) ◀── red / revise ──────── lint+build+e2e)
       ▼                                                            │ green
    (retry)                                        critic ─approve──┴─▶ summarize ─▶ publish
                                                   (LLM review)
```

- **investigate / implement / write_tests** — ReAct agents (`create_react_agent`)
  with the Read/Write/Edit/Bash/Grep/Glob tools.
- **plan / critic / summarize** — single, focused LLM calls.
- **verify** — runs the configured commands (lint + build + e2e); a red result
  loops back to `implement` with the failure log (bounded to 3 cycles, then it
  escalates and opens the PR flagged for human review).
- **prepare** (clone/branch) and **publish** (commit/push/PR/comment) run on the
  **host** (they need `gh` credentials); only the agent's shell work runs in the
  container.

Layers: [config.py](open_claw/config.py) · [db.py](open_claw/db.py) ·
[queue.py](open_claw/queue.py) · [container.py](open_claw/container.py) ·
[tools.py](open_claw/tools.py) · [github/](open_claw/github/) ·
[main.py](open_claw/main.py)

## Execution: host or Docker

If a source declares a `container` block, the agent's Bash/build/test commands
run inside an ephemeral per-task container with the checkout bind-mounted at
`/workspace`; file tools still operate on the host copy. Omit the block to run on
the host (the path-gate is then the only boundary). Use a Playwright image so the
e2e browsers are present.

## Setup

Requires Python 3.12+, `gh` (authenticated, with push access), `git`, and —
for container mode — Docker.

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env                                     # DEEPSEEK_API_KEY, AGENT_*
cp open-claw.config.example.yaml open-claw.config.yaml   # declare your repos
.venv/bin/open-claw        # or: .venv/bin/python -m open_claw.main
```

Secrets and agent settings live in `.env`; the list of GitHub repos lives in
`open-claw.config.yaml` (reference secrets as `${VAR}`). Each entry under
`sources` is a separate instance with a unique `id`.

## Issue routing

Issues are routed **by the instance's labels** (priority bug > feature >
question):

| Label | What it does |
|---|---|
| `bug` | investigate → fix → write regression test → PR |
| `enhancement` | implement the feature → write e2e test → PR (conservative) |
| `question` | reply with an issue comment, **no code, no PR** (thread stays open) |

For bug/feature, if `autoMerge: true` the [merge watcher](open_claw/github/merge_watcher.py)
waits for **green CI** (decided from the GitHub Checks API, not the agent's word)
and, if the diff passes the **blast-radius policy** (`policy.allowedGlobs` +
`policy.maxChangedLines`), squash-merges. Otherwise it comments and leaves the PR
for review.

**The target repo must have a PR CI workflow that runs the same lint/build/e2e**
— the local verify run is a fast pre-check, the CI run is the authoritative gate
the merge-watcher trusts.

## The model

DeepSeek via LangChain `init_chat_model` ([llm.py](open_claw/llm.py)). The API is
only the "brain" — the tool-use loop is LangGraph's and the tool executor is ours
([tools.py](open_claw/tools.py)), so a different provider is a one-line change.

## Keeping it alive

The process runs as long as the machine is on. Wrap it in `pm2`/launchd to
survive sleep/crashes; interrupted tasks are marked failed on boot and re-picked.
