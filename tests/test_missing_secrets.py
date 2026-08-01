"""A suite that is red because a secret is missing must not be treated as a
broken diff.

The concrete case: goals-app's e2e suite cannot boot without Clerk keys, so
without them every spec fails identically on a clean `main`. Nothing the agent
writes fixes that, and each fix cycle re-runs the whole verify suite — so the
cost of getting this wrong is three full suites plus a PR blaming the change.

Two independent guards, tested here at node level:
  1. `baseline` runs the commands on the untouched checkout first, and `verify`
     skips whatever was already red — the failure never enters the loop at all.
  2. With baseline off, `diagnose` classifies the red build as ENVIRONMENT and
     routing sends it to `summarize` instead of `implement`.

Nodes are reached through the compiled graph's `.bound` (a langgraph internal)
because they are closures over env/cfg — that is the seam being tested, since
the routing functions alone cannot show that the command was skipped.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_loop.config import GithubSourceConfig
from agent_loop.github.autofix_graph import after_diagnose, build_autofix_graph

LINT = "npm run lint"
E2E = "npm run test:e2e"

# What Playwright actually prints when the app cannot boot for want of a key.
CLERK_ERROR = "@clerk/nextjs: Missing publishable key. Set NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"


class FakeEnv:
    """Fails every command matching `red_when`, as a missing secret would."""

    repo_path = "/repo"

    def __init__(self, red_when=lambda cmd: False):
        self.red_when = red_when
        self.commands: list[str] = []

    async def shell(self, cmd: str):
        self.commands.append(cmd)
        red = self.red_when(cmd)
        return SimpleNamespace(code=1 if red else 0, stdout=CLERK_ERROR if red else "ok", stderr="")


class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.prompts: list[str] = []

    async def ainvoke(self, messages, config=None):
        self.prompts.append("\n".join(str(m[1]) for m in messages))
        return SimpleNamespace(content=self.content)


def make_graph(env, llm=None, **cfg_kw):
    cfg = GithubSourceConfig(
        type="github", id="t", repo="o/r", verifyCommands=[LINT, E2E], **cfg_kw
    )
    return build_autofix_graph(llm or FakeLLM(""), env, cfg)


def node(graph, name):
    return graph.nodes[name].bound


def state(**kw):
    return {
        "issue": 84,
        "base": "main",
        "repo": "o/r",
        "title": "e2e is red",
        "body": "",
        "url": "u",
        "plan": "p",
        **kw,
    }


# ── guard 1: the failure never enters the loop ─────────────────────────────
async def test_baseline_records_a_suite_that_is_red_without_the_secret():
    env = FakeEnv(red_when=lambda c: c == E2E)
    out = await node(make_graph(env), "baseline").ainvoke(state())
    assert out["baseline_broken"] == [E2E]
    # The lint run still happened — only the secret-dependent suite is excused.
    assert env.commands == [LINT, E2E]


async def test_verify_skips_the_baseline_red_suite_and_stays_green():
    env = FakeEnv(red_when=lambda c: c == E2E)
    out = await node(make_graph(env), "verify").ainvoke(state(baseline_broken=[E2E]))

    # Green: so after_verify goes to critic, and no fix cycle is ever spent.
    assert out["build_ok"] is True
    assert out["build_log"] == ""
    assert env.commands == [LINT], "the suite that needs the missing key must not run"


async def test_the_skipped_suite_is_named_in_the_pr_body():
    # Silently dropping it would read as an untested PR to a reviewer.
    llm = FakeLLM("summary")
    graph = make_graph(FakeEnv(), llm)
    await node(graph, "summarize").ainvoke(state(build_ok=True, baseline_broken=[E2E]))
    prompt = llm.prompts[-1]
    assert E2E in prompt
    assert "ALREADY failing" in prompt


async def test_a_skipped_suite_flags_the_pr_for_a_human():
    # Verified green, but not in the area the skipped suite covers — only a
    # person can say whether that gap matters.
    out = await node(make_graph(FakeEnv()), "mark").ainvoke(
        state(build_ok=True, security_ok=True, baseline_broken=[E2E])
    )
    assert out["needs_human"] is True


# ── guard 2: baseline off — diagnosis has to catch it ──────────────────────
async def test_without_baseline_the_missing_key_is_diagnosed_as_environment():
    env = FakeEnv(red_when=lambda c: c == E2E)
    graph = make_graph(env, FakeLLM("CAUSE: ENVIRONMENT\nno Clerk key in the container"), baselineVerify=False)

    assert await node(graph, "baseline").ainvoke(state()) == {"baseline_broken": []}

    verified = await node(graph, "verify").ainvoke(state(baseline_broken=[]))
    assert verified["build_ok"] is False

    diagnosed = await node(graph, "diagnose").ainvoke(state(build_log=verified["build_log"]))
    assert diagnosed["failure_cause"] == "ENVIRONMENT"
    # Not a retry: FLAKY is the only cause that spends one.
    assert diagnosed["verify_retries"] == 0

    # The point of the whole file: zero fix cycles, straight to summarize.
    assert after_diagnose(state(**diagnosed, implement_runs=0)) == "summarize"
