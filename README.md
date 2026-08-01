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
intake gate ─▶ baseline ─▶ investigate ─▶ plan ─▶ implement ─┬─(no changes)──────────▶ publish
 (LLM, host)  (determin.)   (agent,RO)    (LLM)   (agent)     └─(changes)─▶ write_tests ─▶ verify
      │ reject                                                               (agent)     (determin.:
      ▼                                                                                  lint+build+e2e)
   decline    ┌──── implement (≤3 cycles) ◀─ CODE / revise / sec-fail ─ diagnose ◀── red ──┤
    (host)    ▼                                    │                      (LLM)            │ green
           (retry)                                 └─ ENVIRONMENT ─▶ summarize              │
                                    critic ─approve─▶ security ─pass─▶ summarize ─▶ publish ┘
                                    (LLM review)     (scanners+LLM)
```

- **intake gate** — before any clone: an LLM judges whether the request is safe
  to act on at all, from the issue text + the author's trust level
  (`authorAssociation`). A malicious/unsafe ask (inject a script, exfiltrate
  secrets, weaken auth…) is **declined** and never worked on. External authors
  are judged more strictly.
- **investigate / implement / write_tests** — tool-calling agents
  (`langchain.agents.create_agent`) with the Read/Write/Edit/Bash/Grep/Glob tools.
- **plan / critic / summarize** — single, focused LLM calls.
- **baseline** — runs the same commands once on the untouched base branch,
  before anything is written. A suite that needs a service or a secret the
  container does not have fails identically whatever the diff does, so whatever
  is red here is skipped when verifying the change, named in the PR body, and
  flags the PR for human review. Costs one extra pass over the suite per task —
  set `baselineVerify: false` where that is too slow.
- **verify** — runs the configured commands (lint + build + e2e) minus the ones
  baseline found already red; a red result goes to `diagnose`. Repeated lines in
  a failure log are collapsed with a count, so a dev server logging the same
  warning on every page load cannot evict the assertion that actually failed.
- **diagnose** — one cheap LLM call on the failure log: `CODE` (the diff is
  wrong) loops back to `implement` with the log, bounded to 3 cycles;
  `ENVIRONMENT` (missing service, unreachable host, absent key — nothing a diff
  repairs) goes straight to `summarize`, which states what the environment is
  missing; `FLAKY` re-runs verify once. An unreadable verdict counts as `CODE`.
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

Bug/feature issues are one-shot: enqueued once, never again. The task DB answers
"already taken?" — but it is local and can be reset or lost, so an issue it has
never seen is checked against GitHub too (an open PR on `issue-<n>`, or a comment
of ours on the thread). Without that, a wiped DB re-runs finished work and then
collides with its own open PR at `gh pr create`; when a PR for the branch does
exist, `publish` adopts and updates it instead of failing the run.

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

Changing this repo? [AGENTS.md](AGENTS.md) is the working guide — project shape,
the two boundaries that keep an unattended runner safe, and why a merge does not
deploy but a version bump does. [CONTRIBUTORS.md](CONTRIBUTORS.md) covers PRs.

## The model

DeepSeek via LangChain `init_chat_model` ([llm.py](agent_loop/llm.py)). The API is
only the "brain" — the tool-use loop is LangGraph's and the tool executor is ours
([tools.py](agent_loop/tools.py)), so a different provider is a one-line change.

Two models, not one. `implement` and `critic` run on `DEEPSEEK_MODEL_STRONG`
(`deepseek-v4-pro`); every other node — investigate, plan, diagnose, security,
summarize — runs on `DEEPSEEK_MODEL` (`deepseek-v4-flash`). Those two nodes write
the diff and decide whether it stands, so weak work there is not cheaper: it comes
back as another fix cycle, and each cycle re-runs the whole verify suite. Set both
variables to the same value to go back to a single model.

## Running as a service

Deployment target is this machine — no cloud. Two `launchd` agents: the runner
itself, and a watcher that keeps it on the latest release.

```bash
scripts/install-service.sh            # → ~/agent-loop-prod, both LaunchAgents
scripts/agent-loopctl status          # version, state, running tasks
scripts/agent-loopctl logs | watch-logs | restart | deploy
```

The **prod checkout is separate from your dev clone** (`~/agent-loop-prod`): the
watcher runs `git reset --hard` in it. `.env` and `agent-loop.config.yaml` are
gitignored, so they survive every deploy.

Two environment facts the plists exist to handle: `launchd` gives a process no
shell profile, so `PATH` is set explicitly (`gh` from Homebrew, `docker`,
`node`/`npm`) — without it the source disables itself or every verify goes red;
and all of `.env`, the SQLite DB and the log dir resolve relative to the working
directory, so `WorkingDirectory` is mandatory. These are *LaunchAgents*, not
daemons: they run in your logged-in session, where `gh`'s keychain token is
reachable.

Sleep pauses everything (`sudo pmset -c sleep 0` to prevent it). Interrupted
tasks are re-queued on boot — an interruption is not a failure — and
`awaiting_review` PRs are picked back up by the PR watcher.

## Releases

`main` is protected: no direct pushes, every change goes through a PR with green
CI (`scripts/install-hooks.sh` installs a pre-push hook that says so before the
round-trip). Because `main` also carries ordinary work, **a release is a version
bump** — [release-watch.sh](scripts/release-watch.sh) deploys when `version` in
`pyproject.toml` differs from what is deployed, not on every commit.

Pull-based by design: the machine is behind NAT and the repo is public, so a
self-hosted Actions runner would be both unreachable and a way for any PR author
to run code here. Every 5 minutes the watcher:

1. fetches `main`; exits unless the version changed;
2. waits (≤15 min) for the agent to go **idle** — `queue.stop()` cancels the
   in-flight task rather than draining it, and re-running it from scratch wastes
   its LLM spend;
3. resets to `main`, reinstalls, runs `pytest`;
4. restarts and checks the process is still up 30 s later.

Anything red rolls back to the previous commit and notifies. The deployed
commit is recorded in `data/deployed.sha`, which is also what stamps traces.

## Tracing

With `LANGSMITH_*` set (see [.env.example](.env.example)), every run is tagged
with the build that produced it — `version`, `commit`, plus `source_id`, `repo`,
`issue`, `task_id`, `stage` ([version.py](agent_loop/version.py)). Metadata is
inherited by child runs, so tagging the graph invocation covers every node, tool
call and LLM call inside it; the calls that happen *outside* a graph (the intake
gate, PR-comment classification) carry it themselves. In LangSmith this makes a
trace findable by issue, and success rates comparable across releases.
