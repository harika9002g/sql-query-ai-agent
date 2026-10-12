"""Structured-output schemas the LLM must fill. The graph routes on these fields."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal[
    "generate_sql", "modify_sql", "explain_sql", "optimize_sql", "debug_sql",
    "schema_question", "help", "destructive", "out_of_scope", "ambiguous",
]


class IntentDecision(BaseModel):
    intent: Intent = Field(description="The single best intent for the latest user message.")
    user_sql: Optional[str] = Field(None, description="SQL the user pasted, copied verbatim; null if none.")
    clarification_question: Optional[str] = Field(
        None, description="Only for intent=ambiguous: one short question to ask the user.")
    reason: str = Field(description="One short sentence justifying the intent.")


class SQLDraft(BaseModel):
    status: Literal["ok", "needs_clarification", "cannot_answer"]
    sql: Optional[str] = Field(None, description="A single read-only SQLite SELECT query when status=ok.")
    assumptions: list[str] = Field(default_factory=list, description="Assumptions made to resolve vagueness.")
    clarification_question: Optional[str] = Field(None, description="When status=needs_clarification.")
    reason: Optional[str] = Field(None, description="When status=cannot_answer: what data is missing.")


class SQLIssue(BaseModel):
    problem: str = Field(description="What is wrong, e.g. 'Column EmpName does not exist'.")
    explanation: str = Field(description="Why it is wrong / what the fix is, in plain English.")


class SQLFix(BaseModel):
    issues: list[SQLIssue]
    corrected_sql: str = Field(description="The corrected single read-only SQLite query.")


class SQLOptimization(BaseModel):
    optimized_sql: str = Field(description="Equivalent, cleaner, faster single read-only SQLite query.")
    improvements: list[str] = Field(description="Each readability/performance change made, one per item.")
    index_recommendations: list[str] = Field(
        default_factory=list, description="CREATE INDEX statements recommended (advice only, never executed).")
    removed_joins: list[str] = Field(default_factory=list, description="Joins removed and why.")
