"""Persistence boundary tests for malformed and unsafe inputs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain.models import (
    AffinityEvidenceKind,
    InboxState,
    LocalPreferenceKey,
    SignalKind,
    SourceCapability,
)
from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog, portable
from music_friend.store import catalog as catalog_module

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    (
        "not-json",
        "[]",
        '{"version":2,"metrics":[]}',
        '{"version":1,"metrics":{}}',
        '{"version":1,"metrics":[1]}',
        '{"version":1,"metrics":[{"kind":"pages"}]}',
    ),
)
def test_stored_refresh_summary_rejects_corruption(value: str) -> None:
    with pytest.raises(ValueError):
        catalog_module._decode_summary(value)


@pytest.mark.parametrize(
    "value",
    (
        "not-json",
        "[]",
        '{"version":2,"reasons":[]}',
        '{"version":1,"reasons":{}}',
        '{"version":1,"reasons":[1]}',
        '{"version":1,"reasons":[{"kind":"new_release"}]}',
        '{"version":1,"reasons":[{"kind":"new_release","detail":1}]}',
    ),
)
def test_stored_signal_explanation_rejects_corruption(value: str) -> None:
    with pytest.raises(ValueError):
        catalog_module._decode_explanation(value)


def test_catalog_constructor_and_closed_connection_fail_cleanly(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Catalog()
    with pytest.raises(TypeError):
        Catalog.open("catalog.sqlite3")  # type: ignore[arg-type]
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    catalog.close()
    with pytest.raises(CatalogUnavailableError):
        catalog.get_artist("artist")


@pytest.mark.parametrize(
    "operation",
    (
        lambda catalog: catalog.replace_affinity_evidence("", AffinityEvidenceKind.FOLLOWED, ()),
        lambda catalog: catalog.replace_affinity_evidence("source", "followed", ()),
        lambda catalog: catalog.replace_affinity_evidence(
            "source", AffinityEvidenceKind.FOLLOWED, []
        ),
        lambda catalog: catalog.replace_affinity_evidence(
            "source", AffinityEvidenceKind.FOLLOWED, (object(),)
        ),
        lambda catalog: catalog.get_local_preference("key"),
        lambda catalog: catalog.remove_local_preference("key"),
        lambda catalog: catalog.get_source_cursor("source", "capability"),
        lambda catalog: catalog.remove_source_cursor("source", "capability"),
        lambda catalog: catalog.put_source_limit(object()),
        lambda catalog: catalog.find_signal("provider", "release", "native", "version"),
        lambda catalog: catalog.list_signals("release", limit=1),
        lambda catalog: catalog.list_inbox_entries("unread", limit=1),
        lambda catalog: catalog.list_watchlist(limit=0),
        lambda catalog: catalog.list_signals(None, limit=0),
        lambda catalog: catalog.list_inbox_entries(None, limit=1001),
        lambda catalog: catalog.set_check_time("source", datetime(2026, 9, 2)),
    ),
)
def test_catalog_methods_reject_invalid_closed_values(catalog: Catalog, operation: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        operation(catalog)  # type: ignore[operator]


def test_catalog_missing_optional_state_is_empty(catalog: Catalog) -> None:
    assert catalog.get_local_preference(LocalPreferenceKey.EVENT_RADIUS) is None
    assert catalog.get_source_cursor("source", SourceCapability.SAVED_ITEMS) is None
    assert catalog.find_signal("provider", SignalKind.RELEASE, "native", "v1") is None
    assert catalog.list_signals(None, limit=1) == ()
    assert catalog.list_inbox_entries(InboxState.UNREAD, limit=1) == ()


@pytest.mark.parametrize(
    "action",
    (
        lambda: portable._require_keys({"one": 1}, frozenset({"two"})),
        lambda: portable._text("", "field"),
        lambda: portable._text("x" * 5, "field", maximum=4),
        lambda: portable._datetime("not-a-date", "date"),
        lambda: portable._datetime("2026-09-02", "date"),
        lambda: portable._string_list("not-list", "items"),
        lambda: portable._integer(True, "count"),
        lambda: portable._integer(-1, "count"),
        lambda: portable._refresh_summary([]),
        lambda: portable._refresh_summary({"version": 2, "metrics": []}),
        lambda: portable._refresh_summary({"version": 1, "metrics": [1]}),
        lambda: portable._explanation([]),
        lambda: portable._explanation({"version": 2, "reasons": []}),
        lambda: portable._explanation({"version": 1, "reasons": []}),
        lambda: portable._explanation({"version": 1, "reasons": [1]}),
    ),
)
def test_portable_scalar_and_nested_decoders_reject_invalid_values(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"format": "wrong", "version": 1, "exported_at": NOW.isoformat(), "records": []},
        {
            "format": "music-friend-catalog",
            "version": True,
            "exported_at": NOW.isoformat(),
            "records": [],
        },
        {
            "format": "music-friend-catalog",
            "version": 1,
            "exported_at": NOW.isoformat(),
            "records": {},
        },
        {
            "format": "music-friend-catalog",
            "version": 1,
            "exported_at": NOW.isoformat(),
            "records": [1],
        },
    ),
)
def test_portable_document_rejects_unsupported_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        portable._validate_document(payload)


def test_portable_reader_rejects_limits_paths_encoding_and_roots(tmp_path: Path) -> None:
    source = tmp_path / "catalog.json"
    source.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        portable._read_portable(source, 100, 10)
    with pytest.raises(ValueError):
        portable._read_portable(source, 0, 10)
    with pytest.raises(ValueError):
        portable._read_portable(Path("../catalog.json"), 100, 10)

    source.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError):
        portable._read_portable(source, 100, 10)


def test_export_rejects_unsafe_destination_and_naive_timestamp(
    catalog: Catalog, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        portable.export_catalog(catalog, Path("../catalog.json"))
    with pytest.raises(ValueError):
        portable.export_catalog(
            catalog, tmp_path / "catalog.json", exported_at=datetime(2026, 9, 2)
        )
