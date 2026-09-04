from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    CatalogItem,
    CatalogItemBatch,
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
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceReference,
    WatchlistAction,
    WatchlistOverride,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _artist(local_id: str = "artist-1") -> Artist:
    native_id = local_id.removeprefix("artist-")
    return Artist(
        local_id=local_id,
        display_name=f"Artist {native_id}",
        source_refs=(SourceReference("spotify", native_id, None, NOW),),
        identity_confidence=IdentityConfidence.SOURCE_ONLY,
        observed_at=NOW,
    )


def _item(*artist_refs: str) -> CatalogItem:
    return CatalogItem(
        kind="track",
        local_id="track-1",
        title="Track",
        artist_refs=artist_refs,
        source_refs=(SourceReference("spotify", "track-1", None, NOW),),
        observed_at=NOW,
    )


def test_catalog_item_batch_requires_every_credited_artist_exactly_once() -> None:
    first = _artist("artist-1")
    second = _artist("artist-2")

    batch = CatalogItemBatch((_item(first.local_id, second.local_id),), (second, first), "next")

    assert batch.items[0].artist_refs == ("artist-1", "artist-2")
    assert tuple(artist.local_id for artist in batch.artists) == ("artist-2", "artist-1")
    assert batch.next_cursor == "next"
    with pytest.raises(ValueError, match="credited artist"):
        CatalogItemBatch((_item(first.local_id, second.local_id),), (first,), None)
    with pytest.raises(ValueError, match="duplicate artist"):
        CatalogItemBatch((_item(first.local_id),), (first, first), None)
    with pytest.raises(ValueError, match="credited artist"):
        CatalogItemBatch((_item(first.local_id),), (first, second), None)


def test_affinity_evidence_enforces_kind_specific_rank_and_immutable_identity() -> None:
    evidence = AffinityEvidence(
        local_id="evidence-1",
        artist_local_id="artist-1",
        source="spotify",
        kind=AffinityEvidenceKind.TOP_SHORT_TERM,
        evidence_key="artist-native-1",
        rank=1,
        observed_at=NOW,
    )

    assert evidence.rank == 1
    with pytest.raises(FrozenInstanceError):
        evidence.rank = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="rank"):
        AffinityEvidence(
            "evidence-2",
            "artist-1",
            "spotify",
            AffinityEvidenceKind.SAVED_TRACK,
            "track-native-1",
            4,
            NOW,
        )
    with pytest.raises(ValueError, match="rank"):
        AffinityEvidence(
            "evidence-3",
            "artist-1",
            "spotify",
            AffinityEvidenceKind.TOP_LONG_TERM,
            "artist-native-1",
            None,
            NOW,
        )


def test_watchlist_preferences_cursors_and_inbox_use_closed_validated_states() -> None:
    override = WatchlistOverride("artist-1", WatchlistAction.PIN, NOW)
    preference = LocalPreference(LocalPreferenceKey.EVENT_RADIUS, "50", NOW)
    cursor = SourceCursor(
        source="spotify",
        capability=SourceCapability.SAVED_ITEMS,
        cursor="opaque:page-2",
        updated_at=NOW,
    )
    inbox = InboxEntry("inbox-1", "signal-1", InboxState.UNREAD, NOW, NOW)

    assert override.action is WatchlistAction.PIN
    assert preference.value == "50"
    assert cursor.cursor == "opaque:page-2"
    assert inbox.state is InboxState.UNREAD
    with pytest.raises(ValueError, match="LocalPreferenceKey"):
        LocalPreference("freeform", "value", NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="event radius"):
        LocalPreference(LocalPreferenceKey.EVENT_RADIUS, "500", NOW)
    with pytest.raises(ValueError, match="updated_at"):
        InboxEntry("inbox-2", "signal-2", InboxState.SAVED, NOW, NOW - timedelta(seconds=1))


def test_refresh_summary_and_signal_explanation_are_bounded_canonical_structures() -> None:
    explanation = Explanation(
        (
            ExplanationReason(ExplanationReasonKind.MONITORED_ARTIST, "Artist 1"),
            ExplanationReason(ExplanationReasonKind.NEW_RELEASE, None),
        )
    )
    signal = Signal(
        local_id="signal-1",
        kind=SignalKind.RELEASE,
        record_local_id="release-1",
        provider="spotify",
        provider_native_id="album-1",
        fingerprint="release-fingerprint-v1",
        material_version="material-v1",
        explanation=explanation,
        observed_at=NOW,
    )
    summary = RefreshSummary(
        (
            RefreshMetric(RefreshMetricKind.RECORDS_SEEN, 12),
            RefreshMetric(RefreshMetricKind.RECORDS_CREATED, 3),
        )
    )
    run = RefreshRun(
        local_id="run-1",
        source="spotify",
        kind=RefreshKind.CATALOG,
        status=RefreshStatus.SUCCEEDED,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        summary=summary,
    )

    assert signal.explanation == explanation
    assert run.summary.metrics[1].count == 3
    with pytest.raises(ValueError, match="duplicate explanation"):
        Explanation(
            (
                ExplanationReason(ExplanationReasonKind.NEW_RELEASE, None),
                ExplanationReason(ExplanationReasonKind.NEW_RELEASE, None),
            )
        )
    with pytest.raises(ValueError, match="safe inert text"):
        Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "<execute>"),))
    with pytest.raises(ValueError, match="finished_at"):
        RefreshRun(
            "run-running",
            "spotify",
            RefreshKind.CATALOG,
            RefreshStatus.RUNNING,
            NOW,
            NOW,
            RefreshSummary(()),
        )
    with pytest.raises(ValueError, match="finished_at"):
        RefreshRun(
            "run-failed",
            "spotify",
            RefreshKind.CATALOG,
            RefreshStatus.FAILED,
            NOW,
            None,
            RefreshSummary(()),
        )
