"""Chat-model factory. Provider-agnostic via LangChain's init_chat_model ('provider:model')."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.config import settings

_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY"}

# Groq models that support strict JSON-schema structured outputs
# (https://console.groq.com/docs/structured-outputs). Others fall back to tool calling.
GROQ_STRICT_SCHEMA_MODELS = {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}


def provider() -> str:
    return settings.llm_model.split(":", 1)[0] if ":" in settings.llm_model else "anthropic"


def model_name() -> str:
    return settings.llm_model.split(":", 1)[1] if ":" in settings.llm_model else settings.llm_model


def llm_configured() -> bool:
    env = _KEY_ENV.get(provider())
    return bool(os.getenv(env)) if env else True


def model_kwargs() -> dict[str, Any]:
    """Provider-specific constructor arguments (kept separate so they can be unit-tested)."""
    kwargs: dict[str, Any] = {"max_retries": 2, "timeout": 90}
    if provider() == "anthropic":
        # Current Claude models reject sampling params; tune depth with effort instead.
        kwargs.update(max_tokens=8000, effort=settings.llm_effort)
    elif provider() == "groq":
        # Bounded output keeps each call well inside Groq's free-tier tokens-per-minute budget.
        kwargs.update(temperature=0, max_tokens=4096)
        if model_name().startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = settings.llm_effort  # low | medium | high
    else:
        kwargs.update(temperature=0)
    return kwargs


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    return init_chat_model(settings.llm_model, **model_kwargs())


def structured(llm: Any, schema: type) -> Any:
    """Structured output using each provider's most reliable mode.

    - Claude / OpenAI: native JSON-schema output (Claude rejects forced tool use).
    - Groq gpt-oss: strict JSON-schema output (constrained decoding; all fields required).
    - Anything else: tool calling.
    """
    llm_type = getattr(llm, "_llm_type", "")
    if llm_type in ("anthropic-chat", "openai-chat"):
        return llm.with_structured_output(schema, method="json_schema")
    if llm_type == "groq-chat" and getattr(llm, "model_name", "") in GROQ_STRICT_SCHEMA_MODELS:
        return llm.with_structured_output(schema, method="json_schema", strict=True)
    return llm.with_structured_output(schema)
