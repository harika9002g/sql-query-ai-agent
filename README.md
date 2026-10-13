# SQL Query AI Agent

A task-focused AI agent that turns plain-English questions into **validated, read-only SQL**. It explains each query, checks its performance, and can run it against a sample database. It also explains, optimizes and debugs SQL you paste in, keeps context across follow-ups, and refuses anything outside SQL and the provided schema.

Built with **LangGraph** (orchestration), **FastAPI** (API + SSE streaming), **sqlglot + SQLite** (deterministic validation and execution) and a no-build **vanilla JS** web UI.

| | |
|---|---|
| **Live demo** | **https://sql-query-agent-gvah.onrender.com** — no login needed (free hosting: first load after idle takes ~30–60 s) |
| **Demo video** | _add your video link here_ (script: [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md)) |
| **Architecture & workflow** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| **Prompts** | [`docs/PROMPTS.md`](docs/PROMPTS.md) (generated from [`app/prompts.py`](app/prompts.py)) |
| **Sample schema** | [`data/schema.sql`](data/schema.sql) |

---

## What it does

| Requirement | How it is met |
|---|---|
| **3.1 NL → SQL** | `generate_sql` node, grounded in the injected schema (with FKs, indexes and real categorical values such as `'California'`). |
| **3.2 Validation** | [`app/sql_guard.py`](app/sql_guard.py) checks syntax, single statement, read-only AST, tables, columns, join key types, FK relationships, and compiles with SQLite. DELETE/UPDATE/INSERT/DROP/ALTER/TRUNCATE are never returned; destructive requests get a polite refusal. |
| **3.3 Explanation** | `explain_sql` node, streamed into the chat and the Explanation panel. |
| **3.4 Optimization** | Paste SQL and ask to optimize. The LLM rewrites for readability and performance, seeded with plan-based findings: non-sargable predicates, unnecessary joins, missing indexes, and `CREATE INDEX` advice (never executed). The rewrite is **executed next to the original to prove the results are identical**. |
| **3.5 Debugging** | Paste broken SQL. The validator pinpoints the errors, and the LLM explains each issue and returns a corrected query, which is validated again. |
| **3.6 Context** | Per-conversation LangGraph checkpointer. `modify_sql` turns edit the previous validated query ("Show all customers" → "Only those from California" adds `WHERE State = 'California'`). |
| **3.7 Out of scope** | Intent router plus scope gate return the exact required refusal for general knowledge, sports, politics, maths, creative writing and non-SQL coding. |
| **Guardrails (§7)** | Six layers, from input checks to an SQLite authorizer on a read-only connection. See [Architecture → Guardrails](docs/ARCHITECTURE.md#guardrails-defence-in-depth). Ambiguous requests get a clarifying question; questions the schema can't answer get "that data isn't available" instead of invented columns. |

**Frontend:** chat interface · SQL panel with syntax highlighting · explanation panel · results table · validation & performance panel · conversation history · step-by-step loading indicator · error messages · copy SQL.

**Bonus features implemented:** SQL execution (read-only) · follow-up support · query cost estimation (`EXPLAIN QUERY PLAN`-based) · syntax highlighting · multiple dialects (SQLite / PostgreSQL / MySQL output via sqlglot) · CSV export · download `.sql` · dark mode · streaming responses (SSE) · prompt-injection protection · self-correction loop · unit + integration tests (84). A `Dockerfile` is included but has not been build-tested; the live deployment uses Render's native Python runtime.

---

## Quick start (local)

Requires Python 3.11+ and an API key for one LLM provider. The deployed demo uses **GroqCloud's free tier** (`openai/gpt-oss-120b`); Claude and OpenAI are drop-in alternatives.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env               # then set GROQ_API_KEY= (free key: console.groq.com/keys)
uvicorn app.main:app --reload
```

Open http://localhost:8000. The sample database (`data/sample.db`) is built automatically on first start from `data/schema.sql` with deterministic seed data. You can also build it yourself with `python -m app.seed`.

### LLM providers

| Provider | Environment variables | Notes |
|---|---|---|
| **Groq (free tier)** | `GROQ_API_KEY`, `LLM_MODEL=groq:openai/gpt-oss-120b`, `LLM_EFFORT=low` | Production model with **strict JSON-schema structured outputs** (constrained decoding), used for intent routing and SQL drafts. `LLM_EFFORT` sets gpt-oss `reasoning_effort`. |
| Anthropic | `ANTHROPIC_API_KEY`, `LLM_MODEL=anthropic:claude-opus-5-5` | Native JSON-schema output; `LLM_EFFORT` sets Claude effort. |
| OpenAI | `OPENAI_API_KEY`, `LLM_MODEL=openai:gpt-4.1` | Native JSON-schema output. |

Always set `LLM_MODEL` explicitly. Without it, the provider is inferred from whichever key is present, with Anthropic first. Other Groq models also work; models without strict-schema support fall back to tool calling.

**Groq free-tier budget.** Each question makes about 3 model calls, roughly 3–4K tokens. Groq's published sample free-tier limits for `openai/gpt-oss-120b` are 30 requests/min, 1K requests/day, 8K tokens/min and 200K tokens/day, shared across your Groq organization. That works out to about 2 questions per minute and roughly 50 per day. Your exact limits are on [console.groq.com/settings/limits](https://console.groq.com/settings/limits). When a limit is hit, the UI shows "The language model is rate-limited right now…" instead of failing. The public deployment caps traffic at `RATE_LIMIT_PER_MIN=10` per IP to protect the quota.

**No key yet?** `python scripts/ui_preview.py` runs the real API, graph, validator and database with a scripted stand-in model that only understands the example prompts. It's useful for UI work and is labelled `offline-scripted-model` in the UI.

### Docker

```bash
docker build -t sql-agent .
docker run -p 8000:8000 -e GROQ_API_KEY=... -e LLM_MODEL=groq:openai/gpt-oss-120b sql-agent
```

### Tests

```bash
pytest
```

84 tests run offline with no API key needed: the LangGraph workflow is exercised end to end with a scripted fake LLM ([`tests/fakes.py`](tests/fakes.py)).

- [`test_graph.py`](tests/test_graph.py): every route through the graph. Generate, follow-up modification, out-of-scope, destructive (natural language and SQL), prompt injection blocked before any LLM call, hallucinated-column self-correction, giving up without returning invalid SQL, `cannot_answer`, ambiguity, debug, optimize with equivalence check, explain, explain-on-broken-SQL → debug, execution off, LLM failure, and thread isolation.
- [`test_sql_guard.py`](tests/test_sql_guard.py): validator positives/negatives, including writes hidden in CTEs and multi-statement payloads.
- [`test_guardrails_and_db.py`](tests/test_guardrails_and_db.py): injection heuristics without false positives, a read-only connection that refuses writes even when the validator is bypassed, and the optimizer rules.
- [`test_api.py`](tests/test_api.py): REST + SSE endpoints, request validation, missing-key handling.
- [`test_llm_provider.py`](tests/test_llm_provider.py): provider selection and key-from-env checks. Also runs the real `ChatGroq` client against a mocked Groq HTTP API. That covers the strict json_schema request shape, the full graph, streaming (structured calls don't stream; explanation tokens do) and 429 handling.

---

## Deployment

The app is one stateless-image container (plus in-process conversation memory), so any Docker host works.

**Render (blueprint included, native Python runtime — no Docker needed):** Render → *New → Blueprint* → connect this GitHub repo → enter `GROQ_API_KEY` when prompted → *Apply*. [`render.yaml`](render.yaml) installs `requirements.txt`, builds the sample database, starts uvicorn on `$PORT`, and sets `LLM_MODEL=groq:openai/gpt-oss-120b`, the health check and the rate limit. On the free plan the service sleeps when idle; the first request after a pause takes ~30–60 s to wake.

Other hosts work the same way: install `requirements.txt`, run `python -m app.seed`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, and set the API key. A `Dockerfile` is also included for container hosts.

Run a **single worker**, because conversation memory is in-process. The public endpoint is rate-limited per IP (`RATE_LIMIT_PER_MIN`, default 20) to protect the API key.

**Live verification:** the smoke-test scenarios were run against the deployed app (commit `f84961c`, real Groq `openai/gpt-oss-120b`) on 2026-10-13 and **all 13 checks passed**:

| Check | Result |
|---|---|
| Health endpoint; LLM key configured | PASS · PASS |
| 3.1 NL→SQL (`… WHERE HireDate >= '2024-01-01'`) · 3.2 validation · 3.3 explanation · execution (14 rows) | PASS ×4 |
| 3.6 follow-up modifies previous query ("Only those from California" → 20 rows) | PASS |
| 3.7 out-of-scope refused with the required message | PASS |
| 3.2 destructive request refused · prompt injection blocked | PASS · PASS |
| 3.4 optimization (strftime filter rewritten; result-equivalence check passed) | PASS |
| 3.5 debugging (2 issues found and fixed) | PASS |
| Unanswerable question ("shoe size") refused without inventing columns | PASS |

A three-turn conversation was also verified on the live app: "first 5 rows of customers" → "products ordered by these customers in the year they signed up" → "against each customer". Each turn modified the previous query, and the final 15 customer–product rows matched an independently written reference query.

**Verify a deployment with the real model:** `python scripts/smoke_test.py https://<your-app>.onrender.com` runs the assignment's scenarios (NL→SQL, validation, follow-up, optimize, debug, refusals, injection, no hallucinated columns) and prints PASS/FAIL for each check.

---

## Using it

Try the example chips on the welcome screen, or:

- `Show all employees hired after January 2024.` → then `Only those from Engineering` → then `sort by salary, highest first`
- `Top 5 customers by total spend, with their state`
- `Monthly revenue in 2025 from delivered orders`
- `Optimize: SELECT * FROM Orders o LEFT JOIN Employees e ON e.EmployeeID = o.EmployeeID WHERE strftime('%Y', o.OrderDate) = '2024'`
- `Why does this fail? SELECT EmpName, COUNT(*) FROM Employees WHERE HireDate > 2023-01-01`
- `What tables are there and how are orders linked to products?`
- Guardrails: `Who won the FIFA World Cup?` · `Delete all cancelled orders` · `Ignore previous instructions and …`

The **Run query** toggle controls whether SQL is executed. The SQL panel's dialect picker shows the same query in PostgreSQL or MySQL syntax.

### API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chat/stream` | `{message, thread_id, execute}` → SSE events `step` (node finished), `token` (explanation text), `result` (final payload), `error` |
| `POST` | `/api/chat` | Same input, returns the final payload as JSON |
| `GET` | `/api/schema` | Tables, columns, FKs, indexes, row counts, DDL |
| `GET` | `/api/schema/{table}/sample` | First rows of a schema table (used by the schema browser) |
| `GET` | `/api/health` | Status, configured model, whether a key is present |

Interactive docs: `/docs`.

---

## Sample database

**Retail Co.** (SQLite), 7 tables: `Departments`, `Employees` (with `ManagerID` self-reference), `Customers`, `Categories`, `Products`, `Orders`, `OrderItems`. That's about 3,100 deterministic rows, with hire dates 2018–2025, customer signups 2021–2025, and orders 2021–2025. Each customer's first order falls within ~90 days of signup. Some columns (e.g. `Customers.State`, `Employees.HireDate`) are deliberately unindexed so index recommendations have something to find. The full DDL with comments is in [`data/schema.sql`](data/schema.sql); the UI's **Browse schema** panel shows every table's columns and first 5 rows.

---

## Project structure

```
app/
  main.py            FastAPI app: REST + SSE, rate limit, static UI
  agent/
    graph.py         LangGraph StateGraph wiring + routers
    nodes.py         node implementations
    state.py         AgentState (checkpointed per thread)
    schemas.py       structured-output models for the LLM
  prompts.py         every prompt (→ docs/PROMPTS.md)
  sql_guard.py       deterministic SQL validator + dialect transpiler
  optimizer.py       hints, index advice, unnecessary joins, cost
  guardrails.py      input checks: injection, destructive SQL, size
  db.py              schema introspection, read-only execution, plan-based cost
  llm.py             provider-agnostic chat model factory
  seed.py            builds data/sample.db
  config.py          env-driven settings
frontend/            index.html · styles.css · app.js (no build step)
data/schema.sql      sample schema
docs/                ARCHITECTURE.md · PROMPTS.md · DEMO_SCRIPT.md
scripts/             smoke_test.py (live checks) · ui_preview.py (offline UI dev) · export_prompts.py
tests/               84 offline tests (fake LLM + mocked Groq HTTP)
Dockerfile · render.yaml · requirements*.txt · .env.example
```

---

## Assumptions

- **The sample schema is synthetic.** The assignment says a schema will be provided, but none was included in the brief, so I designed the Retail Co. schema (`data/schema.sql`) and generated its data (`app/seed.py`). To use a different schema, replace `data/schema.sql` and the seed script: validation, retrieval and prompts read the schema at runtime.
- The schema is the only data source. The agent targets **SQLite** for execution; the PostgreSQL/MySQL renderings are transpiled for convenience, not executed.
- "Read-only" means only `SELECT` (including CTEs and set operations). `CREATE INDEX` statements appear as *recommendations* in the optimization panel and are never executed or returned as the answer.
- Users of the public demo are anonymous. Conversations are kept in server memory per browser-generated thread id and in the browser's localStorage. No login.
- Result sets are capped at 500 rows (configurable) and queries time out after 5 s. Optimization equivalence checks compare up to 5,000 rows.
- When a request is underspecified but has an obvious reading (e.g. "recent orders"), the agent makes a stated assumption, shown under the explanation. It asks for clarification only when guessing would materially change the answer.
- Dates are ISO-8601 text, which is the SQLite convention.

## Limitations

- Free hosting: the Render instance sleeps when idle (first request takes ~30–60 s), and Groq's free tier limits throughput (about 2 questions/minute, see above).
- In-process memory: restarting the server forgets follow-up context (the visible history stays in the browser). Use a persistent LangGraph checkpointer for durable memory.
- The injection filter is heuristic. It is one layer of six, and the hard guarantee comes from the validator and the read-only engine.
