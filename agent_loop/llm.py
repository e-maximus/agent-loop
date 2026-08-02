"""The chat model powering the agent — DeepSeek via LangChain init_chat_model.

Single provider for now; init_chat_model keeps it trivial to add others later.
The model only provides the "brain": the tool-use loop is LangChain's
create_agent and the tool executor is ours (tools.py).
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from .config import get_settings


def make_llm(model: str | None = None) -> BaseChatModel:
    settings = get_settings()
    return init_chat_model(
        model or settings.deepseek_model,
        model_provider="deepseek",
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=0,
    )


def make_strong_llm() -> BaseChatModel:
    """The model for implement and critic — see DEEPSEEK_MODEL_STRONG in
    config.py for why those two nodes get their own."""
    return make_llm(get_settings().deepseek_model_strong)
