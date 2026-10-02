import sqlite3
from datetime import datetime, timezone

import pytest

from music_friend.providers import RecentPlay
from music_friend.store import Catalog
from music_friend.store.portable import export_catalog, import_catalog, purge_source
from music_friend.store.recent_history import RecentHistoryStore
from music_friend.store.spotify_history import summarize_history


def test_observations_preserve_precision_and_deduplicate_equivalent_instants(
    catalog: Catalog,
) -> None:
    store = RecentHistoryStore(catalog)
    first = RecentPlay(
        "spotify:track:a", "Track A", "Artist A", "Album A", "2030-01-01T00:00:00.123456789Z"
    )
    equivalent = RecentPlay(
        "spotify:track:a", "Track A", "Artist A", "Album A", "2029-12-31T19:00:00.123456789-05:00"
    )
    assert (
        store.commit_page(
            "spotify",
            (first,),
            attempted_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
            requested_after_ms=None,
            reason="first_check_retention_unknown",
        )
        == 1
    )
    assert (
        store.commit_page(
            "spotify",
            (equivalent,),
            attempted_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
            requested_after_ms=None,
            reason="first_check_retention_unknown",
        )
        == 0
    )
    rows = store.list_observations("spotify")
    assert rows[0].played_at == "2030-01-01T00:00:00.123456789Z"


def test_state_marks_running_then_terminal_without_erasing_intervals(catalog: Catalog) -> None:
    store = RecentHistoryStore(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    store.begin_attempt("spotify", at, requested_after_ms=None)
    assert store.get_state("spotify").needs_repair is True
    store.commit_page(
        "spotify",
        (RecentPlay("spotify:track:a", "A", "Artist", "Album", "2030-01-01T00:00:00.0000001Z"),),
        attempted_at=at,
        requested_after_ms=None,
        reason="first_check_retention_unknown",
    )
    store.finish_success(
        "spotify", at, outcome="first_snapshot", reason="first_check_retention_unknown"
    )
    state = store.get_state("spotify")
    assert state is not None and state.needs_repair is False
    assert state.last_successful_check_at == at
    assert store.list_intervals("spotify")[0].reason == "first_check_retention_unknown"


def test_summary_keeps_source_counts_and_labels_zero_candidate_combined_count(
    catalog: Catalog,
) -> None:
    store = RecentHistoryStore(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    store.begin_attempt("spotify", at, requested_after_ms=None)
    store.commit_page(
        "spotify",
        (
            RecentPlay(
                "spotify:track:a",
                "Track A",
                "Artist A",
                "Album A",
                "2030-01-01T00:00:00.123456789Z",
            ),
        ),
        attempted_at=at,
        requested_after_ms=None,
        reason="first_check_retention_unknown",
    )
    connection = catalog._require_connection()
    connection.execute(
        "INSERT INTO listening_history(event_id,source,played_at,milliseconds_played,track_uri,track_name,artist_name,album_name,archive_digest,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "archive:1",
            "spotify-history",
            "2030-01-01T00:00:01+00:00",
            1000,
            "spotify:track:b",
            "Track B",
            "Artist B",
            "Album B",
            "digest",
            at.isoformat(),
        ),
    )
    summary = summarize_history(catalog)
    assert (
        summary.play_count,
        summary.api_observation_count,
        summary.combined_observation_count,
    ) == (1, 1, 2)
    assert summary.candidate_overlap_count == 0
    assert summary.combined_observations_potentially_duplicated is True
    assert summary.duration_observation_count == 1 and summary.duration_unknown_count == 1
    assert summary.api_top_artists[0].name == "Artist A"


def test_portable_v6_round_trip_purge_and_disconnect_lifecycle(catalog: Catalog, tmp_path) -> None:
    store = RecentHistoryStore(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    store.begin_attempt("spotify", at, requested_after_ms=None)
    store.commit_page(
        "spotify",
        (RecentPlay("spotify:track:a", "A", "Artist", "Album", "2030-01-01T00:00:00.123456789Z"),),
        attempted_at=at,
        requested_after_ms=None,
        reason="first_check_retention_unknown",
    )
    store.finish_success(
        "spotify", at, outcome="first_snapshot", reason="first_check_retention_unknown"
    )
    portable = tmp_path / "portable.json"
    export_catalog(catalog, portable, exported_at=at)
    restored = Catalog.open(tmp_path / "restored" / "catalog.sqlite3")
    try:
        import_catalog(restored, portable)
        restored_store = RecentHistoryStore(restored)
        assert restored_store.list_observations("spotify")[0].played_at.endswith(".123456789Z")
        assert restored_store.get_state("spotify") is not None
        restored.disconnect_source("spotify")
        assert len(restored_store.list_observations("spotify")) == 1
        purge_source(restored, "spotify")
        assert restored_store.list_observations("spotify") == ()
        assert restored_store.get_state("spotify") is None
        assert restored_store.list_intervals("spotify") == ()
    finally:
        restored.close()


@pytest.mark.parametrize("api_first", [False, True])
def test_archive_multiplicity_is_ambiguous_in_both_import_orders(tmp_path, api_first: bool) -> None:
    catalog = Catalog.open(tmp_path / f"order-{api_first}" / "catalog.sqlite3")
    store = RecentHistoryStore(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)

    def add_api_observation() -> None:
        store.begin_attempt("spotify", at, requested_after_ms=None)
        store.commit_page(
            "spotify",
            (
                RecentPlay(
                    "spotify:track:a",
                    "Track A",
                    "Artist A",
                    "Album A",
                    "2030-01-01T00:00:00.123456789Z",
                ),
            ),
            attempted_at=at,
            requested_after_ms=None,
            reason="first_check_retention_unknown",
        )

    def add_archive_occurrences() -> None:
        connection = catalog._require_connection()
        for occurrence in range(2):
            connection.execute(
                "INSERT INTO listening_history(event_id,source,played_at,milliseconds_played,track_uri,track_name,artist_name,album_name,archive_digest,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"archive:{occurrence}",
                    "spotify-history",
                    "2030-01-01T00:00:00.123456789+00:00",
                    1000,
                    "spotify:track:a",
                    "Track A",
                    "Artist A",
                    "Album A",
                    "digest",
                    at.isoformat(),
                ),
            )

    try:
        if api_first:
            add_api_observation()
            add_archive_occurrences()
        else:
            add_archive_occurrences()
            add_api_observation()
        summary = summarize_history(catalog)
        assert summary.play_count == 2
        assert summary.api_observation_count == 1
        assert summary.candidate_overlap_count == 1
        assert summary.ambiguous_overlap_count == 1
    finally:
        catalog.close()


def test_page_commit_rolls_back_observations_and_boundary_together(catalog: Catalog) -> None:
    store = RecentHistoryStore(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    store.begin_attempt("spotify", at, requested_after_ms=None)
    connection = catalog._require_connection()
    connection.execute(
        """CREATE TRIGGER reject_synthetic_interval
        BEFORE INSERT ON history_incomplete_intervals
        BEGIN SELECT RAISE(ABORT, 'synthetic interval failure'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic interval failure"):
        store.commit_page(
            "spotify",
            (
                RecentPlay(
                    "spotify:track:a",
                    "Track A",
                    "Artist A",
                    "Album A",
                    "2030-01-01T00:00:00.123456789Z",
                ),
            ),
            attempted_at=at,
            requested_after_ms=None,
            reason="first_check_retention_unknown",
        )

    state = store.get_state("spotify")
    assert state is not None
    assert state.needs_repair is True
    assert state.newest_observed_played_at is None
    assert store.list_observations("spotify") == ()
