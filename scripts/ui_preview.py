"""Front-end development server that needs NO API key.

Runs the real FastAPI app and the real LangGraph workflow (guardrails, validation, optimization,
execution), but swaps the LLM for a tiny scripted stand-in that only understands the example
prompts. Use it to work on the UI offline:   python scripts/ui_preview.py   → http://localhost:8000

This is NOT the agent — answers to anything other than the examples will be generic.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ["LLM_MODEL"] = "preview:offline-scripted-model"  # shown in the UI badge so it's never mistaken for the agent

import uvicorn  # noqa: E402

import app.main as main  # noqa: E402
from app.agent.graph import build_graph  # noqa: E402
from fakes import FakeLLM, user_request  # noqa: E402

CANNED = [
    (r"hired after january 2024", "SELECT * FROM Employees WHERE HireDate >= '2024-01-01'"),
    (r"top 5 customers", "SELECT c.FirstName, c.LastName, c.State, SUM(o.TotalAmount) AS TotalSpend FROM Customers c "
                         "JOIN Orders o ON o.CustomerID = c.CustomerID GROUP BY c.CustomerID ORDER BY TotalSpend DESC LIMIT 5"),
    (r"^show all customers", "SELECT * FROM Customers"),
    (r"monthly revenue", "SELECT strftime('%Y-%m', OrderDate) AS Month, ROUND(SUM(TotalAmount), 2) AS Revenue FROM Orders "
                         "WHERE Status = 'Delivered' AND OrderDate >= '2025-01-01' AND OrderDate < '2026-01-01' "
                         "GROUP BY Month ORDER BY Month"),
]


def intent(msgs):
    q = user_request(msgs).lower()
    has_prev = "<previous_sql></previous_sql>" not in msgs[-1].content
    if re.search(r"world cup|weather|poem|capital of", q):
        return {"intent": "out_of_scope", "reason": "preview"}
    if re.search(r"\b(delete|remove|drop|update)\b", q):
        return {"intent": "destructive", "reason": "preview"}
    if q.startswith("optimize"):
        return {"intent": "optimize_sql", "reason": "preview"}
    if q.startswith("why does") or "fix" in q:
        return {"intent": "debug_sql", "reason": "preview"}
    if q.startswith("explain"):
        return {"intent": "explain_sql", "reason": "preview"}
    if re.match(r"^(hi|hello|help|what can you do)", q):
        return {"intent": "help", "reason": "preview"}
    if has_prev and re.match(r"^(only|now|and|sort|also|just)", q):
        return {"intent": "modify_sql", "reason": "preview"}
    return {"intent": "generate_sql", "reason": "preview"}


def draft(msgs):
    q = user_request(msgs).lower()
    prev = re.search(r"<previous_sql>(.*?)</previous_sql>", msgs[-1].content, re.S).group(1).strip()
    if prev and "california" in q:
        return {"status": "ok", "sql": prev.replace("FROM Customers", "FROM Customers WHERE State = 'California'")}
    for pattern, sql in CANNED:
        if re.search(pattern, q):
            return {"status": "ok", "sql": sql}
    return {"status": "needs_clarification",
            "clarification_question": "(UI preview mode) Try one of the example prompts — the offline stand-in model only knows those."}


def fix(msgs):
    return {"issues": [
        {"problem": "Column EmpName does not exist", "explanation": "Employees stores names in FirstName and LastName."},
        {"problem": "Date literal is not quoted", "explanation": "2023-01-01 is parsed as arithmetic (2023 − 1 − 1); dates must be quoted strings."},
        {"problem": "COUNT(*) without GROUP BY", "explanation": "Mixing an aggregate with a plain column needs a GROUP BY."}],
        "corrected_sql": "SELECT FirstName, LastName, COUNT(*) AS Hires FROM Employees WHERE HireDate > '2023-01-01' GROUP BY FirstName, LastName"}


def optimize(msgs):
    return {"optimized_sql": "SELECT o.OrderID, o.CustomerID, o.EmployeeID, o.OrderDate, o.ShippedDate, o.Status, o.TotalAmount "
                             "FROM Orders AS o WHERE o.OrderDate >= '2024-01-01' AND o.OrderDate < '2025-01-01'",
            "improvements": ["Replaced strftime() on OrderDate with a date range so idx_orders_orderdate can be used.",
                             "Listed columns explicitly instead of SELECT *."],
            "removed_joins": ["Removed LEFT JOIN Employees — its columns were not needed."],
            "index_recommendations": []}


def explain(msgs):
    body = msgs[-1].content
    if "Mode: debug" in body:
        return "The corrected query lists each employee hired after 1 January 2023 along with a count, grouping by name so the aggregate is valid."
    if "Mode: optimize" in body:
        return "The optimized query returns 2024 orders using a date range on OrderDate, which lets SQLite use its index instead of scanning every order."
    if "Mode: explain" in body:
        return "For each state, this calculates the average order value of its customers, keeping only states with more than 50 orders."
    return "This query retrieves the requested rows from the Retail Co. database, applying the filters described in your question."


def schema_answer(msgs):
    return "The database has Departments, Employees, Customers, Categories, Products, Orders and OrderItems."


if __name__ == "__main__":
    llm = FakeLLM({"IntentDecision": intent, "SQLDraft": draft, "SQLFix": fix, "SQLOptimization": optimize},
                  text=lambda m: explain(m) if "Mode:" in m[-1].content else schema_answer(m))
    main.build_graph = lambda: build_graph(llm)
    main.llm_configured = lambda: True
    uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
