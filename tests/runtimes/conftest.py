"""Keep CLI tests hermetic from the host's native credential store."""

from __future__ import annotations

import pytest

from music_friend.runtimes import cli


@pytest.fixture(autouse=True)
def _native_store_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_native_store_available", lambda: True)
