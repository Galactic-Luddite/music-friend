import sqlite3
from datetime import date, datetime, timedelta, timezone

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

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def _artist(local_id: str, name: str | None = None) -> Artist:
    return Artist(
        local_id=local_id,
        display_name=name or local_id,
        source_refs=(SourceReference("spotify", f"native-{local_id}", None, NOW),),
        identity_confidence=IdentityConfidence.SOURCE_ONLY,
        observed_at=NOW,
    )


def _evidence(
    local_id: str,
    artist_local_id: str,
    kind: AffinityEvidenceKind,
    evidence_key: str,
    *,
    rank: int | None = None,
    observed_at: datetime = NOW,
) -> AffinityEvidence:
    return AffinityEvidence(
        local_id,
        artist_local_id,
        "spotify",
        kind,
        evidence_key,
        rank,
        observed_at,
    )


def _release(catalog: Catalog) -> Release:
    artist = _artist("artist-signal")
    catalog.put_artist(artist)
    release = Release(
        local_id="release-1",
        title="Release",
        release_type="album",
        release_date=date(2026, 9, 1),
        date_precision=ReleaseDatePrecision.DAY,
        artist_refs=(artist.local_id,),
        source_refs=(SourceReference("spotify", "album-1", None, NOW),),
        observed_at=NOW,
    )
    catalog.put_release(release)
    return release


def _signal(record_local_id: str, *, observed_at: datetime = NOW) -> Signal:
    return Signal(
        local_id="signal-1",
        kind=SignalKind.RELEASE,
        record_local_id=record_local_id,
        provider="spotify",
        provider_native_id="album-1",
        fingerprint="release-fingerprint-v1",
        material_version="material-v1",
        explanation=Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release"),)),
        observed_at=observed_at,
    )


def test_migration_three_creates_only_the_v1_application_state_tables(catalog: Catalog) -> None:
    connection = catalog._connection
    assert connection is not None
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert {
        "affinity_evidence",
        "watchlist_overrides",
        "local_preferences",
        "refresh_runs",
        "source_cursors",
        "release_check_cursors",
        "release_check_continuations",
        "release_discoveries",
        "signals",
        "inbox_entries",
    } <= names
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]


def test_affinity_replacement_is_idempotent_atomic_and_capability_scoped(
    catalog: Catalog,
) -> None:
    artist = _artist("artist-1")
    catalog.put_artist(artist)
    followed = _evidence(
        "evidence-followed",
        artist.local_id,
        AffinityEvidenceKind.FOLLOWED,
        "native-artist-1",
    )
    saved = _evidence(
        "evidence-saved",
        artist.local_id,
        AffinityEvidenceKind.SAVED_TRACK,
        "native-track-1",
    )
    catalog.replace_affinity_evidence("spotify", AffinityEvidenceKind.FOLLOWED, (followed,))
    catalog.replace_affinity_evidence("spotify", AffinityEvidenceKind.SAVED_TRACK, (saved,))

    catalog.replace_affinity_evidence("spotify", AffinityEvidenceKind.SAVED_TRACK, (saved,))

    assert catalog.list_affinity_evidence(artist.local_id, limit=10) == (followed, saved)
    invalid = _evidence(
        "evidence-invalid",
        "missing-artist",
        AffinityEvidenceKind.SAVED_TRACK,
        "native-track-2",
    )
    with pytest.raises(sqlite3.IntegrityError):
        catalog.replace_affinity_evidence("spotify", AffinityEvidenceKind.SAVED_TRACK, (invalid,))
    assert catalog.list_affinity_evidence(artist.local_id, limit=10) == (followed, saved)


def test_caught_nested_affinity_failure_restores_replaced_rows(catalog: Catalog) -> None:
    artist = _artist("artist-1")
    catalog.put_artist(artist)
    original = _evidence(
        "evidence-saved",
        artist.local_id,
        AffinityEvidenceKind.SAVED_TRACK,
        "native-track-1",
    )
    catalog.put_affinity_evidence(original)
    invalid = _evidence(
        "evidence-invalid",
        "missing-artist",
        AffinityEvidenceKind.SAVED_TRACK,
        "native-track-2",
    )

    with catalog.transaction():
        catalog.put_local_preference(
            LocalPreference(LocalPreferenceKey.EVENT_RADIUS_UNIT, "miles", NOW)
        )
        try:
            catalog.replace_affinity_evidence(
                "spotify", AffinityEvidenceKind.SAVED_TRACK, (invalid,)
            )
        except sqlite3.IntegrityError:
            pass

    assert catalog.get_affinity_evidence(original.local_id) == original
    assert catalog.get_local_preference(LocalPreferenceKey.EVENT_RADIUS_UNIT) is not None


def test_affinity_unique_provider_evidence_cannot_acquire_a_second_local_identity(
    catalog: Catalog,
) -> None:
    artist = _artist("artist-1")
    catalog.put_artist(artist)
    catalog.put_affinity_evidence(
        _evidence(
            "evidence-1",
            artist.local_id,
            AffinityEvidenceKind.SAVED_TRACK,
            "native-track",
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        catalog.put_affinity_evidence(
            _evidence(
                "evidence-2",
                artist.local_id,
                AffinityEvidenceKind.SAVED_TRACK,
                "native-track",
            )
        )

    assert catalog.get_affinity_evidence("evidence-1") is not None
    assert catalog.get_affinity_evidence("evidence-2") is None


def test_watchlist_overrides_delete_to_remove_and_require_bounded_queries(
    catalog: Catalog,
) -> None:
    for index in range(501):
        catalog.put_artist(_artist(f"artist-{index:03d}"))
    for index in range(501):
        catalog.put_watchlist_override(
            WatchlistOverride(f"artist-{index:03d}", WatchlistAction.ADD, NOW)
        )

    assert len(catalog.list_watchlist_overrides(limit=500)) == 500
    catalog.remove_watchlist_override("artist-000")
    catalog.put_watchlist_override(WatchlistOverride("artist-500", WatchlistAction.MUTE, LATER))
    assert catalog.get_watchlist_override("artist-000") is None
    assert catalog.get_watchlist_override("artist-500") == WatchlistOverride(
        "artist-500", WatchlistAction.MUTE, LATER
    )
    with pytest.raises(ValueError, match="limit"):
        catalog.list_watchlist_overrides(limit=501)


def test_preferences_refresh_runs_and_source_cursors_round_trip_with_stable_bounds(
    catalog: Catalog,
) -> None:
    preferences = (
        LocalPreference(LocalPreferenceKey.EVENT_RADIUS_UNIT, "miles", NOW),
        LocalPreference(LocalPreferenceKey.EVENT_RADIUS, "50", NOW),
    )
    for preference in preferences:
        catalog.put_local_preference(preference)
    running = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.CATALOG,
        RefreshStatus.RUNNING,
        NOW,
        None,
        RefreshSummary(()),
    )
    complete = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.CATALOG,
        RefreshStatus.PARTIAL,
        NOW,
        LATER,
        RefreshSummary((RefreshMetric(RefreshMetricKind.FAILURES, 1),)),
    )
    catalog.put_refresh_run(running)
    catalog.put_refresh_run(complete)
    cursor = SourceCursor("spotify", SourceCapability.SAVED_ITEMS, "opaque", NOW)
    catalog.put_source_cursor(cursor)

    assert catalog.list_local_preferences(limit=4) == tuple(
        sorted(preferences, key=lambda item: item.key.value)
    )
    assert catalog.get_refresh_run("run-1") == complete
    assert catalog.list_refresh_runs(limit=1) == (complete,)
    assert catalog.get_source_cursor("spotify", SourceCapability.SAVED_ITEMS) == cursor
    assert catalog.list_source_cursors("spotify", limit=10) == (cursor,)
    catalog.remove_source_cursor("spotify", SourceCapability.SAVED_ITEMS)
    assert catalog.get_source_cursor("spotify", SourceCapability.SAVED_ITEMS) is None
    for call in (
        lambda: catalog.list_local_preferences(limit=0),
        lambda: catalog.list_refresh_runs(limit=501),
        lambda: catalog.list_source_cursors("spotify", limit=0),
    ):
        with pytest.raises(ValueError, match="limit"):
            call()


def test_signals_repeat_and_inbox_upserts_are_idempotent_and_fail_closed_on_references(
    catalog: Catalog,
) -> None:
    release = _release(catalog)
    signal = _signal(release.local_id)
    catalog.put_signal(signal)
    catalog.put_signal(_signal(release.local_id, observed_at=LATER))
    unread = InboxEntry("inbox-1", signal.local_id, InboxState.UNREAD, NOW, NOW)
    saved = InboxEntry("inbox-1", signal.local_id, InboxState.SAVED, NOW, LATER)
    catalog.put_inbox_entry(unread)
    catalog.put_inbox_entry(saved)

    actual_signal = catalog.get_signal(signal.local_id)
    assert actual_signal is not None
    assert actual_signal.observed_at == NOW
    assert (
        catalog.find_signal("spotify", SignalKind.RELEASE, "album-1", "material-v1")
        == actual_signal
    )
    assert catalog.list_signals(SignalKind.RELEASE, limit=10) == (actual_signal,)
    assert catalog.get_inbox_entry("inbox-1") == saved
    assert catalog.list_inbox_entries(InboxState.SAVED, limit=10) == (saved,)
    with pytest.raises(sqlite3.IntegrityError):
        catalog.put_signal(_signal("missing-release"))
    with pytest.raises(sqlite3.IntegrityError):
        catalog.put_inbox_entry(
            InboxEntry("inbox-invalid", "missing-signal", InboxState.UNREAD, NOW, NOW)
        )


def test_signal_provider_material_converges_on_first_local_identity(catalog: Catalog) -> None:
    release = _release(catalog)
    original = _signal(release.local_id)
    replacement = Signal(
        local_id="alternate-proposed-id",
        kind=original.kind,
        record_local_id=original.record_local_id,
        provider=original.provider,
        provider_native_id=original.provider_native_id,
        fingerprint=original.fingerprint,
        material_version=original.material_version,
        explanation=original.explanation,
        observed_at=LATER,
    )

    catalog.put_signal(original)
    catalog.put_signal(replacement)

    actual = catalog.find_signal(
        original.provider,
        original.kind,
        original.provider_native_id,
        original.material_version,
    )
    assert actual is not None
    assert actual.local_id == "signal-1"
    assert actual.record_local_id == "release-1"
    assert actual.fingerprint == "release-fingerprint-v1"
    assert actual.explanation == Explanation(
        (ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release"),)
    )
    assert actual.observed_at == NOW
    assert catalog.get_signal(replacement.local_id) is None
    assert catalog.list_signals(SignalKind.RELEASE, limit=10) == (actual,)


@pytest.mark.parametrize("conflict_field", ("target", "fingerprint", "explanation"))
def test_signal_material_conflict_preserves_saved_inbox_decision(
    catalog: Catalog, conflict_field: str
) -> None:
    original_release = _release(catalog)
    other_release = Release(
        local_id="release-2",
        title="Other Release",
        release_type="album",
        release_date=date(2026, 9, 2),
        date_precision=ReleaseDatePrecision.DAY,
        artist_refs=original_release.artist_refs,
        source_refs=(SourceReference("spotify", "album-2", None, NOW),),
        observed_at=NOW,
    )
    catalog.put_release(other_release)
    original = _signal(original_release.local_id)
    saved = InboxEntry("inbox-saved", original.local_id, InboxState.SAVED, NOW, NOW)
    catalog.put_signal(original)
    catalog.put_inbox_entry(saved)
    conflicting = Signal(
        local_id=f"proposed-{conflict_field}",
        kind=original.kind,
        record_local_id=(
            other_release.local_id if conflict_field == "target" else original.record_local_id
        ),
        provider=original.provider,
        provider_native_id=original.provider_native_id,
        fingerprint=(
            "conflicting-fingerprint-v1"
            if conflict_field == "fingerprint"
            else original.fingerprint
        ),
        material_version=original.material_version,
        explanation=(
            Explanation(
                (ExplanationReason(ExplanationReasonKind.UPDATED_RELEASE, "Changed Release"),)
            )
            if conflict_field == "explanation"
            else original.explanation
        ),
        observed_at=LATER,
    )

    with pytest.raises(sqlite3.IntegrityError, match="material"):
        catalog.put_signal(conflicting)

    actual_signal = catalog.get_signal(original.local_id)
    assert actual_signal is not None
    assert actual_signal.record_local_id == "release-1"
    assert actual_signal.fingerprint == "release-fingerprint-v1"
    assert actual_signal.explanation == Explanation(
        (ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release"),)
    )
    assert actual_signal.observed_at == NOW
    assert catalog.get_signal(conflicting.local_id) is None
    actual_inbox = catalog.get_inbox_entry(saved.local_id)
    assert actual_inbox is not None
    assert actual_inbox.signal_local_id == "signal-1"
    assert actual_inbox.state is InboxState.SAVED
    assert actual_inbox.created_at == NOW
    assert actual_inbox.updated_at == NOW
    assert catalog.list_inbox_entries(InboxState.SAVED, limit=10) == (saved,)


def test_signal_newest_first_order_uses_absolute_time_across_offsets(catalog: Catalog) -> None:
    release = _release(catalog)
    older = datetime(2026, 9, 1, 12, tzinfo=timezone(timedelta(hours=14)))
    newer = datetime(2026, 9, 1, 1, tzinfo=timezone(timedelta(hours=-7)))
    catalog.put_signal(
        Signal(
            "signal-older",
            SignalKind.RELEASE,
            release.local_id,
            "source",
            "older",
            "older-v1",
            "material-v1",
            _signal(release.local_id).explanation,
            older,
        )
    )
    catalog.put_signal(
        Signal(
            "signal-newer",
            SignalKind.RELEASE,
            release.local_id,
            "source",
            "newer",
            "newer-v1",
            "material-v1",
            _signal(release.local_id).explanation,
            newer,
        )
    )

    actual = catalog.list_signals(SignalKind.RELEASE, limit=10)

    assert tuple(signal.local_id for signal in actual) == ("signal-newer", "signal-older")
    assert all(signal.observed_at.utcoffset() == timedelta(0) for signal in actual)


def test_signal_target_cannot_be_deleted_or_reidentified_while_referenced(
    catalog: Catalog,
) -> None:
    release = _release(catalog)
    release = Release(
        local_id=release.local_id,
        title=release.title,
        release_type=release.release_type,
        release_date=release.release_date,
        date_precision=release.date_precision,
        artist_refs=release.artist_refs,
        source_refs=(),
        observed_at=release.observed_at,
    )
    catalog.put_release(release)
    catalog.put_signal(_signal(release.local_id))
    connection = catalog._connection
    assert connection is not None

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM releases WHERE local_id = ?", (release.local_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE releases SET local_id = ? WHERE local_id = ?",
            ("changed-release", release.local_id),
        )

    assert catalog.get_signal("signal-1") is not None


def test_artist_search_requires_explicit_bound_and_has_stable_name_id_order(
    catalog: Catalog,
) -> None:
    for artist in (
        _artist("artist-b", "same"),
        _artist("artist-a", "Same"),
        _artist("artist-c", "Other"),
    ):
        catalog.put_artist(artist)

    assert tuple(artist.local_id for artist in catalog.search_artists("sam", limit=10)) == (
        "artist-a",
        "artist-b",
    )
    with pytest.raises(ValueError, match="limit"):
        catalog.search_artists("sam", limit=0)
    with pytest.raises(ValueError, match="query"):
        catalog.search_artists("x" * 257, limit=10)


def test_artist_search_is_accent_and_stylization_insensitive_but_returns_names_unchanged(
    catalog: Catalog,
) -> None:
    for artist in (
        _artist("artist-accent", "Élodie Café"),
        _artist("artist-stylized", "Ÿvy"),
        _artist("artist-hyphen", "Rock-N-Roll"),
        _artist("artist-ampersand", "Salt & Pepper"),
        _artist("artist-other", "Someone Else"),
    ):
        catalog.put_artist(artist)

    # Plain ASCII query finds an accented stored name; the display name comes back unchanged.
    accent_matches = catalog.search_artists("elodie cafe", limit=10)
    assert tuple(artist.local_id for artist in accent_matches) == ("artist-accent",)
    assert accent_matches[0].display_name == "Élodie Café"

    # A plain "Y" finds a name stylized with "Y" (NFKD decomposes it to Y + combining diaeresis).
    stylized_matches = catalog.search_artists("yvy", limit=10)
    assert tuple(artist.local_id for artist in stylized_matches) == ("artist-stylized",)

    # Case folding still applies alongside accent folding.
    assert tuple(artist.local_id for artist in catalog.search_artists("ÉLODIE", limit=10)) == (
        "artist-accent",
    )

    # Punctuation (- and &) does not block a reasonable match: it is treated as a separator.
    assert tuple(artist.local_id for artist in catalog.search_artists("rock n roll", limit=10)) == (
        "artist-hyphen",
    )
    assert tuple(artist.local_id for artist in catalog.search_artists("salt pepper", limit=10)) == (
        "artist-ampersand",
    )

    assert catalog.search_artists("nonexistent stylized query", limit=10) == ()


def test_artist_search_treats_percent_and_underscore_as_literal_characters(
    catalog: Catalog,
) -> None:
    catalog.put_artist(_artist("artist-percent", "100% Wolf"))
    catalog.put_artist(_artist("artist-underscore", "under_score"))
    catalog.put_artist(_artist("artist-noise", "Other Artist"))

    assert tuple(artist.local_id for artist in catalog.search_artists("100%", limit=10)) == (
        "artist-percent",
    )
    assert tuple(artist.local_id for artist in catalog.search_artists("under_score", limit=10)) == (
        "artist-underscore",
    )
    # A literal "%" or "_" must not act as a SQL LIKE wildcard matching every stored name; each
    # matches only the one artist whose display name literally contains that character.
    assert tuple(artist.local_id for artist in catalog.search_artists("%", limit=10)) == (
        "artist-percent",
    )
    assert tuple(artist.local_id for artist in catalog.search_artists("_", limit=10)) == (
        "artist-underscore",
    )
