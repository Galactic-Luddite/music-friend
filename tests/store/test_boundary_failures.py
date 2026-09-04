from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.store.portable import delete_catalog, export_catalog, import_catalog

NOW = "2026-09-01T12:00:00+00:00"


class _PlatformOs:
    """Delegate OS operations while selecting the documented fallback branch."""

    name = "fallback"

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _payload(records: object) -> dict[str, object]:
    return {
        "format": "music-friend-catalog",
        "version": 1,
        "exported_at": NOW,
        "records": records,
    }


def _write_payload(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def test_catalog_fallback_creates_private_usable_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import music_friend.store.catalog as catalog_module

    path = tmp_path / "private" / "nested" / "catalog.sqlite3"
    monkeypatch.setattr(catalog_module, "os", _PlatformOs())

    with Catalog.open(path) as catalog:
        catalog.set_check_time("source", datetime.fromisoformat(NOW))
        assert catalog.get_check_time("source") is not None

    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_portable_fallback_exports_through_private_missing_parents(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import music_friend.store.portable as portable

    destination = tmp_path / "portable" / "nested" / "catalog.json"
    monkeypatch.setattr(portable, "os", _PlatformOs())
    export_catalog(catalog, destination)
    assert json.loads(destination.read_text(encoding="utf-8"))["format"] == "music-friend-catalog"
    assert stat.S_IMODE(destination.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_portable_fallback_deletes_catalog_and_regular_sidecars_only(
    catalog: Catalog,
    catalog_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import music_friend.store.portable as portable

    monkeypatch.setattr(portable, "os", _PlatformOs())
    original_close = catalog.close

    def close_with_sidecars() -> None:
        original_close()
        Path(f"{catalog_path}-wal").write_bytes(b"wal")
        Path(f"{catalog_path}-shm").write_bytes(b"shm")

    monkeypatch.setattr(catalog, "close", close_with_sidecars)
    sibling = catalog_path.parent / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    delete_catalog(catalog)

    assert not catalog_path.exists()
    assert not Path(f"{catalog_path}-wal").exists()
    assert not Path(f"{catalog_path}-shm").exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_portable_fallback_delete_refuses_sidecar_symlink_before_deleting_main(
    catalog: Catalog,
    catalog_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import music_friend.store.portable as portable

    monkeypatch.setattr(portable, "os", _PlatformOs())
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    original_close = catalog.close

    def close_with_symlink() -> None:
        original_close()
        Path(f"{catalog_path}-wal").symlink_to(victim)

    monkeypatch.setattr(catalog, "close", close_with_symlink)

    with pytest.raises(OSError, match="substituted"):
        delete_catalog(catalog)

    assert catalog_path.exists()
    assert Path(f"{catalog_path}-wal").is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"


def test_export_removes_temporary_file_when_stream_open_fails(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export" / "catalog.json"
    temporary_name = f".{destination.name}.fixed.tmp"
    monkeypatch.setattr("music_friend.store.portable.secrets.token_hex", lambda _length: "fixed")

    def fail_fdopen(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic stream failure")

    monkeypatch.setattr("music_friend.store.portable.os.fdopen", fail_fdopen)

    with pytest.raises(OSError, match="stream failure"):
        export_catalog(catalog, destination)

    assert not destination.exists()
    assert not (destination.parent / temporary_name).exists()


def test_delete_rejects_missing_opened_catalog_and_closes_handle(
    catalog: Catalog, catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_close = catalog.close

    def close_then_remove() -> None:
        original_close()
        catalog_path.unlink()

    monkeypatch.setattr(catalog, "close", close_then_remove)

    with pytest.raises(OSError, match="missing"):
        delete_catalog(catalog)

    with pytest.raises(CatalogUnavailableError):
        catalog.get_artist("artist")


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"[]", "root must be an object"),
        (b'{"format":"x","format":"y"}', "duplicate JSON key"),
        (b'{"records":[NaN]}', "invalid JSON constant"),
        (b"\xff", "must be UTF-8"),
    ],
    ids=("array-root", "duplicate-key", "nonfinite-number", "invalid-utf8"),
)
def test_import_rejects_ambiguous_or_nonportable_json(
    catalog: Catalog, tmp_path: Path, raw: bytes, error: str
) -> None:
    source = tmp_path / "invalid.json"
    source.write_bytes(raw)

    with pytest.raises(ValueError, match=error):
        import_catalog(catalog, source)

    assert catalog.get_check_time("source") is None


@pytest.mark.parametrize(
    ("records", "error"),
    [
        ({}, "records must be an array"),
        (["not-an-object"], "portable record must be an object"),
        (
            [
                {
                    "kind": "unknown",
                    "local_id": "unknown-1",
                }
            ],
            "unknown portable record kind",
        ),
        (
            [
                {
                    "kind": "source_mapping",
                    "local_id": "artist:missing:source:native",
                    "record_kind": "artist",
                    "record_local_id": "missing",
                    "source": "source",
                    "native_id": "native",
                    "canonical_url": None,
                    "observed_at": NOW,
                    "position": 0,
                }
            ],
            "source mapping target does not exist",
        ),
        (
            [
                {
                    "kind": "artist",
                    "local_id": "artist-1",
                    "display_name": "Artist",
                    "identity_confidence": "source_only",
                    "observed_at": NOW,
                },
                {
                    "kind": "source_mapping",
                    "local_id": "artist:artist-1:source:native",
                    "record_kind": "artist",
                    "record_local_id": "artist-1",
                    "source": "source",
                    "native_id": "native",
                    "canonical_url": None,
                    "observed_at": NOW,
                    "position": 1,
                },
            ],
            "positions must be contiguous",
        ),
        (
            [
                {
                    "kind": "check_time",
                    "local_id": "different-source",
                    "source": "source",
                    "checked_at": NOW,
                }
            ],
            "identity does not match source",
        ),
    ],
    ids=(
        "records-object",
        "scalar-record",
        "unknown-kind",
        "orphan-source-mapping",
        "noncontiguous-source-position",
        "check-time-identity",
    ),
)
def test_import_rejects_invalid_record_shapes_without_mutating_catalog(
    catalog: Catalog, tmp_path: Path, records: object, error: str
) -> None:
    source = tmp_path / "invalid-records.json"
    _write_payload(source, _payload(records))

    with pytest.raises(ValueError, match=error):
        import_catalog(catalog, source)

    assert catalog.get_check_time("source") is None


@pytest.mark.parametrize(("max_bytes", "max_records"), [(0, 1), (1, 0), (-1, 1), (1, -1)])
def test_import_requires_positive_resource_limits(
    catalog: Catalog, tmp_path: Path, max_bytes: int, max_records: int
) -> None:
    source = tmp_path / "empty.json"
    _write_payload(source, _payload([]))

    with pytest.raises(ValueError, match="limits must be positive"):
        import_catalog(catalog, source, max_bytes=max_bytes, max_records=max_records)


def test_import_requires_safe_path_object(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="safe pathlib.Path"):
        import_catalog(catalog, cast(Any, "catalog.json"))
