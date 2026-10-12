"""LangGraph state. Persisted per conversation thread by the checkpointer."""
from __future__ import annotations

from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


RESET = "__reset__"


def _trace_reducer(old: list[str] | None, new: list[str] | None) -> list[str]:
    """Append node names for the current turn; a leading RESET marker starts a new turn."""
    if not new:
        return old or []
    if new[0] == RESET:
        return new[1:]
    return (old or []) + new


class AgentState(TypedDict, total=False):
    # ---- conversation (persists across turns)
    messages: Annotated[list[AnyMessage], add_messages]
    last_sql: Optional[str]          # most recent valid SQL — follow-ups modify this

    # ---- per-turn input
    user_input: str
    execute: bool

    # ---- per-turn working state (reset by guard_input)
    trace: Annotated[list[str], _trace_reducer]
    status: Optional[str]            # ok | rejected | clarify | info | error
    message: Optional[str]           # chat text for rejected/clarify/info/error turns
    intent: Optional[str]
    intent_reason: Optional[str]
    mode: Optional[str]              # generate | modify | explain | optimize | debug
    user_sql: Optional[str]
    user_sql_validation: Optional[dict[str, Any]]
    relevant_tables: list[str]
    schema_context: Optional[str]
    sql: Optional[str]
    attempts: int
    validation: Optional[dict[str, Any]]
    notes: list[str]                 # assumptions / improvements
    issues: list[dict[str, str]]     # debug findings
    llm_index_recommendations: list[str]
    optimization: Optional[dict[str, Any]]
    results: Optional[dict[str, Any]]
    equivalence: Optional[dict[str, Any]]
    explanation: Optional[str]
    error: Optional[str]
    response: Optional[dict[str, Any]]
