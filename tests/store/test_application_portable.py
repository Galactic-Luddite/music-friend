import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxEntry,
    InboxState,
    LocalPreference,
    LocalPreferenceKey,
    RefreshKind,
    RefreshMetric,
    RefreshMetricKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceReference,
    WatchlistAction,
    WatchlistOverride,
)
from music_friend.store import Catalog
from music_friend.store.portable import PurgeResult, export_catalog, import_catalog, purge_source

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=2)


def _seed_application_state(
    catalog: Catalog,
    source: str = "spotify",
    inbox_state: InboxState = InboxState.DISMISSED,
    *,
    inbox_local_id: str = "inbox-1",
    artist_name: str = "Artist",
) -> None:
    artist = Artist(
        "artist-1",
        artist_name,
        (SourceReference(source, "artist-native", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    catalog.put_artist(artist)
    release = Release(
        "release-1",
        "Release",
        "album",
        date(2026, 9, 1),
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (SourceReference(source, "release-native", None, NOW),),
        NOW,
    )
    catalog.put_release(release)
    catalog.put_affinity_evidence(
        AffinityEvidence(
            "evidence-1",
            artist.local_id,
            source,
            AffinityEvidenceKind.SAVED_TRACK,
            "track-native",
            None,
            NOW,
        )
    )
    catalog.put_watchlist_override(WatchlistOverride(artist.local_id, WatchlistAction.PIN, NOW))
    catalog.put_local_preference(
        LocalPreference(LocalPreferenceKey.EVENT_POSTAL_CODE, "94401", NOW)
    )
    catalog.put_refresh_run(
        RefreshRun(
            "run-1",
            source,
            RefreshKind.CATALOG,
            RefreshStatus.SUCCEEDED,
            NOW,
            LATER,
            RefreshSummary((RefreshMetric(RefreshMetricKind.RECORDS_SEEN, 2),)),
        )
    )
    catalog.put_source_cursor(
        SourceCursor(source, SourceCapability.SAVED_ITEMS, "opaque-page", NOW)
    )
    catalog.put_signal(
        Signal(
            "signal-1",
            SignalKind.RELEASE,
            release.local_id,
            source,
            "release-native",
            "release-fingerprint-v1",
            "material-v1",
            Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release"),)),
            NOW,
        )
    )
    catalog.put_inbox_entry(InboxEntry(inbox_local_id, "signal-1", inbox_state, NOW, LATER))


def test_portable_v3_round_trips_all_application_state_without_credentials(
    catalog: Catalog, tmp_path: Path
) -> None:
    _seed_application_state(catalog)
    destination = tmp_path / "catalog-v3.json"

    exported = export_catalog(catalog, destination, exported_at=NOW)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    kinds = {record["kind"] for record in payload["records"]}
    assert {
        "affinity_evidence",
        "watchlist_override",
        "local_preference",
        "refresh_run",
        "source_cursor",
        "signal",
        "inbox_entry",
    } <= kinds
    folded = destination.read_text(encoding="utf-8").casefold()
    assert "access_token" not in folded
    assert "client_secret" not in folded
    target_path = tmp_path / "target" / "catalog.sqlite3"
    with Catalog.open(target_path) as target:
        imported = import_catalog(target, destination)
        assert imported.record_count == exported.record_count
        assert target.get_affinity_evidence("evidence-1") == catalog.get_affinity_evidence(
            "evidence-1"
        )
        assert target.get_watchlist_override("artist-1") == WatchlistOverride(
            "artist-1", WatchlistAction.PIN, NOW
        )
        assert target.get_local_preference(LocalPreferenceKey.EVENT_POSTAL_CODE) == LocalPreference(
            LocalPreferenceKey.EVENT_POSTAL_CODE, "94401", NOW
        )
        assert target.get_refresh_run("run-1") == catalog.get_refresh_run("run-1")
        assert target.get_source_cursor("spotify", SourceCapability.SAVED_ITEMS) == SourceCursor(
            "spotify", SourceCapability.SAVED_ITEMS, "opaque-page", NOW
        )
        assert target.get_signal("signal-1") == catalog.get_signal("signal-1")
        assert target.get_inbox_entry("inbox-1") == InboxEntry(
            "inbox-1", "signal-1", InboxState.DISMISSED, NOW, LATER
        )


def test_portable_source_cursor_round_trips_all_valid_maximum_fields(
    catalog: Catalog, tmp_path: Path
) -> None:
    source_name = "s" * 4096
    expected = SourceCursor(
        source_name,
        SourceCapability.RECENT_RELEASES,
        "c" * 2048,
        NOW,
    )
    catalog.put_source_cursor(expected)
    destination = tmp_path / "maximum-cursor.json"

    export_catalog(catalog, destination, exported_at=NOW)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    cursor_record = next(
        record for record in payload["records"] if record["kind"] == "source_cursor"
    )
    assert len(cursor_record["local_id"]) < 100
    with Catalog.open(tmp_path / "maximum-cursor" / "catalog.sqlite3") as imported:
        import_catalog(imported, destination)
        assert imported.get_source_cursor(source_name, SourceCapability.RECENT_RELEASES) == expected


def test_caught_nested_import_failure_rolls_back_only_replay(
    catalog: Catalog, tmp_path: Path
) -> None:
    source_path = tmp_path / "source" / "catalog.sqlite3"
    portable = tmp_path / "source.json"
    with Catalog.open(source_path) as source:
        _seed_application_state(
            source,
            inbox_local_id="inbox-imported",
            artist_name="Imported Artist",
        )
        export_catalog(source, portable, exported_at=NOW)

    _seed_application_state(
        catalog,
        inbox_local_id="inbox-existing",
        artist_name="Existing Artist",
    )
    catalog.remove_local_preference(LocalPreferenceKey.EVENT_POSTAL_CODE)

    with catalog.transaction():
        catalog.put_local_preference(
            LocalPreference(LocalPreferenceKey.EVENT_RADIUS_UNIT, "miles", NOW)
        )
        try:
            import_catalog(catalog, portable)
        except sqlite3.IntegrityError:
            pass

    artist = catalog.get_artist("artist-1")
    assert artist is not None
    assert artist.display_name == "Existing Artist"
    assert catalog.get_local_preference(LocalPreferenceKey.EVENT_POSTAL_CODE) is None
    assert catalog.get_local_preference(LocalPreferenceKey.EVENT_RADIUS_UNIT) is not None
    assert catalog.get_inbox_entry("inbox-existing") is not None
    assert catalog.get_inbox_entry("inbox-imported") is None


def test_purge_source_removes_unread_provider_inbox_state(
    catalog: Catalog,
) -> None:
    _seed_application_state(catalog, source="remove", inbox_state=InboxState.UNREAD)

    result = purge_source(catalog, "remove")

    assert result == PurgeResult(
        mapping_count=2,
        observation_count=0,
        check_time_count=0,
        affinity_evidence_count=1,
        source_cursor_count=1,
        signal_count=1,
        inbox_entry_count=1,
        refresh_run_count=1,
    )
    assert catalog.get_affinity_evidence("evidence-1") is None
    assert catalog.get_source_cursor("remove", SourceCapability.SAVED_ITEMS) is None
    assert catalog.get_refresh_run("run-1") is None
    assert catalog.get_signal("signal-1") is None
    assert catalog.get_inbox_entry("inbox-1") is None
    assert catalog.get_watchlist_override("artist-1") == WatchlistOverride(
        "artist-1", WatchlistAction.PIN, NOW
    )
    assert catalog.get_local_preference(LocalPreferenceKey.EVENT_POSTAL_CODE) == LocalPreference(
        LocalPreferenceKey.EVENT_POSTAL_CODE, "94401", NOW
    )
    assert catalog.get_artist("artist-1") is not None


@pytest.mark.parametrize("state", (InboxState.SAVED, InboxState.DISMISSED))
def test_purge_source_retains_user_inbox_decision_and_portable_target(
    catalog: Catalog, tmp_path: Path, state: InboxState
) -> None:
    _seed_application_state(catalog, source="remove", inbox_state=state)

    result = purge_source(catalog, "remove")

    assert result.signal_count == 0
    assert result.inbox_entry_count == 0
    assert catalog.get_release("release-1") is not None
    assert catalog.get_signal("signal-1") is not None
    assert catalog.get_inbox_entry("inbox-1") == InboxEntry(
        "inbox-1", "signal-1", state, NOW, LATER
    )

    destination = tmp_path / f"retained-{state.value}.json"
    export_catalog(catalog, destination, exported_at=NOW)
    with Catalog.open(tmp_path / state.value / "catalog.sqlite3") as imported:
        import_catalog(imported, destination)
        assert imported.get_release("release-1") is not None
        assert imported.get_signal("signal-1") == catalog.get_signal("signal-1")
        assert imported.get_inbox_entry("inbox-1") == catalog.get_inbox_entry("inbox-1")


def test_purge_source_retains_cross_source_signal_and_affinity_roots(catalog: Catalog) -> None:
    artist = Artist(
        "artist-root",
        "Artist Root",
        (SourceReference("remove", "artist-native", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    catalog.put_artist(artist)
    release = Release(
        "release-root",
        "Release Root",
        "album",
        date(2026, 9, 1),
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (SourceReference("remove", "release-native", None, NOW),),
        NOW,
    )
    catalog.put_release(release)
    evidence = AffinityEvidence(
        "evidence-keep",
        artist.local_id,
        "keep",
        AffinityEvidenceKind.FOLLOWED,
        "artist-native",
        None,
        NOW,
    )
    catalog.put_affinity_evidence(evidence)
    signal = Signal(
        "signal-keep",
        SignalKind.RELEASE,
        release.local_id,
        "keep",
        "release-native",
        "release-fingerprint-v1",
        "material-v1",
        Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release Root"),)),
        NOW,
    )
    catalog.put_signal(signal)

    result = purge_source(catalog, "remove")

    assert result.mapping_count == 2
    assert catalog.get_affinity_evidence(evidence.local_id) == evidence
    assert catalog.get_signal(signal.local_id) == signal
    assert catalog.get_artist(artist.local_id) is not None
    assert catalog.get_release(release.local_id) is not None
