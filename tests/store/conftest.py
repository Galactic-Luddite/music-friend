from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from music_friend.store import Catalog


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    return tmp_path / "private" / "catalog.sqlite3"


@pytest.fixture
def catalog(catalog_path: Path) -> Iterator[Catalog]:
    opened = Catalog.open(catalog_path)
    try:
        yield opened
    finally:
        opened.close()
