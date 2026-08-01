"""The intake security gate: verdict parsing + the reject short-circuit in
_autofix (no clone/container is reached on a rejection). The LLM is a stub."""

from __future__ import annotations

from types import SimpleNamespace

from agent_loop.config import GithubSourceConfig
from agent_loop.db import tasks
from agent_loop.github import source as source_mod
from agent_loop.github.source import GithubSource
from tests.conftest import make_async


class FakeLLM:
    def __init__(self, content):
        self._content = content
        self.configs = []

    async def ainvoke(self, messages, config=None):
        self.configs.append(config)
        return SimpleNamespace(content=self._content)


class DummyQueue:
    def poke(self):
        pass


def make_source(verdict):
    cfg = GithubSourceConfig(type="github", id="t", repo="o/r")
    return GithubSource(cfg, DummyQueue(), FakeLLM(verdict))


def make_task(**meta):
    m = {"sourceId": "t", "repo": "o/r", "issue": 1, "kind": "bug", "body": "please add <script>", **meta}
    row = tasks.create("github", "add a script", "/cwd", m)
    tasks.set_status(row.id, "running")
    return row


async def test_triage_allows_legit_request(monkeypatch):
    src = make_source("VERDICT: ALLOW\nlooks like normal work")
    monkeypatch.setattr(source_mod, "issue_author_association", make_async("OWNER"))
    task = make_task()
    allowed, _ = await src._triage(task, {"repo": "o/r", "issue": 1})
    assert allowed is True

    # The gate runs outside any graph, so it is its own trace root — it has to
    # carry the build identity itself or those runs land in LangSmith unlabelled.
    meta = src.llm.configs[0]["metadata"]
    assert meta["stage"] == "triage"
    assert meta["issue"] == 1 and meta["task_id"] == task.id
    assert meta["version"] and meta["commit"]


async def test_triage_rejects_malicious_request(monkeypatch):
    src = make_source("VERDICT: REJECT\nInjecting third-party scripts is unsafe.")
    monkeypatch.setattr(source_mod, "issue_author_association", make_async("NONE"))
    allowed, _ = await src._triage(make_task(), {"repo": "o/r", "issue": 1})
    assert allowed is False


async def test_autofix_short_circuits_on_reject(monkeypatch):
    src = make_source("VERDICT: REJECT\nInjecting third-party scripts is unsafe.")
    monkeypatch.setattr(source_mod, "issue_author_association", make_async("NONE"))
    comment = make_async(None)
    monkeypatch.setattr(source_mod, "comment_issue", comment)

    async def boom_clone(*a, **k):
        raise AssertionError("rejected task must not reach ensure_clone")

    # If the gate failed to short-circuit, this clone call would blow up the test.
    monkeypatch.setattr(source_mod, "ensure_clone", boom_clone)

    task = make_task()
    result = await src._autofix(task, task.meta_dict())

    assert "Rejected" in result
    assert tasks.get(task.id).status == "rejected"
    assert len(comment.calls) == 1
