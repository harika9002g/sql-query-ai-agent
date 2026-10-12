"""Regenerates docs/PROMPTS.md from app/prompts.py so the documentation never drifts from the code."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import prompts  # noqa: E402

SECTIONS = [
    ("Intent detection", "detect_intent", "IntentDecision", ["INTENT_SYSTEM", "INTENT_USER"]),
    ("SQL generation (new + follow-up)", "generate_sql (mode generate/modify)", "SQLDraft",
     ["GENERATE_SYSTEM", "GENERATE_USER", "RETRY_FEEDBACK"]),
    ("SQL debugging", "generate_sql (mode debug)", "SQLFix", ["DEBUG_SYSTEM", "DEBUG_USER"]),
    ("SQL optimization", "generate_sql (mode optimize)", "SQLOptimization", ["OPTIMIZE_SYSTEM", "OPTIMIZE_USER"]),
    ("Explanation", "explain_sql", "free text (streamed)", ["EXPLAIN_SYSTEM", "EXPLAIN_USER"]),
    ("Schema questions", "answer_schema_question", "free text", ["SCHEMA_QA_SYSTEM"]),
    ("Help message (static, no LLM call)", "validate_scope", "—", ["HELP_MESSAGE"]),
]


def main() -> None:
    out = [
        "# Prompts",
        "",
        "> Generated from [`app/prompts.py`](../app/prompts.py) by `python scripts/export_prompts.py` — edit the code, not this file.",
        "",
        "**Design choices**",
        "",
        "- **Structured outputs.** Every routing decision comes from a Pydantic schema "
        "([`app/agent/schemas.py`](../app/agent/schemas.py)), not from parsing prose. On Claude this uses native "
        "JSON-schema output (`output_config.format`).",
        "- **Injection hardening.** User text is always wrapped in `<user_request>` / `<user_sql>` tags and each system "
        "prompt says that content is data, never instructions. The deterministic validator re-checks all SQL anyway.",
        "- **Grounding.** The schema (with row counts, foreign keys, indexes and the real categorical values such as "
        "`State = 'California'`) is injected as the single source of truth. The model can answer `cannot_answer` "
        "instead of inventing columns.",
        "- **Self-correction.** When validation fails, `RETRY_FEEDBACK` feeds the rejected SQL and the exact validator "
        "errors back into the next generation attempt.",
        "",
        "`{placeholders}` are filled at runtime: `{schema}` with the schema text, `{history}` with the last few turns, "
        "`{last_sql}` with the previous query, and `{findings}` with the validator's or analyzer's output.",
        "",
    ]
    for title, node, output, names in SECTIONS:
        out += [f"## {title}", "", f"Node: `{node}` · Output: `{output}`", ""]
        for name in names:
            out += [f"### `{name}`", "", "```text", getattr(prompts, name).strip("\n"), "```", ""]
    (ROOT / "docs" / "PROMPTS.md").write_text("\n".join(out), encoding="utf-8")
    print("docs/PROMPTS.md written")


if __name__ == "__main__":
    main()
