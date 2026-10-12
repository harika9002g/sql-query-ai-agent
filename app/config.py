"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _default_model() -> str:
    """Pick a provider based on which API key is present. Format: '<provider>:<model>'."""
    if os.getenv("LLM_MODEL"):
        return os.environ["LLM_MODEL"]
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic:claude-opus-5-5"
    if os.getenv("OPENAI_API_KEY"):
        return "openai:gpt-4.1"
    if os.getenv("GROQ_API_KEY"):
        return "groq:llama-3.3-70b-versatile"
    return "anthropic:claude-opus-5-5"


@dataclass(frozen=True)
class Settings:
    llm_model: str = field(default_factory=_default_model)
    # Effort for Anthropic models; "low" keeps latency chat-friendly for this structured task.
    llm_effort: str = os.getenv("LLM_EFFORT", "low")
    db_path: Path = Path(os.getenv("DB_PATH", ROOT / "data" / "sample.db"))
    schema_path: Path = ROOT / "data" / "schema.sql"
    max_result_rows: int = int(os.getenv("MAX_RESULT_ROWS", "500"))
    query_timeout_s: float = float(os.getenv("QUERY_TIMEOUT_S", "5"))
    max_generation_attempts: int = int(os.getenv("MAX_GENERATION_ATTEMPTS", "3"))
    max_input_chars: int = int(os.getenv("MAX_INPUT_CHARS", "4000"))
    history_turns: int = int(os.getenv("HISTORY_TURNS", "6"))


settings = Settings()
