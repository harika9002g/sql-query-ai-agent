"""FastAPI app: chat API (JSON + Server-Sent Events streaming), schema API, and the static web UI."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.graph import build_graph
from app.agent.nodes import STEP_LABELS
from app.config import ROOT, settings
from app.db import get_catalog
from app.llm import llm_configured

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sql-agent")

FRONTEND = ROOT / "frontend"
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not Path(settings.db_path).exists():
        from app.seed import build_database
        build_database()
        log.info("Built sample database at %s", settings.db_path)
    get_catalog()
    app.state.graph = build_graph()
    log.info("Agent ready (model=%s, llm_configured=%s)", settings.llm_model, llm_configured())
    yield


app = FastAPI(title="SQL Query AI Agent", version="1.0.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=settings.max_input_chars)
    thread_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    execute: bool = True


# ---------------------------------------------------------------- simple per-IP rate limit

_hits: dict[str, deque] = defaultdict(deque)


def _rate_limit(request: Request) -> None:
    if RATE_LIMIT <= 0:
        return
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests — please wait a minute.")
    q.append(now)


def _inputs(req: ChatRequest) -> tuple[dict, dict]:
    inputs = {"messages": [HumanMessage(req.message)], "user_input": req.message, "execute": req.execute}
    config = {"configurable": {"thread_id": req.thread_id}, "recursion_limit": 40}
    return inputs, config


def _require_llm() -> None:
    if not llm_configured():
        raise HTTPException(503, f"No API key configured for model '{settings.llm_model}'. "
                                 "Set ANTHROPIC_API_KEY (or OPENAI_API_KEY / GROQ_API_KEY with LLM_MODEL).")


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model": settings.llm_model, "llm_configured": llm_configured()}


@app.get("/api/schema")
def schema() -> dict[str, Any]:
    cat = get_catalog()
    return {
        "dialect": "sqlite",
        "tables": [{
            "name": t.name, "row_count": t.row_count,
            "columns": [{"name": c.name, "type": c.type, "primary_key": c.primary_key, "not_null": c.not_null,
                         "values": c.sample_values} for c in t.columns],
            "foreign_keys": [{"column": f.column, "ref_table": f.ref_table, "ref_column": f.ref_column}
                             for f in t.foreign_keys],
            "indexes": t.indexes,
        } for t in cat.tables.values()],
        "ddl": settings.schema_path.read_text(encoding="utf-8"),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    _rate_limit(request)
    _require_llm()
    inputs, config = _inputs(req)
    state = await app.state.graph.ainvoke(inputs, config)
    return state["response"]


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
    _rate_limit(request)
    _require_llm()
    inputs, config = _inputs(req)
    graph = app.state.graph

    async def events() -> AsyncIterator[dict]:
        try:
            async for mode, chunk in graph.astream(inputs, config, stream_mode=["updates", "messages"]):
                if mode == "updates":
                    for node in chunk:
                        if node in STEP_LABELS:
                            yield {"event": "step", "data": json.dumps({"node": node, "label": STEP_LABELS[node]})}
                elif mode == "messages":
                    msg, meta = chunk
                    if meta.get("langgraph_node") != "explain_sql":
                        continue
                    text = _chunk_text(msg.content)
                    if text:
                        yield {"event": "token", "data": json.dumps({"text": text})}
            final = await graph.aget_state(config)
            yield {"event": "result", "data": json.dumps(final.values.get("response"), default=str)}
        except Exception as e:  # surface failures to the UI instead of dropping the stream
            log.exception("stream failed")
            yield {"event": "error", "data": json.dumps({"message": f"Server error: {type(e).__name__}"})}

    return EventSourceResponse(events())


def _chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Anthropic content blocks; skip thinking blocks
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


@app.get("/api/threads/{thread_id}")
async def thread(thread_id: str) -> dict[str, Any]:
    state = await app.state.graph.aget_state({"configurable": {"thread_id": thread_id}})
    values = state.values or {}
    return {"thread_id": thread_id, "last_sql": values.get("last_sql"),
            "turns": sum(1 for m in values.get("messages", []) if isinstance(m, HumanMessage))}


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")
