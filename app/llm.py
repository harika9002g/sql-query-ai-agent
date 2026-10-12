"""Chat-model factory. Provider-agnostic via LangChain's init_chat_model ('provider:model')."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.config import settings

_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY"}


def provider() -> str:
    return settings.llm_model.split(":", 1)[0] if ":" in settings.llm_model else "anthropic"


def llm_configured() -> bool:
    env = _KEY_ENV.get(provider())
    return bool(os.getenv(env)) if env else True


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    kwargs: dict[str, Any] = {"max_retries": 2, "timeout": 90}
    if provider() == "anthropic":
        # Current Claude models reject sampling params; tune depth with effort instead.
        kwargs.update(max_tokens=8000, effort=settings.llm_effort)
    else:
        kwargs.update(temperature=0)
    return init_chat_model(settings.llm_model, **kwargs)


def structured(llm: Any, schema: type) -> Any:
    """Structured output. Native JSON-schema mode where supported (Claude rejects forced tool use)."""
    if getattr(llm, "_llm_type", "") in ("anthropic-chat", "openai-chat"):
        return llm.with_structured_output(schema, method="json_schema")
    return llm.with_structured_output(schema)
