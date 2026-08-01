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
- Decide whether this PR should deploy. Merging alone does not: the release
  watcher ships when `version` in `pyproject.toml` changes. Bump it in this PR
  if the change is worth rolling out to the machine, leave it if not.

## PR description

Every PR description **must** contain these three sections:

- **What & Why** — the problem and the chosen solution, in a short paragraph,
  before the diff.
- **Changes** — a map of the diff: what changed and where.
- **Tests** — what you added or updated, and confirmation that the suite passes.

Add any of these **only when they carry weight** — skip the rest:

- Deploys / version bump (say so explicitly when you bumped it, and why)
- How to test (manual steps, especially for anything touching a live repo)
- Breaking changes to `.env` or `agent-loop.config.yaml`, and the migration
- Related issues (`Closes #N`)
- Risks & trade-offs
