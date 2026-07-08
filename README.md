# open-claw

An autonomous Claude-agent runner on your **local machine**. You send tasks via
Telegram and the agent runs them in your environment. It also handles GitHub
issue auto-fix, and has cron-based site parsing planned (next steps).

## Architecture

Everything runs as a single Node process on the local machine:

```
Sources ──ingress──▶ Orchestrator ──▶ Queue (SQLite) ──▶ Source.run() (Agent SDK)
   ▲                                                            │
   └──────────── status / approvals / result ◀──────────────────┘
```

A **source** is a self-contained integration (Telegram, GitHub, …). Each source
owns its ingress (a bot or a poller), enqueues tasks, and knows how to run a task
that originated from it. Multiple instances of the same type can run at once
(e.g. several GitHub repos), each identified by a unique `id`.

- **Long polling / API polling** — works behind NAT, no public IP needed.
- **Telegram user-id allowlist** — commands are accepted only from you.
- **Permission-in-the-loop** — dangerous actions (Bash, file writes) are
  confirmed via inline buttons in Telegram. Paths outside the workspace are
  auto-denied.
- **SQLite** — task state survives restarts; interrupted tasks are marked
  `failed` on boot.

Layers: [config.ts](src/config.ts) · [db.ts](src/db.ts) · [queue.ts](src/queue.ts) ·
[sources/](src/sources/) · [index.ts](src/index.ts)

## Configuration

Secrets and the agent backend live in `.env`. The list of sources (Telegram
bots, GitHub repos) lives in `open-claw.config.yaml`. Secrets are **not** stored
in the YAML — reference them as `${VAR}` and keep the values in `.env`.

1. Create a bot with [@BotFather](https://t.me/BotFather) → you get a `TELEGRAM_BOT_TOKEN`.
2. Find your numeric id via [@userinfobot](https://t.me/userinfobot).
3. Copy the templates and fill them in:

```bash
cp .env.example .env                                     # TELEGRAM_BOT_TOKEN, *_API_KEY, AGENT_*
cp open-claw.config.example.yaml open-claw.config.yaml   # declare your sources
```

Each entry under `sources` is a separate instance with its own unique `id`;
several GitHub repos = several `type: github` instances. Override the config
path with the `OPEN_CLAW_CONFIG` env var.

4. Run:

```bash
npm install
npm start        # or npm run dev — with auto-reload
```

Send `/start` to the bot, then send a task as text.

## Bot commands

- any text — enqueue a task
- `/tasks` — active tasks
- `/status <id>` — status/result
- `/cancel <id>` — cancel a queued task

## GitHub automation

**Polling, not webhooks** — the machine sits behind NAT, so the
[poller](src/sources/github/poller.ts) asks GitHub (`gh issue list`) every
`pollIntervalSec` (from the instance config). Same principle as Telegram long
polling.

Issues are routed **by the instance's labels** (`labels.*`, priority
bug > feature > question):

| Label | What it does |
|---|---|
| `bug` | reproduce → fix → PR |
| `enhancement` | implement the feature → PR (conservative prompt) |
| `question` | reply with an issue comment, **no code, no PR** (thread stays open) |

For bug/feature: clone the repo → branch `issue-N` → the agent fixes it and runs
`lint`+`build` locally → commit → push → open a PR. Then, depending on `autoMerge`:

- `false` — the PR is left for review;
- `true` — the [merge-watcher](src/sources/github/merge-watcher.ts) waits for
  **green CI** (decided from the GitHub Checks API, not from the agent's word)
  and, if the diff passes the **blast-radius policy** (`policy.allowedGlobs` +
  `policy.maxChangedLines`), squash-merges. Otherwise it comments on the issue
  and leaves the PR for review.

A `question` thread stays open: the label is kept and the poller answers again
whenever a human posts a new comment after the last reply (tracked by an
`answeredThrough` cursor).

Requirements: `gh` installed and authenticated (`gh auth login`) with push
access. The target repo must have a **PR CI workflow** (see
[templates/node-pr-ci.yml](templates/node-pr-ci.yml)) — otherwise auto-merge
has nothing to gate on.

Layers: [gh.ts](src/sources/github/gh.ts) · [poller.ts](src/sources/github/poller.ts) ·
[autofix.ts](src/sources/github/autofix.ts) · [answer.ts](src/sources/github/answer.ts) ·
[policy.ts](src/sources/github/policy.ts) · [merge-watcher.ts](src/sources/github/merge-watcher.ts)

## Roadmap

- [x] Steps 1–3: skeleton, Telegram ingress, local worker with approvals
- [x] Step 4: GitHub auto-fix loop (issue → fix → PR)
- [x] Step 5: auto-merge on green CI + blast-radius policy
- [x] Step 6: configurable sources (YAML) + pluggable source folder
- [ ] Step 7: cron-based site parsing + catch-up on boot
- [ ] Step 8: observability (token cost)

## Keeping it alive

The orchestrator runs as long as the machine is on. To survive sleep/crashes,
wrap it in `pm2` or launchd; Telegram messages are not lost (they are retained
for ~24h and picked up on the next start).
