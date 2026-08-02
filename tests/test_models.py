"""The two-model split: implement and critic run on DEEPSEEK_MODEL_STRONG,
everything else on DEEPSEEK_MODEL. The models themselves are never built here —
these tests cover the wiring that decides which one a node gets."""

from __future__ import annotations

from agent_loop.config import GithubSourceConfig, get_settings
from agent_loop.github.source import GithubSource
from tests.test_triage import DummyQueue, FakeLLM


def test_strong_model_defaults_to_a_different_model():
    # If these ever collapse to one value the split silently stops existing, and
    # every node quietly runs on the same model again.
    settings = get_settings()
    assert settings.deepseek_model_strong != settings.deepseek_model


def test_source_keeps_the_two_models_apart():
    weak, strong = FakeLLM("a"), FakeLLM("b")
    src = GithubSource(GithubSourceConfig(type="github", id="t", repo="o/r"), DummyQueue(), weak, strong)
    assert src.llm is weak
    assert src.strong_llm is strong


def test_source_falls_back_to_one_model():
    # A caller with a single model (tests, any embedder of GithubSource) must
    # still get a working source rather than a None strong model.
    weak = FakeLLM("a")
    src = GithubSource(GithubSourceConfig(type="github", id="t", repo="o/r"), DummyQueue(), weak)
    assert src.strong_llm is weak
