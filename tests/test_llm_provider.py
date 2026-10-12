"""Provider wiring tests — no API key and no network.

The Groq tests use the real `ChatGroq` client with an httpx MockTransport standing in for
https://api.groq.com, so they check the exact request we send (model, strict json_schema
response_format, reasoning_effort, no tools) and that responses parse through the real graph.
"""
import dataclasses
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq

import app.llm as llm_mod
from app.agent.graph import build_graph
from app.agent.schemas import IntentDecision, SQLDraft


@pytest.fixture()
def use_model(monkeypatch):
    def _set(model: str, **env):
        monkeypatch.setattr(llm_mod, "settings", dataclasses.replace(llm_mod.settings, llm_model=model, llm_effort="low"))
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        llm_mod.get_chat_model.cache_clear()
    yield _set
    llm_mod.get_chat_model.cache_clear()


def test_groq_selected_and_key_read_from_env(use_model):
    use_model("groq:openai/gpt-oss-120b")
    assert llm_mod.provider() == "groq" and llm_mod.model_name() == "openai/gpt-oss-120b"
    assert llm_mod.llm_configured() is False            # no GROQ_API_KEY yet
    use_model("groq:openai/gpt-oss-120b", GROQ_API_KEY="test-not-a-real-key")
    assert llm_mod.llm_configured() is True


def test_groq_model_construction(use_model):
    use_model("groq:openai/gpt-oss-120b", GROQ_API_KEY="test-not-a-real-key")
    kwargs = llm_mod.model_kwargs()
    assert kwargs["reasoning_effort"] == "low" and kwargs["temperature"] == 0 and kwargs["max_tokens"] == 4096
    model = llm_mod.get_chat_model()
    assert isinstance(model, ChatGroq)
    assert model.model_name == "openai/gpt-oss-120b" and model.reasoning_effort == "low"
    assert model.groq_api_key.get_secret_value() == "test-not-a-real-key"   # read from env, not hardcoded


def test_groq_uses_strict_json_schema(use_model):
    use_model("groq:openai/gpt-oss-120b", GROQ_API_KEY="test-not-a-real-key")
    bound = llm_mod.structured(llm_mod.get_chat_model(), SQLDraft).first
    rf = bound.kwargs["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    schema = rf["json_schema"]["schema"]
    # Groq strict mode: every property required, no additional properties.
    assert set(schema["required"]) == set(schema["properties"]) and schema["additionalProperties"] is False


def test_non_strict_groq_model_falls_back_to_tool_calling(use_model):
    use_model("groq:llama-3.1-8b-instant", GROQ_API_KEY="test-not-a-real-key")
    assert "reasoning_effort" not in llm_mod.model_kwargs()
    bound = llm_mod.structured(llm_mod.get_chat_model(), SQLDraft).first
    assert "tools" in bound.kwargs and "response_format" not in bound.kwargs


def test_anthropic_path_unchanged(use_model):
    use_model("anthropic:claude-opus-5-5", ANTHROPIC_API_KEY="test-not-a-real-key")
    kwargs = llm_mod.model_kwargs()
    assert kwargs["effort"] == "low" and "temperature" not in kwargs


# ---------------------------------------------------------------- full graph over mocked Groq HTTP

def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def test_full_graph_through_real_chatgroq_client():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        name = (body.get("response_format") or {}).get("json_schema", {}).get("name")
        if name == "IntentDecision":
            out = IntentDecision(intent="generate_sql", reason="data question").model_dump()
        elif name == "SQLDraft":
            out = SQLDraft(status="ok", sql="SELECT FirstName, State FROM Customers WHERE State = 'California'").model_dump()
        else:  # explanation (plain text)
            return httpx.Response(200, json=_completion("Lists customers located in California."))
        return httpx.Response(200, json=_completion(json.dumps(out)))

    llm = ChatGroq(model="openai/gpt-oss-120b", api_key="test-not-a-real-key", temperature=0, max_tokens=4096,
                   reasoning_effort="low", max_retries=0,
                   http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    graph = build_graph(llm)
    text = "Customers in California"
    state = graph.invoke({"messages": [HumanMessage(text)], "user_input": text, "execute": True},
                         {"configurable": {"thread_id": "groq-mock"}})
    r = state["response"]

    assert r["status"] == "ok" and r["validation"]["ok"]
    assert r["results"]["row_count"] == 20
    assert r["explanation"] == "Lists customers located in California."
    # Request shape actually sent to Groq
    assert [b.get("response_format", {}).get("json_schema", {}).get("name") for b in requests] == \
        ["IntentDecision", "SQLDraft", None]
    for b in requests:
        assert b["model"] == "openai/gpt-oss-120b" and b["reasoning_effort"] == "low"
        assert "tools" not in b and "tool_choice" not in b
    assert all(b["response_format"]["json_schema"]["strict"] is True for b in requests[:2])


def test_groq_rate_limit_becomes_friendly_error():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "Rate limit reached for model openai/gpt-oss-120b "
                                                   "on tokens per minute (TPM)", "type": "tokens", "code": "rate_limit_exceeded"}})

    llm = ChatGroq(model="openai/gpt-oss-120b", api_key="test-not-a-real-key", max_retries=0,
                   http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    state = build_graph(llm).invoke({"messages": [HumanMessage("show orders")], "user_input": "show orders"},
                                    {"configurable": {"thread_id": "groq-429"}})
    r = state["response"]
    assert r["status"] == "error" and "rate-limited" in r["message"]


def test_streaming_api_path_with_groq():
    """/api/chat/stream path: structured calls must NOT stream (Groq disallows it); explanation tokens do."""
    import asyncio

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = (body.get("response_format") or {}).get("json_schema", {}).get("name")
        seen.append((name, bool(body.get("stream"))))
        if name == "IntentDecision":
            return httpx.Response(200, json=_completion(json.dumps(
                IntentDecision(intent="generate_sql", reason="r").model_dump())))
        if name == "SQLDraft":
            return httpx.Response(200, json=_completion(json.dumps(
                SQLDraft(status="ok", sql="SELECT COUNT(*) AS n FROM Orders").model_dump())))
        chunks = ["Counts ", "all orders."]
        sse = "".join("data: " + json.dumps({
            "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]}) + "\n\n" for c in chunks)
        sse += "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0,
                                      "model": "openai/gpt-oss-120b",
                                      "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
        sse += "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    llm = ChatGroq(model="openai/gpt-oss-120b", api_key="test-not-a-real-key", max_retries=0,
                   http_client=httpx.Client(transport=transport), http_async_client=httpx.AsyncClient(transport=transport))
    graph = build_graph(llm)
    cfg = {"configurable": {"thread_id": "groq-stream"}}

    async def run():
        tokens = []
        async for mode, chunk in graph.astream({"messages": [HumanMessage("how many orders")], "user_input": "how many orders"},
                                               cfg, stream_mode=["updates", "messages"]):
            if mode == "messages" and chunk[1].get("langgraph_node") == "explain_sql" and chunk[0].content:
                tokens.append(chunk[0].content)
        return tokens, (await graph.aget_state(cfg)).values["response"]

    tokens, r = asyncio.run(run())
    assert r["status"] == "ok" and r["results"]["rows"] == [[800]]
    assert seen[0] == ("IntentDecision", False) and seen[1] == ("SQLDraft", False)
    assert "".join(tokens) == "Counts all orders." and r["explanation"] == "Counts all orders."
