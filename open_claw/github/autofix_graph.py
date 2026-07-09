"""The autofix pipeline as a LangGraph StateGraph.

    (start) ─┬─ first pass ─▶ investigate → plan ─┐
             └─ rework ──────────────────────────▶ implement ─┬─(no changes)─▶ publish
                                                              └─(changes)─▶ write_tests ─▶ verify
                                                                                             │
       ┌──────── implement (bounded loop) ◀── red / revise / sec-fail ──────────────────────┤
       ▼                                                                                     │ green
    (re-run)                             critic ─approve─▶ security ─pass─▶ summarize ─▶ publish

The intake security gate ("should we do this at all?") runs BEFORE this graph,
on the host in the source runner, and can reject the task outright. `security`
here is the diff-level review ("did we introduce a vulnerability?").

Prepare (clone/branch) runs on the host BEFORE the container starts, so it lives
in the source runner, not here. In `rework` mode the graph re-enters at
`implement` to amend an existing PR. Publish (git/gh) also runs on the host but
is modelled as the terminal node.

The agent-heavy nodes (investigate/implement/write_tests) use create_react_agent
with the tools from tools.py; plan/critic/summarize are single LLM calls; verify
is deterministic (runs the configured commands, exit code decides).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from ..config import GithubSourceConfig, settings
from ..container import ExecEnv
from ..logging import create_logger
from ..tools import build_tools, describe_tool
from . import gh, prompts

log = create_logger("autofix")

MAX_FIX_CYCLES = 3
# react recursion limit derived from the turn budget (each turn ≈ model + tools).
_RECURSION = settings.agent_max_turns * 2 + 1


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
    build_ok: bool
    build_log: str
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


def build_autofix_graph(
    llm: BaseChatModel, env: ExecEnv, cfg: GithubSourceConfig, guidance: str = ""
):
    repo_path = env.repo_path
    # Repository instructions (AGENTS.md/CLAUDE.md) are injected into every node's
    # system prompt so the agent always sees them before acting.
    guidance_suffix = f"\n\n{guidance}" if guidance else ""

    async def _run_agent(system: str, user: str, *, include_write: bool) -> str:
        tools = build_tools(env, include_write=include_write)
        agent = create_react_agent(llm, tools, prompt=system + guidance_suffix)
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

    async def _ask(system: str, user: str) -> str:
        resp = await llm.ainvoke([("system", system + guidance_suffix), ("user", user)])
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
        summary = await _run_agent(prompts.implement_prompt(s["kind"]), user, include_write=True)
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

    async def verify(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} verify: {cfg.verify_commands}")
        logs: list[str] = []
        ok = True
        for cmd in cfg.verify_commands:
            res = await env.shell(cmd)
            logs.append(f"$ {cmd}\nexit {res.code}\n{(res.stdout + res.stderr)[-4000:]}")
            if res.code != 0:
                ok = False
                break  # stop at first failure; feed it back
        return {"build_ok": ok, "build_log": "" if ok else "\n\n".join(logs)}

    async def critic(s: AutofixState) -> dict[str, Any]:
        log.info(f"#{s['issue']} critic")
        diff = await env.shell("git diff HEAD")
        user = (
            f"{_issue_context(s)}\n\n--- plan ---\n{s['plan']}\n\n--- diff ---\n{diff.stdout[:12000]}"
        )
        verdict = await _ask(prompts.critic_prompt(), user)
        approved = "APPROVE" in verdict.split("\n", 1)[0].upper()
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
        review_ok = "PASS" in verdict.split("\n", 1)[0].upper()
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
        return {"pr_summary": await _ask(prompts.summarize_prompt(), user)}

    async def publish(s: AutofixState) -> dict[str, Any]:
        repo, issue, kind = s["repo"], s["issue"], s["kind"]
        summary = s.get("pr_summary") or s.get("diff_text", "")
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
            await gh.commit_all(repo_path, f"fix: address review feedback on #{issue}\n\n🤖 open-claw")
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
                f"{gh.BOT_COMMENT_PREFIX}\n\nopen-claw made no changes for this task.\n\n{s.get('diff_text', '')[:3000]}",
            )
            return {"result": f"No changes. {s.get('diff_text', '')[:500]}"}

        prefix = "feat" if kind == "feature" else "fix"
        title = f"{prefix}: {s['title']}"[:100]
        await gh.commit_all(repo_path, f"{title}\n\nCloses #{issue}\n\n🤖 open-claw")
        await gh.push(repo_path, s["branch"])

        body = f"Automated {'feature' if kind == 'feature' else 'fix'} for issue #{issue} by open-claw.\n\nCloses #{issue}\n\n---\n{summary[:3000]}{human}"
        pr = await gh.open_pr(repo_path, base=s["base"], title=title, body=body)

        await gh.comment_issue(
            repo, issue,
            f"{gh.BOT_COMMENT_PREFIX}\n\nOpened a PR: {pr.url}\n\n**What changed:**\n\n{summary[:2000]}{human}",
        )
        log.info(f"#{issue}: PR {pr.url}")
        merge_note = "Waiting for green CI to auto-merge." if cfg.auto_merge else "Auto-merge is off — PR is waiting for review."
        return {"result": f"PR opened: {pr.url}. {merge_note}", "pr_number": pr.number, "pr_url": pr.url}

    # ── routing ──────────────────────────────────────────────────────────────
    def after_implement(s: AutofixState) -> Literal["write_tests", "publish"]:
        return "write_tests" if s.get("made_changes") else "publish"

    def after_verify(s: AutofixState) -> Literal["critic", "implement", "summarize"]:
        if s["build_ok"]:
            return "critic"
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

    g = StateGraph(AutofixState)
    g.add_node("investigate", investigate)
    g.add_node("plan", plan)
    g.add_node("implement", implement)
    g.add_node("write_tests", write_tests)
    g.add_node("verify", verify)
    g.add_node("critic", critic)
    g.add_node("security", security)
    g.add_node("summarize", summarize)
    g.add_node("publish", publish)

    g.add_conditional_edges(START, entry, ["implement", "investigate"])
    g.add_edge("investigate", "plan")
    g.add_edge("plan", "implement")
    g.add_conditional_edges("implement", after_implement, ["write_tests", "publish"])
    g.add_edge("write_tests", "verify")
    g.add_conditional_edges("verify", after_verify, ["critic", "implement", "summarize"])
    g.add_conditional_edges("critic", after_critic, ["security", "implement"])
    g.add_conditional_edges("security", after_security, ["summarize", "implement"])

    def set_needs_human(s: AutofixState) -> dict[str, Any]:
        return {"needs_human": (not s.get("build_ok", False)) or (not s.get("security_ok", True))}

    g.add_node("mark", set_needs_human)
    g.add_edge("summarize", "mark")
    g.add_edge("mark", "publish")
    g.add_edge("publish", END)

    return g.compile()
