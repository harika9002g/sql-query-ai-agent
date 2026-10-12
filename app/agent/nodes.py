"""Graph nodes. Each takes the state and returns a partial update.

LLM nodes: detect_intent, generate_sql, explain_sql, answer_schema_question.
Deterministic nodes: guard_input, validate_scope, analyze_user_sql, retrieve_schema, validate_sql,
optimize_sql, execute_sql, respond.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app import prompts
from app.agent.schemas import IntentDecision, SQLDraft, SQLFix, SQLOptimization
from app.agent.state import AgentState
from app.config import settings
from app.db import SchemaCatalog, execute_readonly
from app.guardrails import REFUSAL_DESTRUCTIVE, REFUSAL_OUT_OF_SCOPE, check_input
from app.llm import structured
from app.optimizer import analyze, drop_existing_indexes
from app.sql_guard import clean_sql, extract_sql, pretty, transpile, validate_sql

log = logging.getLogger(__name__)

# Human-readable step labels (streamed to the UI as progress).
STEP_LABELS = {
    "guard_input": "Checking input guardrails",
    "detect_intent": "Detecting intent",
    "validate_scope": "Validating scope",
    "analyze_user_sql": "Analyzing your SQL",
    "retrieve_schema": "Retrieving relevant schema",
    "generate_sql": "Generating SQL",
    "validate_sql": "Validating SQL against schema",
    "optimize_sql": "Checking performance & cost",
    "execute_sql": "Running query (read-only)",
    "explain_sql": "Writing explanation",
    "answer_schema_question": "Reading the schema",
    "respond": "Preparing response",
}

_SYNONYMS = {
    "staff": "employees", "worker": "employees", "hired": "hiredate", "hire": "hiredate", "manager": "employees",
    "salary": "employees", "paid": "employees", "client": "customers", "buyer": "customers", "user": "customers",
    "state": "customers", "city": "customers", "california": "customers", "revenue": "orders", "sale": "orders",
    "purchase": "orders", "spent": "orders", "spend": "orders", "bought": "orderitems", "sold": "orderitems",
    "quantity": "orderitems", "item": "orderitems", "stock": "products", "inventory": "products",
    "price": "products", "category": "categories", "team": "departments", "dept": "departments",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}


def _history(state: AgentState, turns: int | None = None) -> str:
    """Compact transcript of previous turns (excluding the current user message)."""
    msgs = state.get("messages", [])[:-1]
    msgs = msgs[-2 * (turns or settings.history_turns):]
    lines = []
    for m in msgs:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        content = m.content if isinstance(m.content, str) else str(m.content)
        lines.append(f"{role}: {content[:800]}")
    return "\n".join(lines) or "(none)"


class Nodes:
    def __init__(self, llm: Any, catalog: SchemaCatalog):
        self.llm = llm
        self.catalog = catalog

    # ------------------------------------------------------------------ guardrails / routing

    def guard_input(self, state: AgentState) -> dict:
        reset = {
            "trace": None, "status": None, "message": None, "intent": None, "intent_reason": None,
            "mode": None, "user_sql": None, "user_sql_validation": None, "relevant_tables": [],
            "schema_context": None, "sql": None, "attempts": 0, "validation": None, "notes": [],
            "issues": [], "llm_index_recommendations": [], "optimization": None, "results": None,
            "equivalence": None, "explanation": None, "error": None, "response": None,
        }
        result = check_input(state.get("user_input", ""))
        if result.blocked:
            reset.update(status="rejected", message=result.message,
                         intent="destructive" if result.reason == "destructive" else result.reason)
        else:
            reset["user_sql"] = result.user_sql
        return reset

    def detect_intent(self, state: AgentState) -> dict:
        table_list = ", ".join(t.name for t in self.catalog.tables.values())
        messages = [
            SystemMessage(prompts.INTENT_SYSTEM.format(table_list=table_list)),
            HumanMessage(prompts.INTENT_USER.format(
                last_sql=state.get("last_sql") or "", history=_history(state), question=state["user_input"])),
        ]
        decision: IntentDecision = structured(self.llm, IntentDecision).invoke(messages)
        user_sql = clean_sql(decision.user_sql) if decision.user_sql else state.get("user_sql")
        return {"intent": decision.intent, "intent_reason": decision.reason, "user_sql": user_sql,
                "message": decision.clarification_question}

    def validate_scope(self, state: AgentState) -> dict:
        """Turn the intent into a scope decision. Out-of-scope and destructive requests stop here."""
        intent = state.get("intent")
        if intent == "out_of_scope":
            return {"status": "rejected", "message": REFUSAL_OUT_OF_SCOPE}
        if intent == "destructive":
            return {"status": "rejected", "message": REFUSAL_DESTRUCTIVE}
        if intent == "ambiguous":
            return {"status": "clarify", "message": state.get("message") or
                    "Could you clarify what you'd like to see — which data, filters, or time period?"}
        if intent == "help":
            tables = ", ".join(t.name for t in self.catalog.tables.values())
            return {"status": "info", "message": prompts.HELP_MESSAGE.format(tables=tables)}
        if intent in ("explain_sql", "optimize_sql", "debug_sql"):
            user_sql = state.get("user_sql") or extract_sql(state.get("user_input", ""))
            if not user_sql and state.get("last_sql"):
                user_sql = state["last_sql"]  # "explain that" / "optimize it" → the previous query
            if not user_sql:
                return {"status": "clarify", "message": "Please paste the SQL query you'd like me to "
                        f"{intent.split('_')[0]}."}
            return {"mode": intent.split("_")[0], "user_sql": user_sql}
        if intent == "modify_sql" and state.get("last_sql"):
            return {"mode": "modify"}
        if intent == "schema_question":
            return {"mode": "schema"}
        return {"mode": "generate"}

    # ------------------------------------------------------------------ user-supplied SQL

    def analyze_user_sql(self, state: AgentState) -> dict:
        report = validate_sql(state["user_sql"], self.catalog)
        if report.destructive:
            return {"status": "rejected", "message": REFUSAL_DESTRUCTIVE, "user_sql_validation": report.to_dict()}
        update: dict[str, Any] = {"user_sql_validation": report.to_dict()}
        mode = state.get("mode")
        if not report.ok and mode in ("explain", "optimize"):
            # Broken SQL can't be explained or optimized faithfully — fix it first.
            update["mode"] = "debug"
            update["notes"] = [f"Your query has errors, so I fixed it before trying to {mode} it."]
        if report.ok and mode == "explain":
            update.update(sql=pretty(report.sql), validation=report.to_dict())
        return update

    # ------------------------------------------------------------------ schema retrieval

    def retrieve_schema(self, state: AgentState) -> dict:
        """Score tables by lexical overlap (+synonyms) with the request and previous SQL, expand along FKs."""
        text = " ".join(filter(None, [state.get("user_input"), state.get("user_sql"),
                                      state.get("last_sql") if state.get("mode") == "modify" else None]))
        toks = _tokens(text)
        toks |= {_SYNONYMS[t] for t in toks if t in _SYNONYMS}
        scores: dict[str, int] = {}
        for key, table in self.catalog.tables.items():
            score = 3 * len(_tokens(table.name) & toks) + (3 if key in toks else 0)
            score += sum(1 for c in table.columns if c.name.lower() in toks or _tokens(c.name) <= toks)
            if score:
                scores[table.name] = score
        relevant = sorted(scores, key=scores.get, reverse=True)
        # FK neighbours of relevant tables are needed to write correct joins.
        for name in list(relevant):
            for fk in self.catalog.table(name).foreign_keys:
                if fk.ref_table not in relevant:
                    relevant.append(fk.ref_table)
        # Small schemas: send everything (cheap, and avoids missing a join path); large: only relevant tables.
        if len(self.catalog.tables) <= 12 or not relevant:
            context = self.catalog.to_prompt()
        else:
            context = self.catalog.to_prompt(relevant)
        return {"relevant_tables": relevant, "schema_context": context}

    # ------------------------------------------------------------------ SQL production (LLM)

    def generate_sql(self, state: AgentState) -> dict:
        mode = state.get("mode") or "generate"
        attempts = state.get("attempts", 0) + 1
        feedback = ""
        if state.get("validation") and not state["validation"].get("ok"):
            feedback = prompts.RETRY_FEEDBACK.format(sql=state.get("sql") or "",
                                                     errors="\n".join(state["validation"]["errors"]))
        schema = state.get("schema_context") or self.catalog.to_prompt()

        if mode == "debug":
            uv = state.get("user_sql_validation") or {}
            findings = "\n".join(uv.get("errors", []) + uv.get("warnings", [])) or "(none detected automatically)"
            fix: SQLFix = structured(self.llm, SQLFix).invoke([
                SystemMessage(prompts.DEBUG_SYSTEM.format(schema=schema)),
                HumanMessage(prompts.DEBUG_USER.format(question=state["user_input"], user_sql=state["user_sql"],
                                                       findings=findings, feedback=feedback)),
            ])
            return {"sql": clean_sql(fix.corrected_sql), "attempts": attempts,
                    "issues": [i.model_dump() for i in fix.issues]}

        if mode == "optimize":
            pre = analyze(state["user_sql"], self.catalog)
            findings = "\n".join(pre["hints"] + pre["unnecessary_joins"] +
                                 [f"Suggested index: {i}" for i in pre["index_recommendations"]] +
                                 [f"Query plan: {' | '.join(pre['cost'].get('plan', []))}"])
            opt: SQLOptimization = structured(self.llm, SQLOptimization).invoke([
                SystemMessage(prompts.OPTIMIZE_SYSTEM.format(schema=schema)),
                HumanMessage(prompts.OPTIMIZE_USER.format(question=state["user_input"], user_sql=state["user_sql"],
                                                          findings=findings, feedback=feedback)),
            ])
            return {"sql": clean_sql(opt.optimized_sql), "attempts": attempts,
                    "notes": opt.improvements + opt.removed_joins,
                    "llm_index_recommendations": opt.index_recommendations}

        draft: SQLDraft = structured(self.llm, SQLDraft).invoke([
            SystemMessage(prompts.GENERATE_SYSTEM.format(schema=schema)),
            HumanMessage(prompts.GENERATE_USER.format(
                last_sql=state.get("last_sql") if mode == "modify" else "", history=_history(state),
                feedback=feedback, question=state["user_input"])),
        ])
        if draft.status == "needs_clarification" or (draft.status == "ok" and not draft.sql):
            return {"status": "clarify", "attempts": attempts,
                    "message": draft.clarification_question or "Could you give a bit more detail about what you need?"}
        if draft.status == "cannot_answer":
            tables = ", ".join(t.name for t in self.catalog.tables.values())
            return {"status": "clarify", "attempts": attempts,
                    "message": f"I can't answer that from this database. {draft.reason or ''} "
                               f"The available tables are: {tables}.".replace("  ", " ")}
        return {"sql": clean_sql(draft.sql), "attempts": attempts, "notes": draft.assumptions}

    # ------------------------------------------------------------------ validation / optimization / execution

    def validate_sql(self, state: AgentState) -> dict:
        report = validate_sql(state.get("sql") or "", self.catalog)
        update: dict[str, Any] = {"validation": report.to_dict()}
        if report.ok:
            update["sql"] = pretty(report.sql)
        return update

    def optimize_sql(self, state: AgentState) -> dict:
        analysis = analyze(state["sql"], self.catalog)
        llm_recs = state.get("llm_index_recommendations") or []
        analysis["index_recommendations"] = drop_existing_indexes(
            list(dict.fromkeys(llm_recs + analysis["index_recommendations"])), self.catalog)
        return {"optimization": analysis}

    def execute_sql(self, state: AgentState) -> dict:
        update: dict[str, Any] = {}
        try:
            res = execute_readonly(state["sql"])
            update["results"] = {"columns": res.columns, "rows": res.rows, "row_count": res.row_count,
                                 "truncated": res.truncated, "elapsed_ms": res.elapsed_ms, "error": None}
        except sqlite3.Error as e:
            update["results"] = {"columns": [], "rows": [], "row_count": 0, "truncated": False,
                                 "elapsed_ms": 0, "error": str(e)}
            return update
        # Optimization must not change semantics: compare against the original query's results.
        uv = state.get("user_sql_validation") or {}
        if state.get("mode") == "optimize" and uv.get("ok"):
            try:
                orig = execute_readonly(state["user_sql"], max_rows=5000)
                new = execute_readonly(state["sql"], max_rows=5000)
                ordered = bool(re.search(r"\border\s+by\b", state["user_sql"], re.I))
                a, b = ([tuple(map(repr, r)) for r in x.rows] for x in (orig, new))
                same = (a == b) if ordered else (sorted(a) == sorted(b))
                update["equivalence"] = {
                    "same_results": same and len(orig.columns) == len(new.columns),
                    "original_rows": orig.row_count, "optimized_rows": new.row_count,
                    "original_columns": len(orig.columns), "optimized_columns": len(new.columns),
                    "original_ms": orig.elapsed_ms, "optimized_ms": new.elapsed_ms,
                }
            except sqlite3.Error as e:
                update["equivalence"] = {"same_results": None, "error": str(e)}
        return update

    # ------------------------------------------------------------------ explanation / schema Q&A (LLM)

    def explain_sql(self, state: AgentState) -> dict:
        res = state.get("results")
        if res is None:
            summary = "not executed"
        elif res.get("error"):
            summary = f"execution failed: {res['error']}"
        else:
            summary = f"{res['row_count']} row(s){' (truncated)' if res['truncated'] else ''}"
        notes = list(state.get("notes") or [])
        notes += [f"Fixed: {i['problem']}" for i in state.get("issues") or []]
        try:
            msg = self.llm.invoke([
                SystemMessage(prompts.EXPLAIN_SYSTEM),
                HumanMessage(prompts.EXPLAIN_USER.format(
                    question=state["user_input"], mode=state.get("mode"), sql=state["sql"],
                    notes="; ".join(notes) or "none", result_summary=summary)),
            ])
            text = msg.content if isinstance(msg.content, str) else "".join(
                b.get("text", "") for b in msg.content if isinstance(b, dict))
        except Exception as e:  # the SQL is still valid and useful without the prose
            log.warning("explanation failed: %s", e)
            text = "An explanation could not be generated right now, but the query above was validated successfully."
        return {"explanation": text.strip()}

    def answer_schema_question(self, state: AgentState) -> dict:
        msg = self.llm.invoke([
            SystemMessage(prompts.SCHEMA_QA_SYSTEM.format(schema=self.catalog.to_prompt())),
            HumanMessage(f"<user_request>{state['user_input']}</user_request>"),
        ])
        text = msg.content if isinstance(msg.content, str) else "".join(
            b.get("text", "") for b in msg.content if isinstance(b, dict))
        return {"status": "info", "message": text.strip()}

    # ------------------------------------------------------------------ final response

    def respond(self, state: AgentState) -> dict:
        status = state.get("status")
        validation = state.get("validation")
        if state.get("error"):
            status = "error"
            message = state["error"]
        elif status in ("rejected", "clarify", "info"):
            message = state.get("message") or ""
        elif validation and validation.get("ok") and state.get("sql"):
            status, message = "ok", state.get("explanation") or ""
        elif validation:
            status = "error"
            message = ("I couldn't produce a query that passes validation after "
                       f"{state.get('attempts', 0)} attempt(s):\n- " + "\n- ".join(validation.get("errors", [])) +
                       "\n\nTry rephrasing, or ask about the tables and columns that exist.")
        else:
            status, message = "error", "Something went wrong while handling that request."

        ok = status == "ok"
        sql = state.get("sql") if ok else None
        response = {
            "status": status,
            "intent": state.get("intent"),
            "mode": state.get("mode"),
            "message": message,
            "sql": sql,
            "dialects": transpile(sql) if sql else None,
            "original_sql": state.get("user_sql") if state.get("mode") in ("optimize", "debug", "explain") else None,
            "explanation": state.get("explanation") if ok else None,
            "notes": state.get("notes") or [],
            "issues": state.get("issues") or [],
            "validation": validation if ok else (validation or state.get("user_sql_validation")),
            "optimization": state.get("optimization") if ok else None,
            "results": state.get("results") if ok else None,
            "equivalence": state.get("equivalence") if ok else None,
            "attempts": state.get("attempts", 0),
            "relevant_tables": state.get("relevant_tables") or [],
            "trace": (state.get("trace") or []) + ["respond"],
        }
        # What the next turn's LLM sees of this turn.
        memory = f"{message}\n\nSQL:\n{sql}" if sql else message
        update: dict[str, Any] = {"response": response, "status": status, "messages": [AIMessage(memory)]}
        if sql:
            update["last_sql"] = sql
        return update
