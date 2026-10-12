"""LangGraph wiring for the SQL agent (diagram and walkthrough: docs/ARCHITECTURE.md).

Main path:  guard_input → detect_intent → validate_scope → retrieve_schema → generate_sql
            → validate_sql (⟲ back to generate_sql with validator errors, up to N attempts)
            → optimize_sql → execute_sql (optional) → explain_sql → respond
Side paths: blocked input / out-of-scope / destructive / ambiguous → respond;
            pasted SQL (explain | optimize | debug) → analyze_user_sql first;
            schema questions → answer_schema_question.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import Nodes
from app.agent.state import RESET, AgentState
from app.config import settings
from app.db import SchemaCatalog, get_catalog

log = logging.getLogger(__name__)


def _friendly_llm_error(e: Exception) -> str:
    name = type(e).__name__
    text = str(e).lower()
    if "api_key" in text or "api key" in text or "authentication" in text or name == "AuthenticationError":
        return ("The language model isn't configured correctly (missing or invalid API key). "
                "Set ANTHROPIC_API_KEY (or another provider's key) and restart the server.")
    if "rate" in text and "limit" in text or name == "RateLimitError":
        return "The language model is rate-limited right now. Please wait a moment and try again."
    if "timeout" in text or "timed out" in text:
        return "The language model took too long to respond. Please try again."
    return f"The language model request failed ({name}). Please try again."


def _node(name: str, fn: Callable[[AgentState], dict], *, reset_trace: bool = False) -> Callable:
    @functools.wraps(fn)
    def wrapper(state: AgentState) -> dict:
        try:
            update = fn(state) or {}
        except Exception as e:
            log.exception("node %s failed", name)
            update = {"error": _friendly_llm_error(e)}
        update["trace"] = [RESET, name] if reset_trace else [name]
        return update
    return wrapper


# ---------------------------------------------------------------- routers

def route_after_guard(state: AgentState) -> str:
    return "respond" if state.get("status") else "detect_intent"


def route_after_scope(state: AgentState) -> str:
    if state.get("error") or state.get("status"):
        return "respond"
    mode = state.get("mode")
    if mode == "schema":
        return "answer_schema_question"
    if mode in ("explain", "optimize", "debug"):
        return "analyze_user_sql"
    return "retrieve_schema"


def route_after_analyze(state: AgentState) -> str:
    if state.get("status"):
        return "respond"
    if state.get("mode") == "explain":
        return "optimize_sql"
    return "retrieve_schema"


def route_after_generate(state: AgentState) -> str:
    return "respond" if state.get("error") or state.get("status") else "validate_sql"


def route_after_validate(state: AgentState) -> str:
    if state["validation"]["ok"]:
        return "optimize_sql"
    if state["validation"].get("destructive"):
        return "respond"  # never retry towards a write
    if state.get("attempts", 0) < settings.max_generation_attempts:
        return "generate_sql"  # self-correction loop with validator feedback
    return "respond"


def route_after_optimize(state: AgentState) -> str:
    return "execute_sql" if state.get("execute", True) else "explain_sql"


# ---------------------------------------------------------------- build

def build_graph(llm: Any = None, catalog: SchemaCatalog | None = None, checkpointer: Any = None):
    if llm is None:
        from app.llm import get_chat_model
        llm = get_chat_model()
    n = Nodes(llm, catalog or get_catalog())

    g = StateGraph(AgentState)
    g.add_node("guard_input", _node("guard_input", n.guard_input, reset_trace=True))
    for name in ("detect_intent", "validate_scope", "analyze_user_sql", "retrieve_schema", "generate_sql",
                 "validate_sql", "optimize_sql", "execute_sql", "explain_sql", "answer_schema_question"):
        g.add_node(name, _node(name, getattr(n, name)))
    g.add_node("respond", n.respond)

    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", route_after_guard, ["detect_intent", "respond"])
    g.add_conditional_edges("detect_intent", lambda s: "respond" if s.get("error") else "validate_scope",
                            ["validate_scope", "respond"])
    g.add_conditional_edges("validate_scope", route_after_scope,
                            ["respond", "answer_schema_question", "analyze_user_sql", "retrieve_schema"])
    g.add_conditional_edges("analyze_user_sql", route_after_analyze, ["respond", "optimize_sql", "retrieve_schema"])
    g.add_edge("retrieve_schema", "generate_sql")
    g.add_conditional_edges("generate_sql", route_after_generate, ["respond", "validate_sql"])
    g.add_conditional_edges("validate_sql", route_after_validate, ["optimize_sql", "generate_sql", "respond"])
    g.add_conditional_edges("optimize_sql", route_after_optimize, ["execute_sql", "explain_sql"])
    g.add_edge("execute_sql", "explain_sql")
    g.add_edge("explain_sql", "respond")
    g.add_edge("answer_schema_question", "respond")
    g.add_edge("respond", END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())
