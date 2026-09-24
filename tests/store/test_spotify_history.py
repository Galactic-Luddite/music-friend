from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from music_friend.store import Catalog
from music_friend.store.portable import export_catalog, import_catalog, purge_source
from music_friend.store.spotify_history import import_spotify_history, summarize_history


def _archive(
    path: Path, records: list[dict[str, object]], *, name: str = "Streaming_History_Audio_2026.json"
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"Spotify Extended Streaming History/{name}",
            json.dumps(records),
        )
    return path


def _track(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "ts": "2026-01-02T03:04:05Z",
        "ms_played": 180000,
        "master_metadata_track_name": "Track One",
        "master_metadata_album_artist_name": "Artist One",
        "master_metadata_album_album_name": "Album One",
        "spotify_track_uri": "spotify:track:one",
        "episode_name": None,
        "episode_show_name": None,
        "spotify_episode_uri": None,
        "audiobook_chapter_title": None,
        "audiobook_chapter_uri": None,
        "audiobook_title": None,
        "audiobook_uri": None,
        "reason_start": "trackdone",
        "reason_end": "trackdone",
        "shuffle": False,
        "skipped": False,
        "offline": False,
        "offline_timestamp": None,
        "incognito_mode": False,
        "platform": "secret device",
        "conn_country": "US",
        "ip_addr": "203.0.113.9",
    }
    record.update(changes)
    return record


def test_history_window_survives_backup_restore_and_fractional_seconds(tmp_path: Path) -> None:
    source = _archive(
        tmp_path / "history.zip",
        [
            _track(),
            _track(ts="2026-01-02T03:04:05.500000Z"),
        ],
    )
    with Catalog.open(tmp_path / "original.sqlite3") as catalog:
        import_spotify_history(catalog, source)
        expected = summarize_history(
            catalog, since="2026-01-02T03:04:05Z", until="2026-01-02T03:04:05.500000Z"
        )
        assert expected.play_count == 1
        export_catalog(catalog, tmp_path / "backup")
    with Catalog.open(tmp_path / "restored.sqlite3") as catalog:
        import_catalog(catalog, tmp_path / "backup")
        assert (
            summarize_history(
                catalog, since="2026-01-02T03:04:05Z", until="2026-01-02T03:04:05.500000Z"
            )
            == expected
        )


def test_history_backup_preserves_empty_playback_reasons(tmp_path: Path) -> None:
    source = _archive(tmp_path / "history.zip", [_track(reason_start="", reason_end="")])
    with Catalog.open(tmp_path / "original.sqlite3") as catalog:
        import_spotify_history(catalog, source)
        export_catalog(catalog, tmp_path / "backup")
    with Catalog.open(tmp_path / "restored.sqlite3") as catalog:
        import_catalog(catalog, tmp_path / "backup")
        assert catalog._require_connection().execute(
            "SELECT reason_start, reason_end FROM listening_history"
        ).fetchone() == ("", "")


def test_import_keeps_music_plays_and_excludes_non_music_and_sensitive_fields(
    tmp_path: Path,
) -> None:
    source = _archive(
        tmp_path / "spotify.zip",
        [
            _track(),
            _track(
                ts="2026-01-03T03:04:05Z",
                spotify_track_uri=None,
                master_metadata_track_name=None,
                master_metadata_album_artist_name=None,
                master_metadata_album_album_name=None,
                episode_name="Episode",
                spotify_episode_uri="spotify:episode:one",
            ),
        ],
    )
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        result = import_spotify_history(catalog, source)
        row = (
            catalog._require_connection()
            .execute("SELECT track_name, artist_name, album_name, skipped FROM listening_history")
            .fetchone()
        )
        columns = {
            str(item[1])
            for item in catalog._require_connection().execute(
                "PRAGMA table_info(listening_history)"
            )
        }

    assert result.imported == 1
    assert result.non_music == 1
    assert row == ("Track One", "Artist One", "Album One", 0)
    assert {"ip_addr", "platform", "conn_country"}.isdisjoint(columns)


def test_reimport_is_idempotent_but_preserves_repeated_identical_plays(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track(), _track()])
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        first = import_spotify_history(catalog, source)
        second = import_spotify_history(catalog, source)

    assert (first.imported, first.duplicates) == (2, 0)
    assert (second.imported, second.duplicates) == (0, 2)


def test_identity_preserves_plays_that_differ_only_in_discarded_sensitive_fields(
    tmp_path: Path,
) -> None:
    source = _archive(
        tmp_path / "spotify.zip",
        [_track(platform="device one"), _track(platform="device two")],
    )
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        result = import_spotify_history(catalog, source)

    assert (result.imported, result.duplicates) == (2, 0)


def test_dry_run_reports_existing_duplicates_without_writing(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track(), _track()])
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        import_spotify_history(catalog, source)
        preview = import_spotify_history(catalog, source, dry_run=True)
        count = summarize_history(catalog).play_count

    assert (preview.imported, preview.duplicates) == (0, 2)
    assert count == 2


def test_invalid_record_rolls_back_whole_archive(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track(), _track(ms_played=-1)])
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(ValueError, match="invalid Spotify history archive"):
            import_spotify_history(catalog, source)
        count = (
            catalog._require_connection()
            .execute("SELECT COUNT(*) FROM listening_history")
            .fetchone()[0]
        )

    assert count == 0


@pytest.mark.parametrize(
    "change",
    [
        {"ts": "2026-01-02T03:04:05"},
        {"ts": "not-a-dateZ"},
        {"shuffle": "false"},
        {"master_metadata_track_name": None},
    ],
)
def test_import_rejects_malformed_music_records(tmp_path: Path, change: dict[str, object]) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track(**change)])
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(ValueError, match="invalid Spotify history archive"):
            import_spotify_history(catalog, source)


def test_import_rejects_unsafe_or_malformed_archives(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    malformed = tmp_path / "malformed.zip"
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr(
            "Spotify Extended Streaming History/Streaming_History_Audio_2026.json",
            "{}",
        )
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../Streaming_History_Audio_2026.json", "[]")

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        for source in (directory, malformed, traversal):
            with pytest.raises(ValueError, match="invalid Spotify history archive"):
                import_spotify_history(catalog, source)


def test_import_rejects_non_object_history_record(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", ["not an object"])  # type: ignore[list-item]
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(ValueError, match="invalid Spotify history archive"):
            import_spotify_history(catalog, source)


def test_import_accepts_nullable_optional_history_fields(tmp_path: Path) -> None:
    source = _archive(
        tmp_path / "spotify.zip",
        [_track(master_metadata_album_album_name=None, offline=None)],
    )
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        result = import_spotify_history(catalog, source)

    assert result.imported == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit must be from 1 through 50"),
        (
            {"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
            "since must not be after until",
        ),
        (
            {"since": "2026-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
            "since and until must not be equal",
        ),
        (
            # Same instant expressed with different offsets is still a reversed
            # range once normalized to UTC.
            {"since": "2026-02-01T01:00:00+01:00", "until": "2026-02-01T00:00:00Z"},
            "since and until must not be equal",
        ),
        ({"since": "2026-03-01T00:00:00"}, "since must include a UTC offset"),
        ({"until": "2026-03-01T00:00:00"}, "until must include a UTC offset"),
        ({"since": "2026-02-30T00:00:00Z"}, "since must be a valid RFC 3339 date-time"),
        ({"since": "not-a-timestamp"}, "since must be a valid RFC 3339 date-time"),
    ],
)
def test_summary_rejects_invalid_bounds(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(ValueError, match=message):
            summarize_history(catalog, **arguments)  # type: ignore[arg-type]


def test_summary_rejects_a_non_string_bound_as_a_caller_argument_error(tmp_path: Path) -> None:
    """A non-string ``since``/``until`` bound must raise ``HistoryArgumentError`` (a
    caller mistake), not the plain internal ``ValueError`` that `_text` raises."""
    from music_friend.store.spotify_history import HistoryArgumentError

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(HistoryArgumentError, match="since must be a valid RFC 3339"):
            summarize_history(catalog, since=12345)  # type: ignore[arg-type]


def test_summary_accepts_rfc_3339_offsets_and_normalizes_to_utc(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track(ts="2026-03-01T09:00:00Z")])
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        import_spotify_history(catalog, source)

        via_z = summarize_history(
            catalog, since="2026-03-01T08:00:00Z", until="2026-03-01T10:00:00Z"
        )
        via_positive_offset = summarize_history(
            catalog, since="2026-03-01T09:00:00+01:00", until="2026-03-01T11:00:00+01:00"
        )
        via_negative_offset = summarize_history(
            catalog, since="2026-03-01T00:00:00-08:00", until="2026-03-01T02:00:00-08:00"
        )

    assert via_z.play_count == 1
    assert via_positive_offset.play_count == 1
    assert via_negative_offset.play_count == 1
    assert via_positive_offset.since == "2026-03-01T08:00:00Z"
    assert via_negative_offset.since == "2026-03-01T08:00:00Z"


def test_summary_reports_range_rankings_and_brief_skip_breakdown(tmp_path: Path) -> None:
    source = _archive(
        tmp_path / "spotify.zip",
        [
            _track(ms_played=180000),
            _track(
                ts="2026-01-03T03:04:05Z",
                spotify_track_uri="spotify:track:two",
                master_metadata_track_name="Track Two",
                ms_played=4000,
                skipped=True,
            ),
            _track(ts="2025-01-03T03:04:05Z", ms_played=60000),
        ],
    )
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        import_spotify_history(catalog, source)
        summary = summarize_history(
            catalog, since="2026-01-01T00:00:00Z", until="2027-01-01T00:00:00Z", limit=5
        )

    assert summary.play_count == 2
    assert summary.milliseconds_played == 184000
    assert summary.skipped_count == 1
    assert summary.brief_count == 1
    assert summary.top_artists[0].name == "Artist One"
    assert [item.name for item in summary.top_tracks] == ["Track One", "Track Two"]


def test_history_round_trips_through_portable_export_and_source_purge(tmp_path: Path) -> None:
    source = _archive(tmp_path / "spotify.zip", [_track()])
    portable = tmp_path / "portable.json"
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        import_spotify_history(catalog, source)
        export_catalog(catalog, portable)
    with Catalog.open(tmp_path / "restored.sqlite3") as restored:
        import_catalog(restored, portable)
        assert summarize_history(restored).play_count == 1
        purge_source(restored, "spotify-history")
        assert summarize_history(restored).play_count == 0
