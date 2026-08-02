# Backlog — ideas worth building

Local, untracked (`.gitignore`), one entry per idea. **This is a parking lot, not
a commitment**: an entry means "someone thought this was worth doing", not "this
is scheduled".

**Rules**

- **Delete an entry once it ships.** The PR that implements it removes it in the
  same change — a shipped idea left here reads as an open one next session.
- Trim, don't archive. Git history is not available for this file; that is the
  point. If an idea is worth remembering after rejection, it belongs in an issue
  or in AGENTS.md, not here.
- Sorted by `Priority`, then by whatever order they landed. Add new entries at
  the end of their priority block.
- Keep an entry short enough to read in ten seconds. If it needs a design doc,
  the entry links to it.

**Fields**

| Field | Values |
|---|---|
| `Type` | `pipeline` (what the agent does) · `quality` (how we know it works) · `safety` (boundaries, trust) · `ops` (cost, deploy, observability) · `dx` (working in this repo) |
| `Priority` | `high` — pays for itself soon, or blocks judging other work · `medium` — clear win, no urgency · `low` — nice to have |
| `Effort` | `S` ≈ an afternoon · `M` ≈ a day · `L` ≈ multi-day or needs a design first |
| `Added` | ISO date, absolute — relative dates rot |

---

## evaluation-set

`Type: quality · Priority: high · Effort: L · Added: 2026-08-01`

**Idea.** A fixed set of 15–50 real tasks (repo + commit SHA + issue text + a
test that must go green), and a runner that drives the autofix pipeline over
them and reports how many were solved.

**Why.** This system's behaviour lives in prompts. Right now a change to
`implement_prompt` cannot be judged at all — [tests/](tests/) covers routing,
verdict parsing and plumbing, none of which move when a prompt does. Every other
entry in this file is a guess until there is a number to compare.

**Where.** New top-level runner; reuses `build_autofix_graph` with publish
stubbed out.

**Note.** Too expensive per commit — the natural trigger is the version bump,
which is already the deliberate act that ships to the machine.

**Done when.** `pytest`-independent command prints solved/total, and two prompt
variants can be compared on it.

---

## reproduce-before-implement

`Type: pipeline · Priority: high · Effort: M · Added: 2026-08-01`

**Idea.** For `kind == "bug"`, a `reproduce` node between `plan` and
`implement`: write a test that reproduces the issue, run it, require it to FAIL.
Keep it as the oracle for the fix. Features skip the node.

**Why.** Today `write_tests` runs *after* `implement`, so the model writes a test
against the diff it just produced — it is near-guaranteed green and proves close
to nothing. A test that failed before the fix and passes after is the only cheap
evidence the bug was actually the bug.

**Where.** [autofix_graph.py](agent_loop/github/autofix_graph.py) — new node,
`entry`/`after_implement` unchanged, state gains `repro_test`. The wording in
`write_tests_prompt` already describes the right artefact ("fails without your
fix, passes with it"); it just runs at the wrong moment.

**Done when.** A bug task that cannot be reproduced is visible as such instead of
producing a confidently green PR.

---

## token-and-cost-accounting

`Type: ops · Priority: high · Effort: S · Added: 2026-08-01`

**Idea.** Record tokens and cost per node per task in SQLite; surface the total
in `agent-loopctl status` and in the task log.

**Why.** Nothing currently measures spend. `AGENT_MAX_TURNS` is called a cost
guard but bounds *steps*, and a step holding a 12k-char diff is not a step that
ran one Grep. Without per-node numbers, "should we sample N patches" and "is
`critic` worth the strong model" are opinions.

**Where.** Every model call already funnels through `_ask` / `_run_agent` in the
graph builders, plus the two calls in
[source.py](agent_loop/github/source.py) that sit outside the graph. Read
`usage_metadata` off the response; one new table in [db.py](agent_loop/db.py).

**Done when.** A finished task reports what it cost, broken down by node.

---

## sample-n-patches

`Type: pipeline · Priority: medium · Effort: L · Added: 2026-08-01`

**Idea.** When `critic` rejects the first attempt, generate an alternative patch
from a clean tree instead of spending another cycle revising the same one.
Choose between candidates by verify outcome.

**Why.** Sampling several candidates and selecting on tests is the most reliable
known quality lever, and it works on top of any model. All feedback currently
folds back into the *same* diff — if the first direction was wrong, three cycles
polish a wrong answer. `after_critic` gives up into `security` rather than
restarting.

**Cost.** Multiplies the expensive node. Restricting it to "first attempt was
rejected" keeps the common path at one sample.

**Depends on.** [evaluation-set] to tell whether it actually pays, and
[token-and-cost-accounting] to tell what it costs.

**Where.** `after_critic` + a git stash/branch dance around `implement`.

---

## repo-map-for-investigate

`Type: pipeline · Priority: medium · Effort: M · Added: 2026-08-01`

**Idea.** Build a compact map of the checkout (files, key symbols, ranked by
reference count) and put it in `investigate`'s first user message.

**Why.** `investigate` reconstructs the shape of an unfamiliar repo with Grep and
Read, spending turns out of a 100-turn budget on what a static pass answers
deterministically. Target repos with a good `AGENTS.md` partly get this for free;
the rest do not.

**Where.** New module; consumed in `investigate` in
[autofix_graph.py](agent_loop/github/autofix_graph.py). tree-sitter is the usual
tool — no embeddings, no index to invalidate.

---

## selective-verify-during-cycles

`Type: ops · Priority: medium · Effort: M · Added: 2026-08-01`

**Idea.** Run only the tests related to the changed files inside the fix cycle;
run the full `verify_commands` once before `summarize`.

**Why.** Every red cycle re-runs the entire suite, and a fix cycle is already the
expensive part. The authoritative full run stays authoritative — it just happens
once instead of three times.

**Risk.** "Related tests" is per-ecosystem guesswork. Needs a config field
(`quickVerifyCommands`?) rather than cleverness, which means
`agent-loop.config.example.yaml` too.

---

## container-required-for-untrusted-sources

`Type: safety · Priority: medium · Effort: S · Added: 2026-08-01`

**Idea.** A source that accepts issues from non-collaborators must declare a
`container`; refuse to start it otherwise (config validation, not a runtime
check).

**Why.** The container is opt-in today, so a misconfigured source leaves the
path-gate as the only boundary while attacker-controlled issue text still steers
what `Bash` runs. The intake gate judges intent, not blast radius.

**Where.** [config.py](agent_loop/config.py) validation +
`agent-loop.config.example.yaml`. Note this does not change the host/container
split for file tools — `Write`/`Edit` stay on the host copy by design.

---

## resume-interrupted-tasks

`Type: ops · Priority: low · Effort: L · Added: 2026-08-01`

**Idea.** Persist graph state per node so an interrupted task resumes instead of
restarting from `baseline`.

**Why.** `queue.stop()` cancels the in-flight task rather than draining it, so
every deploy during a run discards `findings`, `plan` and any completed fix
cycles — the strong-model work included.

**Against.** State is tied to a specific checkout and container; resuming into
`implement` is only meaningful if that tree still exists with its edits. And a
LangGraph checkpointer would be a second persistence layer beside
[db.py](agent_loop/db.py), with a different lifecycle — the kind of pair that
drifts apart.

**Cheaper alternative.** Have the release watcher wait for the *current node*
rather than the whole task, or refuse to cancel past `implement`.

---

## issue-deduplication

`Type: pipeline · Priority: low · Effort: S · Added: 2026-08-01`

**Idea.** Before enqueueing, check whether a near-duplicate issue is already in
flight or was recently closed by us.

**Why.** The poller only knows "was *this* issue taken"
([poller.py](agent_loop/github/poller.py)). Two issues describing one bug get two
full pipelines and two competing PRs.

---

## release-run-concurrency-drops-bumps

`Type: ops · Priority: high · Effort: S · Added: 2026-08-02`

**Idea.** Make `auto-release.yml` survive several merges landing close together
— either by computing the bump from every PR merged since the last release
rather than only the one that triggered the run, or by retrying the run instead
of letting it be superseded.

**Why.** The workflow uses `concurrency: {group: auto-release,
cancel-in-progress: false}`, which serializes runs but keeps only *one* pending
run per group: a third merge cancels the second's queued run. Observed on
2026-08-02 — merging #12, #4, #3, #5 in a row produced two `failure` runs and
two `cancelled` ones. A cancelled run is a merge that never released, and since
the level comes from that PR's labels, a `release:minor` swallowed by a later
patch is a behaviour change that reaches the machine labelled as a fix.

**Where.** [.github/workflows/auto-release.yml](.github/workflows/auto-release.yml)
and `level_from_labels` in [scripts/bump_version.py](scripts/bump_version.py) —
which already takes a comma-separated label string, so feeding it the union of
labels from every PR merged since the last tag is a small change.

**Blocked on.** `RELEASE_TOKEN` — until releases can push to `main` at all, this
is invisible.

**Done when.** Four merges in a minute produce one release whose level is the
largest any of them asked for.
