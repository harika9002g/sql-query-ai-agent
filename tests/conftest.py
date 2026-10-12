import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def sample_db():
    if not Path(settings.db_path).exists():
        from app.seed import build_database
        build_database()
    return settings.db_path
