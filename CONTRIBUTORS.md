# Contributing

Short guide for opening a pull request in this repo. For the full working guide
(project shape, boundaries, testing conventions, release mechanics) see
[AGENTS.md](AGENTS.md).

## Branches

- Branch off `main`; never commit directly to `main` — it is protected, and the
  pre-push hook from `scripts/install-hooks.sh` blocks it locally too.
- Name the branch after the change, in kebab-case, optionally with a type
  prefix: `feat/strong-model-for-critic`, `fix/verify-skips-baseline-red`,
  `chore/prune-dead-env-vars`.

## Before you open a PR

- Add or update tests for any behaviour change — they are mocked and fast, so
  "too slow to test" is not a reason here.
- Run what CI runs, and make sure it passes:
  ```bash
  .venv/bin/python -m pytest -q
  ```
- **Do not touch `version` in `pyproject.toml`** — the merge bumps it, and CI
  fails a PR that edits the line. Set the *level* with a label instead:
  `release:minor` or `release:major`, or nothing at all for a patch. The
  criteria are in [AGENTS.md](AGENTS.md#versioning--releases). Every merge
  releases, patch included, so merging a PR ships it.

## PR description

Every PR description **must** contain these three sections:

- **What & Why** — the problem and the chosen solution, in a short paragraph,
  before the diff.
- **Changes** — a map of the diff: what changed and where.
- **Tests** — what you added or updated, and confirmation that the suite passes.

Add any of these **only when they carry weight** — skip the rest:

- Deploys / release level (say so when you added `release:minor` or
  `release:major`, and why)
- How to test (manual steps, especially for anything touching a live repo)
- Breaking changes to `.env` or `agent-loop.config.yaml`, and the migration
- Related issues (`Closes #N`)
- Risks & trade-offs
