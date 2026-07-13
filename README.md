# agent-loop

An autonomous GitHub issue **auto-fix** runner on your **local machine**, built
on **Python + LangGraph**. It polls your repos, and for each labelled issue it
screens the request for safety, investigates, plans, implements a fix, writes
e2e tests, verifies the build, runs a security review of the diff, and opens a
PR. From there it watches the PR: it re-runs flaky/cancelled CI once, answers or
reworks the PR from human review comments, and — optionally — auto-merges once
CI is green. A task is only **closed when its PR is merged**. The agent's work
runs in an isolated **Docker** container (opt-in).

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
intake gate ─▶ investigate ─▶ plan ─▶ implement ─┬─(no changes)──────────────▶ publish
 (LLM, host)    (agent,RO)    (LLM)   (agent)     └─(changes)─▶ write_tests ─▶ verify
      │ reject                                                   (agent)     (deterministic:
      ▼                                                                       lint+build+e2e)
   decline    ┌──── implement (≤3 cycles) ◀── red / revise / sec-fail ──────────┤
    (host)    ▼                                                                 │ green
           (retry)                    critic ─approve─▶ security ─pass─▶ summarize ─▶ publish
                                      (LLM review)     (scanners+LLM)
```

- **intake gate** — before any clone: an LLM judges whether the request is safe
  to act on at all, from the issue text + the author's trust level
  (`authorAssociation`). A malicious/unsafe ask (inject a script, exfiltrate
  secrets, weaken auth…) is **declined** and never worked on. External authors
  are judged more strictly.
- **investigate / implement / write_tests** — ReAct agents (`create_react_agent`)
  with the Read/Write/Edit/Bash/Grep/Glob tools.
- **plan / critic / summarize** — single, focused LLM calls.
- **verify** — runs the configured commands (lint + build + e2e); a red result
  loops back to `implement` with the failure log (bounded to 3 cycles, then it
  escalates and opens the PR flagged for human review).
- **security** — after the critic approves: runs any configured security
  scanners (`securityCommands`, e.g. `npm audit`/`semgrep`) plus an LLM security
  review of the diff. A finding loops back to `implement` within the same cycle
  budget. This is the diff-level check ("did we introduce a vulnerability?"),
  distinct from the intake gate ("should we do this at all?").
- **prepare** (clone/branch), the **intake gate**, and **publish**
  (commit/push/PR/comment) run on the **host** (they need `gh` credentials); only
  the agent's shell work runs in the container.

On **rework** (a review comment asked for changes, or a fix must be pushed to an
existing PR) the graph re-enters straight at `implement` on the PR branch and
pushes to it instead of opening a new PR.

Layers: [config.py](agent_loop/config.py) · [db.py](agent_loop/db.py) ·
[queue.py](agent_loop/queue.py) · [container.py](agent_loop/container.py) ·
[tools.py](agent_loop/tools.py) · [github/](agent_loop/github/) ·
[main.py](agent_loop/main.py)

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
cp agent-loop.config.example.yaml agent-loop.config.yaml   # declare your repos
.venv/bin/agent-loop        # or: .venv/bin/python -m agent_loop.main
```

Secrets and agent settings live in `.env`; the list of GitHub repos lives in
`agent-loop.config.yaml` (reference secrets as `${VAR}`). Each entry under
`sources` is a separate instance with a unique `id`.

## Issue routing

Issues are routed **by the instance's labels** (priority bug > feature >
question):

| Label | What it does |
|---|---|
| `bug` | intake gate → investigate → fix → regression test → security → PR |
| `enhancement` | intake gate → implement → e2e test → security → PR (conservative) |
| `question` | reply with an issue comment, **no code, no PR** (thread stays open) |

## PR lifecycle

Opening a PR does **not** close the task — it moves to `awaiting_review` and is
closed (`done`) only when the PR merges. The
[PR watcher](agent_loop/github/merge_watcher.py) manages each open PR (it runs
even when auto-merge is off), deciding from GitHub's own Checks API — never the
agent's word:

- **CI red** → it re-runs the failed jobs **once** (this alone clears most
  `cancelled`/flaky failures). If CI is still red after that single re-run, the
  PR is handed off to a human.
- **Human review comments** → all new comments are classified as a **question**
  (answered on the PR, grounded in the branch code) or a **change request** (the
  task goes back into **rework** — the agent amends the same PR branch and
  pushes). Inline comments keep their **file/line anchor** so the fix lands where
  the comment points. CI need not be green for this.
- **CI green** → if `autoMerge: true`, the diff passes the **blast-radius
  policy** (`policy.allowedGlobs` + `policy.maxChangedLines`), **and** (unless
  `requireApproval: false`) the PR is **approved** — the `approveLabel` label is
  present (GitHub blocks approving your own PR, so a label is how the author
  signs off) or a different reviewer approved — it squash-merges. Otherwise it
  keeps waiting / leaves the PR for review.

**The target repo must have a PR CI workflow that runs the same lint/build/e2e**
— the local verify run is a fast pre-check, the CI run is the authoritative gate
the PR watcher trusts.

## Tests

Fast, fully mocked unit tests — no network, LLM, or Docker. They cover the PR
watcher's decision tree (CI re-run, comment→rework/answer, label-gated merge),
the CI roll-up, the task lifecycle/dedup queries, the queue guard, and the
intake gate.

```bash
.venv/bin/pip install -e ".[dev]"   # pytest + pytest-asyncio
.venv/bin/python -m pytest
```

## The model

DeepSeek via LangChain `init_chat_model` ([llm.py](agent_loop/llm.py)). The API is
only the "brain" — the tool-use loop is LangGraph's and the tool executor is ours
([tools.py](agent_loop/tools.py)), so a different provider is a one-line change.

## Keeping it alive

The process runs as long as the machine is on. Wrap it in `pm2`/launchd to
survive sleep/crashes; tasks interrupted mid-run are re-queued on boot (an
interruption is not a failure), while `awaiting_review` PRs are picked back up by
the watcher.
