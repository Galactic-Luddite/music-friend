from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def observed_at() -> datetime:
    return datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


@pytest.fixture
def load_spotify_fixture() -> Any:
    def load(name: str) -> dict[str, object]:
        value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        assert type(value) is dict
        return value

    return load
