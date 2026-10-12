"""API tests: FastAPI app with the real graph wired to a fake LLM."""
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.graph import build_graph
from fakes import FakeLLM


@pytest.fixture()
def client(monkeypatch):
    llm = FakeLLM({
        "IntentDecision": lambda m: {"intent": "generate_sql", "reason": "t"},
        "SQLDraft": lambda m: {"status": "ok", "sql": "SELECT FirstName, State FROM Customers WHERE State = 'Texas'"},
    }, text=lambda m: "Lists customers in Texas.")
    monkeypatch.setattr(main, "build_graph", lambda: build_graph(llm))
    monkeypatch.setattr(main, "llm_configured", lambda: True)
    monkeypatch.setattr(main, "RATE_LIMIT", 0)
    with TestClient(main.app) as c:
        yield c


def test_health_and_index(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert "SQL" in client.get("/").text


def test_schema_endpoint(client):
    data = client.get("/api/schema").json()
    assert data["dialect"] == "sqlite"
    assert any(t["name"] == "Orders" for t in data["tables"])
    assert "CREATE TABLE Employees" in data["ddl"]


def test_chat_json(client):
    r = client.post("/api/chat", json={"message": "customers in texas", "thread_id": "api-1"}).json()
    assert r["status"] == "ok" and r["results"]["row_count"] > 0


def test_chat_stream_emits_steps_and_result(client):
    with client.stream("POST", "/api/chat/stream", json={"message": "customers in texas", "thread_id": "api-2"}) as r:
        body = "".join(r.iter_text())
    events = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert "step" in events and events[-1] == "result"
    data_lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    result = json.loads(data_lines[-1])
    assert result["status"] == "ok" and "Texas" in result["sql"]


def test_request_validation(client):
    assert client.post("/api/chat", json={"message": "", "thread_id": "x"}).status_code == 422
    assert client.post("/api/chat", json={"message": "hi", "thread_id": "../etc"}).status_code == 422


def test_missing_api_key_returns_503(client, monkeypatch):
    monkeypatch.setattr(main, "llm_configured", lambda: False)
    r = client.post("/api/chat", json={"message": "hi", "thread_id": "x"})
    assert r.status_code == 503 and "API key" in r.json()["detail"]


def test_table_sample_endpoint(client):
    data = client.get("/api/schema/Customers/sample").json()
    assert data["table"] == "Customers" and data["row_count"] == 200
    assert "State" in data["columns"] and len(data["rows"]) == 5
    assert client.get("/api/schema/customers/sample").status_code == 200          # case-insensitive
    assert client.get("/api/schema/sqlite_master/sample").status_code == 404     # only schema tables
    assert client.get('/api/schema/Orders";DROP TABLE Orders;--/sample').status_code == 404
