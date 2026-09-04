from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_friend.domain import SourceLimitObservation, SourceLimitState
from music_friend.store import Catalog
from music_friend.store.portable import export_catalog, import_catalog

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def test_source_limit_migration_and_catalog_round_trip(tmp_path: Path) -> None:
    """Catches a source cooldown being held only in process memory."""
    expected = SourceLimitObservation(
        "spotify",
        SourceLimitState.COOLING_DOWN,
        NOW,
        NOW + timedelta(minutes=2),
        False,
        2,
    )

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        versions = catalog._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        catalog.put_source_limit(expected)

        assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
        assert catalog.get_source_limit("spotify") == expected


def test_source_limit_portable_export_import_round_trip(tmp_path: Path) -> None:
    """Catches portable backup silently losing the source cooldown safety state."""
    expected = SourceLimitObservation(
        "spotify",
        SourceLimitState.COOLING_DOWN,
        NOW,
        NOW + timedelta(seconds=60),
        True,
        1,
    )
    portable = tmp_path / "catalog.json"

    with Catalog.open(tmp_path / "source.sqlite3") as source:
        source.put_source_limit(expected)
        export_catalog(source, portable, exported_at=NOW)
    payload = json.loads(portable.read_text(encoding="utf-8"))
    record = next(item for item in payload["records"] if item["kind"] == "source_limit")

    assert payload["version"] == 3
    assert record == {
        "kind": "source_limit",
        "local_id": "spotify",
        "source": "spotify",
        "state": "cooling_down",
        "observed_at": NOW.isoformat(),
        "retry_at": (NOW + timedelta(seconds=60)).isoformat(),
        "retry_is_exact": True,
        "consecutive_limits": 1,
    }
    with Catalog.open(tmp_path / "target.sqlite3") as target:
        import_catalog(target, portable)
        assert target.get_source_limit("spotify") == expected
