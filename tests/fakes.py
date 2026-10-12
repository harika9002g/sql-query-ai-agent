"""A scripted stand-in for a chat model, so the LangGraph workflow can be tested offline and deterministically."""
from __future__ import annotations

import re
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage


def user_request(messages: list[BaseMessage]) -> str:
    m = re.search(r"<user_request>(.*?)</user_request>", messages[-1].content, re.S)
    return (m.group(1) if m else messages[-1].content).strip()


class _Structured:
    def __init__(self, fake: "FakeLLM", schema: type):
        self.fake, self.schema = fake, schema

    def invoke(self, messages: list[BaseMessage], *_: Any, **__: Any) -> Any:
        self.fake.calls.append((self.schema.__name__, messages))
        handler = self.fake.handlers[self.schema.__name__]
        out = handler(messages)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, self.schema) else self.schema(**out)


class FakeLLM:
    """handlers: {"IntentDecision": fn(messages) -> dict|model, "SQLDraft": ..., ...}; text: fn(messages) -> str."""

    def __init__(self, handlers: dict[str, Callable] | None = None, text: Callable | None = None):
        self.handlers = handlers or {}
        self.text = text or (lambda msgs: "This query returns the requested rows.")
        self.calls: list[tuple[str, list[BaseMessage]]] = []

    def with_structured_output(self, schema: type, **_: Any) -> _Structured:
        return _Structured(self, schema)

    def invoke(self, messages: list[BaseMessage], *_: Any, **__: Any) -> AIMessage:
        self.calls.append(("text", messages))
        return AIMessage(self.text(messages))

    def called(self, name: str) -> list[list[BaseMessage]]:
        return [m for n, m in self.calls if n == name]
