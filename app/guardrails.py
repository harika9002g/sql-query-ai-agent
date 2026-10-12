"""Pre-LLM input guardrails: size limits, prompt-injection heuristics, destructive-SQL detection.

These are cheap deterministic checks that run before any model call. They are one layer of
several (see README → Guardrails): the system prompts treat user text as data, and every SQL
statement is re-validated by `sql_guard` and executed on a read-only connection regardless.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import settings
from app.sql_guard import extract_sql, find_forbidden_keywords

_INJECTION_PATTERNS = [
    r"\bignore\b.{0,40}\b(previous|prior|above|earlier|all|your|the)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines?)",
    r"\bdisregard\b.{0,40}\b(instructions?|rules?|prompts?|guardrails?)",
    r"\bforget\b.{0,30}\b(instructions?|rules?|everything)",
    r"\b(reveal|show|print|repeat|output|leak)\b.{0,30}\b(system|hidden|initial|original)\s+(prompt|instructions?|message)",
    # Generic "act as …" is left to the LLM intent check — "act as a filter" is a legitimate phrasing.
    r"\byou are now\b|\bfrom now on,? you\b|\bpretend (to be|you are)\b",
    r"\b(developer|god|dan|jailbreak|unrestricted)\s+mode\b",
    r"\bjailbreak\b",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>",
    r"\b(new|updated|override)\s+(system\s+)?(instructions?|rules?)\s*:",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.I | re.S)

REFUSAL_OUT_OF_SCOPE = (
    "I'm designed to assist only with SQL and database-related tasks. "
    "Please ask a question related to the provided database schema."
)
REFUSAL_DESTRUCTIVE = (
    "I can't help with that — I only generate read-only queries, so operations like "
    "DELETE, UPDATE, INSERT, DROP, ALTER or TRUNCATE are not allowed. "
    "I can help you write a SELECT query to look at the data instead."
)
REFUSAL_INJECTION = (
    "I can't follow instructions that try to change how I work. I'm an SQL assistant for the "
    "provided database schema — please ask a question about the data."
)


@dataclass
class GuardResult:
    blocked: bool
    reason: str | None = None   # "too_long" | "empty" | "injection" | "destructive"
    message: str | None = None
    user_sql: str | None = None


def check_input(text: str) -> GuardResult:
    text = (text or "").strip()
    if not text:
        return GuardResult(True, "empty", "Please type a question about the database.")
    if len(text) > settings.max_input_chars:
        return GuardResult(True, "too_long",
                           f"That message is too long ({len(text)} characters). Please keep it under "
                           f"{settings.max_input_chars} characters.")
    if _INJECTION_RE.search(text):
        return GuardResult(True, "injection", REFUSAL_INJECTION)

    user_sql = extract_sql(text)
    if user_sql and find_forbidden_keywords(user_sql):
        return GuardResult(True, "destructive", REFUSAL_DESTRUCTIVE, user_sql)
    return GuardResult(False, user_sql=user_sql)
