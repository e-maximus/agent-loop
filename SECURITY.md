# Security

agent-loop runs unattended on one machine, acts on **attacker-controlled input**
(GitHub issue bodies and PR comments are written by anyone who can open an
issue), and holds credentials that can push to repositories. This file states
what it defends, how, and what it does not defend — so the boundaries are
checkable rather than assumed.

## Reporting a vulnerability

Open a [security advisory](https://github.com/e-maximus/agent-loop/security/advisories/new)
rather than a public issue. Include what an attacker controls, what they reach,
and a reproduction if you have one. There is no bounty; this is a personal
project run on one machine.

## Trust model

**Untrusted:** issue titles and bodies, issue and PR comments, the contents of
any repository the agent checks out, and every model response derived from them.

**Trusted:** `.env` and `agent-loop.config.yaml` on the host, the `gh`
credentials, and this repository's own code.

The model is not a security boundary. It reads untrusted text and its output
steers tools, so every guarantee below has to hold independently of what the
model decides to do.

## The boundaries

**Path-gate** ([tools.py](agent_loop/tools.py)) — file tools resolve paths with
`realpath` and refuse anything outside the checkout, symlinks included. `.git/`
inside the checkout is refused too: publishing runs `git commit` on the host
even when the agent's shell is containerised, and git executes `.git/hooks/`
when it does. `gh.py` disables `core.hooksPath` on every git call as the second
lock on that same door.

**Execution environment** ([container.py](agent_loop/container.py)) — when a
source declares a `container`, the agent's shell commands run in an ephemeral
container with the checkout bind-mounted. Without one they run on the host, and
the path-gate is the only boundary. Prefer declaring a container for any source
that accepts issues from outside your organisation.

**Environment scrubbing** ([exec.py](agent_loop/exec.py)) — commands the agent
chose to run get an allowlisted environment (`PATH`, `HOME`, toolchain roots),
never the runner's. `DEEPSEEK_API_KEY` and the GitHub token stay with the
host-side calls that need them. Add a variable to `_AGENT_ENV_ALLOWLIST` only
after asking what reading it would give an attacker.

**No shell** ([exec.py](agent_loop/exec.py)) — every subprocess is an argv
array. Issue text reaches command arguments; it must never reach a shell parser.

**Intake gate and diff review** — the triage gate judges the *request* before
any clone and rejects unsafe ones outright; the `security` node judges the
*diff* before a PR opens. Both parse verdicts with
[verdicts.py](agent_loop/github/verdicts.py), which fails closed: an unreadable
verdict blocks rather than allows.

**Merge policy** ([policy.py](agent_loop/github/policy.py)) — auto-merge
additionally requires green CI from GitHub's Checks API (never the agent's own
account of itself), an approval, changed files inside the allowed globs, and a
diff under the line limit.

## What is not defended

- **A malicious dependency of a target repository.** The agent runs that repo's
  build and test commands; without a container they run on your host as you.
- **Cost.** `AGENT_MAX_TURNS` bounds steps, not spend.
- **A compromised model endpoint.** Responses are treated as untrusted input,
  but a hostile endpoint that also controls the target repository is out of
  scope.

## Changing any of this

The pre-PR checklist in [AGENTS.md](AGENTS.md) applies: a diff touching
`tools.py`, `exec.py`, `container.py`, or any place issue text reaches a prompt
or a process argument needs the boundary tests in
[tests/test_path_gate.py](tests/test_path_gate.py) and
[tests/test_agent_env.py](tests/test_agent_env.py) extended, not just passing.
