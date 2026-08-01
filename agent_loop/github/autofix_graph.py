"""The autofix pipeline as a LangGraph StateGraph.

    (start) ─▶ baseline ─┬─ first pass ─▶ investigate → plan ─┐
                         └─ rework ──────────────────────────▶ implement ─┬─(no changes)─▶ publish
                                                                          └─(changes)─▶ write_tests ─▶ verify
                                                                                                         │
       ┌──────── implement (bounded loop) ◀── CODE / revise / sec-fail ── diagnose ◀── red ──────────────┤
       ▼                                          │                          └─ FLAKY ─▶ verify (once)   │ green
    (re-run)                                      └─ ENVIRONMENT ─▶ summarize                            │
                                         critic ─approve─▶ security ─pass─▶ summarize ─▶ publish ◀───────┘

`baseline` runs the verify commands on the untouched checkout first: whatever is
red there cannot be the change's fault, so it is skipped when verifying the diff.
`diagnose` classifies a red build (CODE / ENVIRONMENT / FLAKY) before another fix
cycle is spent on a failure no diff can repair.

The intake security gate ("should we do this at all?") runs BEFORE this graph,
on the host in the source runner, and can reject the task outright. `security`
here is the diff-level review ("did we introduce a vulnerability?").

Prepare (clone/branch) runs on the host BEFORE the container starts, so it lives
in the source runner, not here. In `rework` mode the graph re-enters at
`implement` to amend an existing PR. Publish (git/gh) also runs on the host but
is modelled as the terminal node.

The agent-heavy nodes (investigate/implement/write_tests) use create_agent
with the tools from tools.py; plan/critic/summarize are single LLM calls; verify
is deterministic (runs the configured commands, exit code decides).

Two models: `implement` and `critic` run on the strong one (DEEPSEEK_MODEL_STRONG),
the rest on the default. Those are the two nodes that decide what the diff is and
whether it stands.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ..config import GithubSourceConfig, settings
from ..container import ExecEnv
from ..logging import create_logger
from ..tools import build_tools, describe_tool
from . import gh, prompts
from .verdicts import parse_verdict

log = create_logger("autofix")

MAX_FIX_CYCLES = 3
# One retry for a failure diagnosed as flaky before it counts as environmental.
MAX_VERIFY_RETRIES = 1
# How much of a failing command's output to put in the task log, and how much
# of it to hand the model.
VERIFY_LOG_TAIL = 2000
VERIFY_MODEL_TAIL = 4000
# react recursion limit derived from the turn budget (each turn ≈ model + tools).
_RECURSION = settings.agent_max_turns * 2 + 1


def condense(text: str, limit: int) -> str:
    """Collapse repeated lines, then take the tail.

    A dev server logging the same warning on every page load (Playwright's
    `[WebServer]` relay is the usual culprit) can push thousands of identical
    lines through the window, evicting the assertion failure that says what
    actually broke. Deduplicating first means the tail carries distinct
    information rather than the loudest repeat.
    """
    seen: dict[str, int] = {}
    order: list[str] = []
    for line in text.splitlines():
        key = line.strip()
        if not key:
            continue
        if key in seen:
            seen[key] += 1
        else:
            seen[key] = 1
            order.append(key)

    out: list[str] = []
    for key in order:
        n = seen[key]
        out.append(key if n == 1 else f"{key}  (× {n})")
    return "\n".join(out)[-limit:]


class AutofixState(TypedDict, total=False):
    # ── input (from the poller task) ──
    repo: str
    issue: int
    kind: str  # "bug" | "feature"
    title: str
    body: str
    url: str
    base: str
    branch: str
    repo_path: str
    # "rework" when re-entering to address CI/review feedback on an existing PR;
    # empty/absent for a first pass that opens a new PR.
    mode: str

    # ── produced by nodes ──
    findings: str
    plan: str
    made_changes: bool
    implement_runs: int
    diff_text: str
    test_note: str
    baseline_broken: list[str]
    build_ok: bool
    build_log: str
    # Why verify went red, per `diagnose`: CODE | ENVIRONMENT | FLAKY.
    failure_cause: str
    failure_reason: str
    verify_retries: int
    critic_feedback: str
    security_ok: bool
    security_feedback: str
    needs_human: bool
    pr_summary: str
    pr_number: int
    pr_url: str
    result: str  # final human-readable outcome


def _issue_context(s: AutofixState) -> str:
    return (
        f"Issue #{s['issue']} in {s['repo']}\n\nTitle: {s['title']}\n\n"
        f"{s.get('body') or '(no body)'}\n\nIssue URL: {s['url']}"
    )


# ── routing ────────────────────────────────────────────────────────────────
# Pure functions of the state: no cfg, env or LLM, so the pipeline's decisions
# can be tested without building a graph.
def after_implement(s: AutofixState) -> Literal["write_tests", "publish"]:
    return "write_tests" if s.get("made_changes") else "publish"


def after_verify(s: AutofixState) -> Literal["critic", "diagnose"]:
    return "critic" if s["build_ok"] else "diagnose"


def after_diagnose(s: AutofixState) -> Literal["verify", "implement", "summarize"]:
    cause = s.get("failure_cause", "CODE")
    if cause == "FLAKY" and s.get("verify_retries", 0) <= MAX_VERIFY_RETRIES:
        log.info(f"#{s['issue']}: failure looks flaky — re-running verify once")
        return "verify"
    if cause != "CODE":
        # No diff fixes this. Publish what we have, labelled, and let a human
        # fix the environment instead of burning cycles pretending otherwise.
        log.warn(
            f"#{s['issue']}: verify is red for a reason no code change fixes "
            f"({cause}) — escalating\n{s.get('failure_reason', '')[:VERIFY_LOG_TAIL]}"
        )
        return "summarize"
    if s.get("implement_runs", 0) < MAX_FIX_CYCLES:
        return "implement"
    log.warn(f"#{s['issue']}: build still red after {MAX_FIX_CYCLES} cycles — escalating")
    return "summarize"


def after_critic(s: AutofixState) -> Literal["security", "implement"]:
    # Approved by the critic → security review. REVISE → back to implement
    # (bounded), else give up and let security still run before publishing.
    if not s.get("critic_feedback"):
        return "security"
    if s.get("implement_runs", 0) < MAX_FIX_CYCLES:
        return "implement"
    return "security"


def after_security(s: AutofixState) -> Literal["summarize", "implement"]:
    if s.get("security_ok"):
        return "summarize"
    if s.get("implement_runs", 0) < MAX_FIX_CYCLES:
        return "implement"
    log.warn(f"#{s['issue']}: security still failing after {MAX_FIX_CYCLES} cycles — escalating")
    return "summarize"


# Rework re-enters straight at implement (an existing PR is being amended);
# a first pass starts at investigate.
def entry(s: AutofixState) -> Literal["implement", "investigate"]:
    return "implement" if s.get("mode") == "rework" else "investigate"


def build_autofix_graph(
    llm: BaseChatModel,
    env: ExecEnv,
    cfg: GithubSourceConfig,
    guidance: str = "",
    strong_llm: BaseChatModel | None = None,
):
    # `implement` and `critic` run on strong_llm: writing the diff and judging it
    # are where model quality turns into fewer fix cycles. Everything else —
    # reading the repo, planning, diagnosing, summarising — stays on llm.
    strong = strong_llm or llm
    repo_path = env.repo_path
    # Repository instructions (AGENTS.md/CLAUDE.md) are injected into every node's
    # system prompt so the agent always sees them before acting.
    guidance_suffix = f"\n\n{guidance}" if guidance else ""

    async def _run_agent(
        system: str, user: str, *, include_write: bool, model: BaseChatModel | None = None
    ) -> str:
        tools = build_tools(env, include_write=include_write)
        agent = create_agent(model or llm, tools, system_prompt=system + guidance_suffix)
        # Stream so every tool call is logged the moment it happens. If the run
        # crashes mid-flight (e.g. it hits the recursion limit), the trace up to
        # that point survives — otherwise we'd log nothing and never know whether
        # the agent was stuck in a loop or simply ran out of steps.
        logged = 0
        final = ""
        async for state in agent.astream(
            {"messages": [("user", user)]},
            config={"recursion_limit": _RECURSION},
            stream_mode="values",
        ):
            messages = state["messages"]
            for m in messages[logged:]:
                for call in getattr(m, "tool_calls", None) or []:
                    log.info(f"→ {describe_tool(call['name'], call.get('args', {}))}")
            logged = len(messages)
            last = messages[-1]
            final = last.content.strip() if isinstance(last.content, str) else str(last.content)
        return final

    async def _ask(system: str, user: str, model: BaseChatModel | None = None) -> str:
        resp = await (model or llm).ainvoke(
            [("system", system + guidance_suffix), ("user", user)]
        )
        return resp.content.strip() if isinstance(resp.content, str) else str(resp.content)

    # ── nodes ──────────────────────────────────────────────────────────────
    async def investigate(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} investigate")
        findings = await _run_agent(
            prompts.investigate_prompt(s["kind"]), _issue_context(s), include_write=False
        )
        return {"findings": findings}

    async def plan(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} plan")
        user = f"{_issue_context(s)}\n\n--- investigation findings ---\n{s['findings']}"
        return {"plan": await _ask(prompts.plan_prompt(s["kind"]), user)}

    async def implement(s: AutofixState) -> dict[str, Any]:
        runs = s.get("implement_runs", 0) + 1
        log.info(f"#{s['issue']} implement (run {runs})")
        user = f"{_issue_context(s)}\n\n--- plan ---\n{s['plan']}"
        if s.get("build_log"):
            user += f"\n\n--- previous build/test FAILED, fix this ---\n{s['build_log'][:6000]}"
        if s.get("critic_feedback"):
            user += f"\n\n--- reviewer asked for changes ---\n{s['critic_feedback']}"
        if s.get("security_feedback"):
            user += f"\n\n--- security review found issues, fix them ---\n{s['security_feedback'][:6000]}"
        summary = await _run_agent(
            prompts.implement_prompt(s["kind"]), user, include_write=True, model=strong
        )
        changed = await gh.has_changes(repo_path)
        return {
            "implement_runs": runs,
            "made_changes": changed,
            "diff_text": summary,
            # clear the consumed feedback so the next cycle starts fresh
            "build_log": "",
            "critic_feedback": "",
            "security_feedback": "",
        }

    async def write_tests(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} write_tests")
        user = f"{_issue_context(s)}\n\n--- what changed ---\n{s.get('diff_text', '')}"
        note = await _run_agent(prompts.write_tests_prompt(s["kind"]), user, include_write=True)
        return {"test_note": note}

    async def baseline(s: AutofixState) -> dict[str, Any]:
        """Which verify commands are ALREADY red on the base branch, before we
        change anything.

        A suite that needs a service or a secret the container does not have
        fails identically whatever the diff does — gating on it spends every fix
        cycle on something no code change can repair, then blocks the PR. Run
        the commands once on the untouched checkout and treat what is red here
        as an environment fact, not a regression.
        """
        if not cfg.baseline_verify:
            return {"baseline_broken": []}

        broken: list[str] = []
        for cmd in cfg.verify_commands:
            res = await env.shell(cmd)
            if res.code != 0:
                broken.append(cmd)
                tail = condense(res.stdout + res.stderr, VERIFY_LOG_TAIL)
                log.warn(
                    f"#{s['issue']} baseline: `{cmd}` is ALREADY red on {s['base']} "
                    f"(exit {res.code}) — it will not gate this PR\n{tail}"
                )
        if broken:
            log.warn(f"#{s['issue']} baseline: {len(broken)}/{len(cfg.verify_commands)} red before any change")
        else:
            log.info(f"#{s['issue']} baseline: all {len(cfg.verify_commands)} commands green")
        return {"baseline_broken": broken}

    async def verify(s: AutofixState) -> dict[str, Any]:
        skip = set(s.get("baseline_broken") or [])
        commands = [c for c in cfg.verify_commands if c not in skip]
        log.info(f"#{s['issue']} verify: {commands}" + (f" (skipping baseline-red: {sorted(skip)})" if skip else ""))
        logs: list[str] = []
        ok = True
        for cmd in commands:
            res = await env.shell(cmd)
            logs.append(f"$ {cmd}\nexit {res.code}\n{condense(res.stdout + res.stderr, VERIFY_MODEL_TAIL)}")
            if res.code != 0:
                ok = False
                # The failure is fed back to the model, but without it in the task
                # log a red cycle is indistinguishable from any other — you cannot
                # tell a broken diff from a broken environment without re-running
                # the commands by hand.
                tail = condense(res.stdout + res.stderr, VERIFY_LOG_TAIL)
                log.warn(f"#{s['issue']} verify FAILED: `{cmd}` exit {res.code}\n{tail}")
                break  # stop at first failure; feed it back
        if ok:
            log.info(f"#{s['issue']} verify: all {len(commands)} commands green")
        return {"build_ok": ok, "build_log": "" if ok else "\n\n".join(logs)}

    async def diagnose(s: AutofixState) -> dict[str, Any]:
        """Can a code change fix this red build at all?

        Nothing in the diff repairs a missing service, an unreachable host, or
        an absent API key, and three implement cycles is an expensive way to
        find that out. Ask once, cheaply, before spending another one.
        """
        verdict = await _ask(prompts.diagnose_prompt(), condense(s.get("build_log", ""), 8000))
        # Unparseable → CODE: an unreadable verdict must not be what excuses the
        # agent from fixing its own diff.
        cause = parse_verdict(
            verdict, "CAUSE", ("CODE", "ENVIRONMENT", "FLAKY"), default="CODE"
        )
        log.info(f"#{s['issue']} diagnose: {cause}")
        retries = s.get("verify_retries", 0) + (1 if cause == "FLAKY" else 0)
        return {"failure_cause": cause, "failure_reason": verdict, "verify_retries": retries}

    async def critic(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} critic")
        diff = await env.shell("git diff HEAD")
        user = (
            f"{_issue_context(s)}\n\n--- plan ---\n{s['plan']}\n\n--- diff ---\n{diff.stdout[:12000]}"
        )
        verdict = await _ask(prompts.critic_prompt(), user, model=strong)
        # Unparseable → REVISE: an unreadable review must not wave the diff through.
        approved = parse_verdict(verdict, "VERDICT", ("APPROVE", "REVISE"), default="REVISE") == "APPROVE"
        return {"critic_feedback": "" if approved else verdict, "build_ok": s["build_ok"]}

    async def security(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} security: {cfg.security_commands}")
        # Deterministic scanners first (npm audit, semgrep, …) — any non-zero
        # exit is a finding. Then an LLM security review of the diff itself.
        scan_logs: list[str] = []
        scan_ok = True
        for cmd in cfg.security_commands:
            res = await env.shell(cmd)
            if res.code != 0:
                scan_ok = False
                scan_logs.append(f"$ {cmd}\nexit {res.code}\n{(res.stdout + res.stderr)[-3000:]}")
        diff = await env.shell("git diff HEAD")
        user = f"{_issue_context(s)}\n\n--- plan ---\n{s['plan']}\n\n--- diff ---\n{diff.stdout[:12000]}"
        verdict = await _ask(prompts.security_prompt(), user)
        # Unparseable → FAIL. Note PASS/FAIL specifically: the reviewer's own
        # vocabulary (password, bypass) contains "PASS" as a substring.
        review_ok = parse_verdict(verdict, "VERDICT", ("PASS", "FAIL"), default="FAIL") == "PASS"
        ok = scan_ok and review_ok
        feedback = ""
        if not ok:
            parts = [] if review_ok else [verdict]
            parts += scan_logs
            feedback = "\n\n".join(parts)
        return {"security_ok": ok, "security_feedback": feedback}

    async def summarize(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} summarize")
        diff = await env.shell("git diff HEAD")
        user = f"{_issue_context(s)}\n\n--- diff ---\n{diff.stdout[:12000]}"
        # Without the verify outcome the summary describes whatever the agent ran
        # itself inside `implement`, which is how a PR ends up claiming green
        # tests after three red cycles.
        skipped = s.get("baseline_broken") or []
        ran = [c for c in cfg.verify_commands if c not in set(skipped)]
        if s.get("build_ok"):
            user += f"\n\n--- verification (authoritative) ---\nAll checks passed: {', '.join(ran)}"
        else:
            user += (
                "\n\n--- verification (authoritative) ---\nChecks are STILL FAILING. "
                "Do not claim they pass; state plainly what is red.\n"
                f"{condense(s.get('build_log', ''), VERIFY_MODEL_TAIL)}"
            )
        if skipped:
            # Say it in the PR: a reviewer who does not know these were red
            # before the change will read the gap as an untested PR.
            user += (
                f"\n\n--- not run ---\nThese commands were ALREADY failing on `{s['base']}` "
                f"before this change, so they were skipped and did not gate it: {', '.join(skipped)}. "
                "Mention this plainly and do not present it as the change's fault."
            )
        if s.get("failure_cause") in ("ENVIRONMENT", "FLAKY"):
            user += (
                f"\n\n--- why it is red ---\nThe failure was diagnosed as {s['failure_cause']} — "
                f"not something a code change fixes:\n{s.get('failure_reason', '')[:2000]}\n"
                "Say what the environment is missing so a human can fix it."
            )
        return {"pr_summary": await _ask(prompts.summarize_prompt(), user)}

    async def publish(s: AutofixState) -> dict[str, Any]:
        repo, issue, kind = s["repo"], s["issue"], s["kind"]
        summary = s.get("pr_summary") or s.get("diff_text", "")
        # publish runs on the host and never sees the repo's AGENTS.md, so the
        # attribution rule has to come from config rather than guidance.
        trailer = "\n\n🤖 agent-loop" if cfg.git_attribution else ""
        byline = " by agent-loop" if cfg.git_attribution else ""
        human = "\n\n> ⚠️ CI/tests or security review were still failing locally — please review carefully." if s.get("needs_human") else ""

        # ── rework: an existing PR is being amended, not a new one opened ──
        if s.get("mode") == "rework":
            pr_number, pr_url = s.get("pr_number", 0), s.get("pr_url", "")
            if not s.get("made_changes"):
                log.warn(f"#{issue}: rework produced no changes")
                await gh.comment_pr(
                    repo, pr_number,
                    f"{gh.BOT_COMMENT_PREFIX}\n\nI looked at the feedback but did not find a change to make.\n\n{s.get('diff_text', '')[:2000]}",
                )
                return {"result": "Rework: no changes.", "pr_number": pr_number, "pr_url": pr_url}
            await gh.commit_all(repo_path, f"fix: address review feedback on #{issue}{trailer}")
            await gh.push(repo_path, s["branch"])
            await gh.comment_pr(
                repo, pr_number,
                f"{gh.BOT_COMMENT_PREFIX}\n\nPushed changes addressing the review feedback.\n\n**What changed:**\n\n{summary[:2000]}{human}",
            )
            log.info(f"#{issue}: reworked PR {pr_url}")
            return {"result": f"Reworked PR: {pr_url}.{' Needs human review.' if human else ''}", "pr_number": pr_number, "pr_url": pr_url}

        if not s.get("made_changes"):
            log.warn(f"#{issue}: no changes")
            await gh.comment_issue(
                repo, issue,
                f"{gh.BOT_COMMENT_PREFIX}\n\nagent-loop made no changes for this task.\n\n{s.get('diff_text', '')[:3000]}",
            )
            return {"result": f"No changes. {s.get('diff_text', '')[:500]}"}

        prefix = "feat" if kind == "feature" else "fix"
        title = f"{prefix}: {s['title']}"[:100]
        await gh.commit_all(repo_path, f"{title}\n\nCloses #{issue}{trailer}")
        await gh.push(repo_path, s["branch"])

        body = f"Automated {'feature' if kind == 'feature' else 'fix'} for issue #{issue}{byline}.\n\nCloses #{issue}\n\n---\n{summary[:3000]}{human}"
        pr = await gh.open_pr(
            repo_path, base=s["base"], title=title, body=body, repo=repo, head=s["branch"]
        )

        if pr.existing:
            # Adopted a PR opened by an earlier run of this issue. The push above
            # already updated the branch; bring the description along with it.
            await gh.update_pr(repo, pr.number, title=title, body=body)
            verb = f"Updated the existing PR: {pr.url}"
        else:
            verb = f"Opened a PR: {pr.url}"

        await gh.comment_issue(
            repo, issue,
            f"{gh.BOT_COMMENT_PREFIX}\n\n{verb}\n\n**What changed:**\n\n{summary[:2000]}{human}",
        )
        log.info(f"#{issue}: PR {pr.url}")
        merge_note = "Waiting for green CI to auto-merge." if cfg.auto_merge else "Auto-merge is off — PR is waiting for review."
        return {"result": f"PR opened: {pr.url}. {merge_note}", "pr_number": pr.number, "pr_url": pr.url}

    g = StateGraph(AutofixState)
    g.add_node("baseline", baseline)
    g.add_node("investigate", investigate)
    g.add_node("plan", plan)
    g.add_node("implement", implement)
    g.add_node("write_tests", write_tests)
    g.add_node("verify", verify)
    g.add_node("diagnose", diagnose)
    g.add_node("critic", critic)
    g.add_node("security", security)
    g.add_node("summarize", summarize)
    g.add_node("publish", publish)

    # baseline runs first, on the untouched checkout — it is only meaningful
    # before implement has written anything, and rework needs it just as much.
    g.add_edge(START, "baseline")
    g.add_conditional_edges("baseline", entry, ["implement", "investigate"])
    g.add_edge("investigate", "plan")
    g.add_edge("plan", "implement")
    g.add_conditional_edges("implement", after_implement, ["write_tests", "publish"])
    g.add_edge("write_tests", "verify")
    g.add_conditional_edges("verify", after_verify, ["critic", "diagnose"])
    g.add_conditional_edges("diagnose", after_diagnose, ["verify", "implement", "summarize"])
    g.add_conditional_edges("critic", after_critic, ["security", "implement"])
    g.add_conditional_edges("security", after_security, ["summarize", "implement"])

    def set_needs_human(s: AutofixState) -> dict[str, Any]:
        # Commands skipped as baseline-red count too: the change is unverified in
        # exactly the area the suite would have covered, and only a human can say
        # whether that matters.
        return {
            "needs_human": (not s.get("build_ok", False))
            or (not s.get("security_ok", True))
            or bool(s.get("baseline_broken"))
        }

    g.add_node("mark", set_needs_human)
    g.add_edge("summarize", "mark")
    g.add_edge("mark", "publish")
    g.add_edge("publish", END)

    return g.compile()
