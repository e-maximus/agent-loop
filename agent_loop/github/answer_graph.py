"""The `question` handler: a degenerate two-step graph — read-only agent answers,
then we post the answer as an issue comment. No branch, no PR.

Kept as a small graph for symmetry with the autofix graph; a question thread
stays open (the `question` label is kept), and the conversation cursor
(`answeredThrough`) marks what we have already answered.
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ..config import GithubSourceConfig, get_settings
from ..container import ExecEnv
from ..logging import create_logger
from ..tools import build_tools
from . import gh, prompts

log = create_logger("answer")


def _recursion_limit() -> int:
    """react recursion limit derived from the turn budget (each turn ≈ model + tools)."""
    return get_settings().agent_max_turns * 2 + 1


class _AnswerInput(TypedDict):
    """Supplied by the source runner before the graph runs; required so nodes
    can read them directly."""

    repo: str
    issue: int
    title: str
    body: str
    url: str
    thread: str  # rendered prior comments


class AnswerState(_AnswerInput, total=False):
    answer: str
    result: str


def render_thread(comments: list[gh.IssueComment]) -> str:
    if not comments:
        return ""
    lines = []
    for c in comments:
        who = "agent-loop (you, earlier)" if gh.is_bot_comment(c.body) else f"user ({c.author})"
        where = f" on {c.path}:{c.line}" if getattr(c, "path", "") else ""
        lines.append(f"--- {who}{where} ---\n{c.body}")
    return "\n\nConversation so far (oldest first):\n\n" + "\n\n".join(lines)


def build_answer_graph(
    llm: BaseChatModel,
    env: ExecEnv,
    cfg: GithubSourceConfig,
    guidance: str = "",
    *,
    auto_post: bool = True,
):
    guidance_suffix = f"\n\n{guidance}" if guidance else ""

    async def answer(s: AnswerState) -> dict[str, Any]:
        log.info(f"#{s['issue']} answer")
        tools = build_tools(env, include_write=False)  # read-only
        agent = create_agent(
            llm,
            tools,
            system_prompt=prompts.answer_prompt(cfg.labels.bug, cfg.labels.feature) + guidance_suffix,
        )
        user = (
            f"Answer this GitHub issue (a question).\n\nTitle: {s['title']}\n\n"
            f"{s.get('body') or '(no body)'}{s.get('thread', '')}\n\nIssue: {s['url']}"
        )
        result = await agent.ainvoke(
            cast("Any", {"messages": [("user", user)]}),
            config={"recursion_limit": _recursion_limit()},
        )
        final = result["messages"][-1]
        text = final.content.strip() if isinstance(final.content, str) else str(final.content)
        return {"answer": text or "(agent produced no answer)"}

    async def post(s: AnswerState) -> dict[str, Any]:
        await gh.comment_issue(
            s["repo"], s["issue"], f"{gh.BOT_COMMENT_PREFIX}\n\n{s.get('answer', '')[:60000]}"
        )
        return {"result": f"Answered question #{s['issue']}."}

    g = StateGraph(AnswerState)

    # langgraph types a node as StateNode[NodeInputT, None], which does not
    # accept a plain `async def (State) -> dict`. The functions below are exactly
    # what the runtime calls; `add` is the one place that says so, instead of a
    # pyright-ignore on every registration.
    def add(name: str, fn: Any) -> None:
        g.add_node(name, fn)

    add("answer", answer)
    g.add_edge(START, "answer")
    if auto_post:
        # Issue questions post themselves as an issue comment. PR replies skip
        # this — the caller posts the answer on the PR thread instead.
        add("post", post)
        g.add_edge("answer", "post")
        g.add_edge("post", END)
    else:
        g.add_edge("answer", END)
    return g.compile()
