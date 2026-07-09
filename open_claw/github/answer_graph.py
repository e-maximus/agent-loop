"""The `question` handler: a degenerate two-step graph — read-only agent answers,
then we post the answer as an issue comment. No branch, no PR.

Kept as a small graph for symmetry with the autofix graph; a question thread
stays open (the `question` label is kept), and the conversation cursor
(`answeredThrough`) marks what we have already answered.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from ..config import GithubSourceConfig, settings
from ..container import ExecEnv
from ..logging import create_logger
from ..tools import build_tools
from . import gh, prompts

log = create_logger("answer")

_RECURSION = settings.agent_max_turns * 2 + 1


class AnswerState(TypedDict, total=False):
    repo: str
    issue: int
    title: str
    body: str
    url: str
    thread: str  # rendered prior comments
    answer: str
    result: str


def render_thread(comments: list[gh.IssueComment]) -> str:
    if not comments:
        return ""
    lines = []
    for c in comments:
        who = "open-claw (you, earlier)" if gh.is_bot_comment(c.body) else f"user ({c.author})"
        lines.append(f"--- {who} ---\n{c.body}")
    return "\n\nConversation so far (oldest first):\n\n" + "\n\n".join(lines)


def build_answer_graph(
    llm: BaseChatModel, env: ExecEnv, cfg: GithubSourceConfig, guidance: str = ""
):
    guidance_suffix = f"\n\n{guidance}" if guidance else ""

    async def answer(s: AnswerState) -> dict[str, Any]:
        log.info(f"#{s['issue']} answer")
        tools = build_tools(env, include_write=False)  # read-only
        agent = create_react_agent(
            llm,
            tools,
            prompt=prompts.answer_prompt(cfg.labels.bug, cfg.labels.feature) + guidance_suffix,
        )
        user = (
            f"Answer this GitHub issue (a question).\n\nTitle: {s['title']}\n\n"
            f"{s.get('body') or '(no body)'}{s.get('thread', '')}\n\nIssue: {s['url']}"
        )
        result = await agent.ainvoke(
            {"messages": [("user", user)]}, config={"recursion_limit": _RECURSION}
        )
        final = result["messages"][-1]
        text = final.content.strip() if isinstance(final.content, str) else str(final.content)
        return {"answer": text or "(agent produced no answer)"}

    async def post(s: AnswerState) -> dict[str, Any]:
        await gh.comment_issue(
            s["repo"], s["issue"], f"{gh.BOT_COMMENT_PREFIX}\n\n{s['answer'][:60000]}"
        )
        return {"result": f"Answered question #{s['issue']}."}

    g = StateGraph(AnswerState)
    g.add_node("answer", answer)
    g.add_node("post", post)
    g.add_edge(START, "answer")
    g.add_edge("answer", "post")
    g.add_edge("post", END)
    return g.compile()
