from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain import Artist, IdentityConfidence, SourceReference
from music_friend.store import Catalog
from music_friend.store.portable import ExportResult, export_catalog

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _seed(catalog: Catalog) -> None:
    catalog.put_artist(
        Artist(
            local_id="artist-2",
            display_name="Second",
            source_refs=(SourceReference("spotify", "two", None, NOW),),
            identity_confidence=IdentityConfidence.EXTERNAL_ID,
            observed_at=NOW,
        )
    )
    catalog.put_artist(
        Artist(
            local_id="artist-1",
            display_name="First",
            source_refs=(SourceReference("spotify", "one", "https://example.test/one", NOW),),
            identity_confidence=IdentityConfidence.USER_CONFIRMED,
            observed_at=NOW,
        )
    )
    catalog.set_check_time("spotify", NOW)


def test_export_writes_canonical_private_deterministic_file(
    catalog: Catalog, tmp_path: Path
) -> None:
    _seed(catalog)
    destination = tmp_path / "new" / "nested" / "catalog.json"

    result = export_catalog(catalog, destination, exported_at=NOW)

    assert result == ExportResult(path=destination, record_count=5)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    raw = destination.read_bytes()
    assert raw.endswith(b"\n")
    assert b": " not in raw
    payload = json.loads(raw)
    assert list(payload) == ["format", "version", "exported_at", "records"]
    assert payload["format"] == "music-friend-catalog"
    assert payload["version"] == 4
    assert payload["exported_at"] == "2026-09-01T12:00:00+00:00"
    assert [(record["kind"], record["local_id"]) for record in payload["records"]] == [
        ("artist", "artist-1"),
        ("artist", "artist-2"),
        ("check_time", "spotify"),
        ("source_mapping", "artist:artist-1:spotify:one"),
        ("source_mapping", "artist:artist-2:spotify:two"),
    ]


def test_export_does_not_chmod_existing_parent(catalog: Catalog, tmp_path: Path) -> None:
    parent = tmp_path / "selected"
    parent.mkdir(mode=0o750)
    os.chmod(parent, 0o750)

    export_catalog(catalog, parent / "catalog.json", exported_at=NOW)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750


def test_export_repairs_new_nested_parent_modes_under_restrictive_umask(
    catalog: Catalog, tmp_path: Path
) -> None:
    first = tmp_path / "umask-parent"
    second = first / "nested"
    destination = second / "catalog.json"
    try:
        previous_umask = os.umask(0o777)
        try:
            export_catalog(catalog, destination, exported_at=NOW)
        finally:
            os.umask(previous_umask)

        assert destination.is_file()
        assert stat.S_IMODE(first.stat().st_mode) == 0o700
        assert stat.S_IMODE(second.stat().st_mode) == 0o700
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    finally:
        for path in (first, second):
            if path.exists():
                path.chmod(0o700)


def test_export_refuses_overwrite_without_replace(catalog: Catalog, tmp_path: Path) -> None:
    destination = tmp_path / "catalog.json"
    destination.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        export_catalog(catalog, destination, exported_at=NOW)

    assert destination.read_text(encoding="utf-8") == "keep"


def test_export_replace_is_explicit_and_atomic(catalog: Catalog, tmp_path: Path) -> None:
    destination = tmp_path / "catalog.json"
    destination.write_text("old", encoding="utf-8")

    export_catalog(catalog, destination, replace=True, exported_at=NOW)

    assert json.loads(destination.read_text(encoding="utf-8"))["version"] == 4
    assert not tuple(tmp_path.glob(".catalog.json.*.tmp"))


def test_export_syncs_file_and_parent_around_atomic_replace(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "catalog.json"
    destination.write_text("old", encoding="utf-8")
    real_fsync = os.fsync
    real_replace = os.replace
    synced: list[int] = []
    replaced: list[tuple[object, object]] = []

    def recording_fsync(descriptor: int) -> None:
        synced.append(descriptor)
        real_fsync(descriptor)

    def recording_replace(source: object, target: object, **kwargs: object) -> None:
        replaced.append((source, target))
        real_replace(source, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    export_catalog(catalog, destination, replace=True, exported_at=NOW)

    assert len(synced) == 2
    assert len(set(synced)) == 2
    assert len(replaced) == 1
    temporary, target = replaced[0]
    assert str(temporary).startswith(".catalog.json.")
    assert str(temporary).endswith(".tmp")
    assert target == "catalog.json"


@pytest.mark.parametrize("target_kind", ("symlink", "directory"))
def test_export_refuses_nonregular_target(
    catalog: Catalog, tmp_path: Path, target_kind: str
) -> None:
    destination = tmp_path / "catalog.json"
    if target_kind == "symlink":
        actual = tmp_path / "actual.json"
        actual.write_text("keep", encoding="utf-8")
        destination.symlink_to(actual)
    else:
        destination.mkdir()

    with pytest.raises((OSError, ValueError)):
        export_catalog(catalog, destination, replace=True, exported_at=NOW)


def test_export_contains_no_credential_or_raw_payload_schema(
    catalog: Catalog, tmp_path: Path
) -> None:
    destination = tmp_path / "catalog.json"
    export_catalog(catalog, destination, exported_at=NOW)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    forbidden = {"token", "secret", "credential", "password", "raw", "payload"}

    for record in payload["records"]:
        assert forbidden.isdisjoint({key.lower() for key in record})
        assert all(
            not any(word in str(value).lower() for word in forbidden) for value in record.values()
        )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("display_name", "client_" + "secret=sentinel"),
        ("canonical_url", "https://example.test/artist?access_" + "token=sentinel"),
    ),
)
def test_export_refuses_credential_like_legacy_values(
    catalog: Catalog, tmp_path: Path, column: str, value: str
) -> None:
    _seed(catalog)
    connection = catalog._connection
    assert connection is not None
    if column == "display_name":
        connection.execute(
            "UPDATE artists SET display_name = ? WHERE local_id = ?", (value, "artist-1")
        )
    else:
        connection.execute(
            "UPDATE source_references SET canonical_url = ? WHERE native_id = ?", (value, "one")
        )
    destination = tmp_path / "catalog.json"

    with pytest.raises(ValueError, match="credential-like"):
        export_catalog(catalog, destination, exported_at=NOW)

    assert not destination.exists()


def test_export_refuses_legacy_value_over_portable_limit(catalog: Catalog, tmp_path: Path) -> None:
    _seed(catalog)
    connection = catalog._connection
    assert connection is not None
    connection.execute(
        "UPDATE artists SET display_name = ? WHERE local_id = ?", ("x" * 4097, "artist-1")
    )
    destination = tmp_path / "catalog.json"

    with pytest.raises(ValueError, match="portable text limit"):
        export_catalog(catalog, destination, exported_at=NOW)

    assert not destination.exists()


def test_export_refuses_denylisted_schema_key(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import music_friend.store.portable as portable

    monkeypatch.setattr(
        portable,
        "_export_records",
        lambda _catalog: [
            {"kind": "artist", "local_id": "artist-1", "access_" + "token": "sentinel"}
        ],
    )
    destination = tmp_path / "catalog.json"

    with pytest.raises(ValueError, match="credential-like"):
        export_catalog(catalog, destination, exported_at=NOW)

    assert not destination.exists()


def test_export_remains_anchored_when_ancestor_is_substituted(
    catalog: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import music_friend.store.portable as portable

    selected = tmp_path / "selected"
    nested = selected / "nested"
    nested.mkdir(parents=True)
    moved = tmp_path / "moved"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    real_open_parent = portable._open_export_parent_posix

    def open_then_substitute(path: Path) -> int:
        descriptor = real_open_parent(path)
        selected.rename(moved)
        selected.symlink_to(attacker, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(portable, "_open_export_parent_posix", open_then_substitute)

    export_catalog(catalog, nested / "catalog.json", exported_at=NOW)

    assert (moved / "nested" / "catalog.json").is_file()
    assert not (attacker / "nested" / "catalog.json").exists()


def test_export_refuses_existing_symlink_ancestor(catalog: Catalog, tmp_path: Path) -> None:
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    selected = tmp_path / "selected"
    selected.symlink_to(attacker, target_is_directory=True)

    with pytest.raises(OSError):
        export_catalog(catalog, selected / "nested" / "catalog.json", exported_at=NOW)

    assert not (attacker / "nested" / "catalog.json").exists()
