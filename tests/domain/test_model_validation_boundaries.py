"""Boundary tests for provider-neutral domain records."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from music_friend.domain.models import (
    AffinityScore,
    Artist,
    ArtistEventDiscoveryResult,
    ArtistReleaseDiscoveryResult,
    CatalogItem,
    CatalogItemBatch,
    Event,
    EventCandidate,
    EventCandidateKind,
    EventDiscovery,
    EventDiscoveryResult,
    EventDiscoveryStatus,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    LocalPreference,
    LocalPreferenceKey,
    RefreshKind,
    RefreshMetric,
    RefreshMetricKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    Release,
    ReleaseCandidate,
    ReleaseCandidateKind,
    ReleaseCheckContinuation,
    ReleaseDatePrecision,
    ReleaseDiscovery,
    ReleaseDiscoveryResult,
    ReleaseDiscoveryStatus,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
    SyncCapabilityResult,
    SyncCapabilityStatus,
    WatchlistEntry,
    WatchlistInclusionReason,
)

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
REF = SourceReference("source", "native", "https://example.test/item", NOW)
ARTIST = Artist("artist-1", "Artist", (REF,), IdentityConfidence.SOURCE_ONLY, NOW)
RELEASE = Release(
    "release-1",
    "Release",
    "album",
    date(2026, 9, 2),
    ReleaseDatePrecision.DAY,
    (ARTIST.local_id,),
    (REF,),
    NOW,
)
EVENT = Event(
    "event-1",
    "Event",
    (ARTIST.local_id,),
    "Venue",
    "City",
    NOW,
    "second",
    ("https://example.test/tickets",),
    (REF,),
    NOW,
)


@pytest.mark.parametrize(
    "action",
    (
        lambda: SourceReference("source", "native", "https://[invalid", NOW),
        lambda: CatalogItem("track", "id", "title", [], (REF,), NOW),
        lambda: CatalogItem("track", "id", "title", (), (), NOW),
        lambda: CatalogItem("track", "id", "title", (), (object(),), NOW),
        lambda: Release("id", "title", "album", NOW, ReleaseDatePrecision.DAY, ("a",), (), NOW),
        lambda: Release(
            "id", "title", "album", date(2026, 2, 2), ReleaseDatePrecision.YEAR, ("a",), (), NOW
        ),
        lambda: Release(
            "id", "title", "album", date(2026, 2, 2), ReleaseDatePrecision.MONTH, ("a",), (), NOW
        ),
        lambda: Event("id", "title", (), None, None, None, "date", (), (), NOW),
        lambda: Event("id", "title", (), None, None, NOW, "week", (), (), NOW),
        lambda: Event("id", "title", (), None, None, NOW, None, [], (), NOW),
    ),
)
def test_core_records_reject_invalid_shapes(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


@pytest.mark.parametrize(
    "action",
    (
        lambda: CatalogItemBatch([], ()),
        lambda: CatalogItemBatch((), []),
        lambda: CatalogItemBatch((), (), 1),
        lambda: CatalogItemBatch((), (), "x" * 2049),
        lambda: CatalogItemBatch(
            tuple(CatalogItem("track", f"t-{n}", "title", (), (REF,), NOW) for n in range(501)),
            (),
        ),
        lambda: RefreshSummary([]),
        lambda: RefreshSummary(
            tuple(RefreshMetric(kind, 0) for kind in RefreshMetricKind)
            + (RefreshMetric(RefreshMetricKind.PAGES, 0),)
        ),
        lambda: RefreshSummary(
            (RefreshMetric(RefreshMetricKind.PAGES, 0), RefreshMetric(RefreshMetricKind.PAGES, 1))
        ),
        lambda: Explanation([]),
        lambda: Explanation(()),
        lambda: Explanation(
            tuple(ExplanationReason(ExplanationReasonKind.NEW_RELEASE, None) for _ in range(17))
        ),
    ),
)
def test_collection_records_enforce_types_sizes_and_uniqueness(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


@pytest.mark.parametrize(
    "action",
    (
        lambda: AffinityScore(0, 1, 0, 0, 0, None, 0, None, 0, None, 0),
        lambda: AffinityScore(0, 0, 0, 1, 0, None, 0, None, 0, None, 0),
        lambda: AffinityScore(0, 0, 0, 0, 0, 1, 1, None, 0, None, 0),
        lambda: AffinityScore(0, 0, 0, 0, 0, None, 0, 1, 1, None, 0),
        lambda: AffinityScore(0, 0, 0, 0, 0, None, 0, None, 0, 1, 1),
        lambda: AffinityScore(1, 0, 0, 0, 0, None, 0, None, 0, None, 0),
        lambda: AffinityScore(0, 0, 0, 0, 0, 0, 0, None, 0, None, 0),
        lambda: WatchlistEntry(object(), WatchlistInclusionReason.AUTOMATIC, object()),
    ),
)
def test_affinity_records_reject_inconsistent_components(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("key", "value"),
    (
        (LocalPreferenceKey.EVENT_COUNTRY_CODE, "usa"),
        (LocalPreferenceKey.EVENT_POSTAL_CODE, "bad/postal"),
        (LocalPreferenceKey.EVENT_RADIUS, "0"),
        (LocalPreferenceKey.EVENT_RADIUS_UNIT, "leagues"),
    ),
)
def test_preferences_reject_values_outside_closed_policy(
    key: LocalPreferenceKey, value: str
) -> None:
    with pytest.raises(ValueError):
        LocalPreference(key, value, NOW)


@pytest.mark.parametrize(
    "action",
    (
        lambda: RefreshRun(
            "run",
            "source",
            RefreshKind.ALL,
            RefreshStatus.SUCCEEDED,
            NOW,
            NOW - timedelta(seconds=1),
            RefreshSummary(()),
        ),
        lambda: RefreshRun(
            "run", "source", RefreshKind.ALL, RefreshStatus.SUCCEEDED, NOW, NOW, object()
        ),
        lambda: SourceLimitObservation("source", SourceLimitState.AVAILABLE, NOW, None, True, 0),
        lambda: SourceLimitObservation(
            "source", SourceLimitState.COOLING_DOWN, NOW, None, False, 1
        ),
        lambda: SourceLimitObservation(
            "source", SourceLimitState.QUOTA_EXHAUSTED, NOW, LATER, False, 1
        ),
        lambda: SourceLimitObservation(
            "source", SourceLimitState.AVAILABLE, NOW, None, False, 1_000_001
        ),
        lambda: SourceLimitObservation("source", SourceLimitState.AVAILABLE, NOW, None, 0, 0),
    ),
)
def test_refresh_and_limit_records_reject_inconsistent_states(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


def test_release_discovery_records_enforce_relationships_and_partial_cursor() -> None:
    candidate = ReleaseCandidate(RELEASE, ARTIST.local_id, ReleaseCandidateKind.NEW)
    other_release = Release(
        "release-2",
        "Other",
        "album",
        date(2026, 1, 1),
        ReleaseDatePrecision.DAY,
        ("artist-2",),
        (REF,),
        NOW,
    )
    actions = (
        lambda: ReleaseDiscovery(
            "release-1", "source", "native", "title", date(2026, 1, 1), "token", LATER, NOW
        ),
        lambda: ReleaseCandidate(object(), ARTIST.local_id, ReleaseCandidateKind.NEW),
        lambda: ReleaseCandidate(other_release, ARTIST.local_id, ReleaseCandidateKind.NEW),
        lambda: ArtistReleaseDiscoveryResult(
            ARTIST.local_id, ReleaseDiscoveryStatus.SUCCESS, 0, [], None
        ),
        lambda: ArtistReleaseDiscoveryResult(
            ARTIST.local_id, ReleaseDiscoveryStatus.SUCCESS, 0, (candidate,), None
        ),
        lambda: ArtistReleaseDiscoveryResult(
            "other", ReleaseDiscoveryStatus.SUCCESS, 1, (candidate,), None
        ),
        lambda: ArtistReleaseDiscoveryResult(
            ARTIST.local_id, ReleaseDiscoveryStatus.PARTIAL, 99, (), "cursor"
        ),
        lambda: ArtistReleaseDiscoveryResult(
            ARTIST.local_id, ReleaseDiscoveryStatus.SUCCESS, 0, (), "cursor"
        ),
        lambda: ReleaseDiscoveryResult([]),
        lambda: ReleaseDiscoveryResult(
            (
                ArtistReleaseDiscoveryResult(
                    ARTIST.local_id, ReleaseDiscoveryStatus.SUCCESS, 0, (), None
                ),
            )
            * 2
        ),
    )
    for action in actions:
        with pytest.raises(ValueError):
            action()


def test_event_discovery_records_enforce_relationships_and_closed_states() -> None:
    candidate = EventCandidate(EVENT, ARTIST.local_id, EventCandidateKind.NEW)
    result = ArtistEventDiscoveryResult(
        ARTIST.local_id, EventDiscoveryStatus.SUCCESS, 1, (candidate,)
    )
    actions = (
        lambda: EventDiscovery(
            "event-1",
            "source",
            "native",
            ARTIST.local_id,
            "variant",
            "material",
            None,
            LATER,
            NOW,
            NOW,
            LATER,
        ),
        lambda: EventDiscovery(
            "event-1",
            "source",
            "native",
            ARTIST.local_id,
            "variant",
            "material",
            None,
            NOW,
            NOW,
            LATER,
            NOW,
        ),
        lambda: EventCandidate(object(), ARTIST.local_id, EventCandidateKind.NEW),
        lambda: EventCandidate(EVENT, "other", EventCandidateKind.NEW),
        lambda: ArtistEventDiscoveryResult(
            ARTIST.local_id,
            EventDiscoveryStatus.SUCCESS,
            0,
            [],
        ),
        lambda: ArtistEventDiscoveryResult(
            ARTIST.local_id, EventDiscoveryStatus.SUCCESS, 0, (candidate,)
        ),
        lambda: ArtistEventDiscoveryResult("other", EventDiscoveryStatus.SUCCESS, 1, (candidate,)),
        lambda: ArtistEventDiscoveryResult(
            ARTIST.local_id, EventDiscoveryStatus.CACHED, 1, (candidate,)
        ),
        lambda: EventDiscoveryResult(EventDiscoveryStatus.SUCCESS, []),
        lambda: EventDiscoveryResult(EventDiscoveryStatus.SUCCESS, (result, result)),
        lambda: EventDiscoveryResult(EventDiscoveryStatus.SKIPPED, (result,)),
    )
    for action in actions:
        with pytest.raises(ValueError):
            action()


def test_signal_requires_a_valid_explanation_record() -> None:
    with pytest.raises(ValueError):
        Signal(
            "signal",
            SignalKind.RELEASE,
            "release",
            "source",
            "native",
            "fingerprint",
            "v1",
            object(),
            NOW,
        )


def test_sync_results_reject_duplicate_capabilities() -> None:
    item = SyncCapabilityResult(SourceCapability.SAVED_ITEMS, SyncCapabilityStatus.SUCCESS, 0, 0, 0)
    from music_friend.domain.models import CatalogSyncResult

    with pytest.raises(ValueError):
        CatalogSyncResult([item])
    with pytest.raises(ValueError):
        CatalogSyncResult((item, item))


def test_remaining_scalar_bounds_reject_unsafe_values() -> None:
    valid_affinity = AffinityScore(0, 0, 0, 0, 0, None, 0, None, 0, None, 0)
    actions = (
        lambda: RefreshMetric(RefreshMetricKind.PAGES, True),
        lambda: ReleaseDiscovery(
            "release", "source", "native", "title", date(2026, 1, 1), "bad token", NOW, NOW
        ),
        lambda: WatchlistEntry(ARTIST, WatchlistInclusionReason.AUTOMATIC, object()),
        lambda: SourceCursor("source", SourceCapability.SAVED_ITEMS, "x" * 2049, NOW),
        lambda: ReleaseCheckContinuation("source", ARTIST.local_id, "bad cursor!", NOW),
        lambda: ArtistReleaseDiscoveryResult(
            ARTIST.local_id, ReleaseDiscoveryStatus.PARTIAL, 100, (), "bad cursor!"
        ),
    )
    assert valid_affinity.total_points == 0
    for action in actions:
        with pytest.raises(ValueError):
            action()
