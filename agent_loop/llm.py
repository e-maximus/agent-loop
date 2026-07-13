"""The chat model powering the agent — DeepSeek via LangChain init_chat_model.

Single provider for now; init_chat_model keeps it trivial to add others later.
The model only provides the "brain": the tool-use loop is LangGraph's
create_react_agent and the tool executor is ours (tools.py).
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from .config import settings


def make_llm() -> BaseChatModel:
    return init_chat_model(
        settings.deepseek_model,
        model_provider="deepseek",
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=0,
    )
