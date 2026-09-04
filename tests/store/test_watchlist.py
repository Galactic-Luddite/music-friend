from __future__ import annotations

from datetime import datetime, timezone

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    IdentityConfidence,
    SourceReference,
    WatchlistAction,
    WatchlistInclusionReason,
    WatchlistOverride,
)
from music_friend.store import Catalog

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _artist(local_id: str, name: str) -> Artist:
    return Artist(
        local_id,
        name,
        (SourceReference("spotify", f"native-{local_id}", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _followed(local_id: str) -> AffinityEvidence:
    return AffinityEvidence(
        f"evidence-{local_id}",
        local_id,
        "spotify",
        AffinityEvidenceKind.FOLLOWED,
        f"native-{local_id}",
        None,
        NOW,
    )


def test_watchlist_seeds_fifty_then_applies_manual_groups_without_duplicates(
    catalog: Catalog,
) -> None:
    """Catches a hard total-50 cap, muted inclusion, duplicate manual rows, and group drift."""
    for index in range(52):
        local_id = f"artist-{index:03d}"
        catalog.put_artist(_artist(local_id, f"Artist {index:03d}"))
        catalog.put_affinity_evidence(_followed(local_id))
    catalog.put_artist(_artist("artist-zero", "Zero Score"))
    catalog.put_watchlist_override(WatchlistOverride("artist-000", WatchlistAction.PIN, NOW))
    catalog.put_watchlist_override(WatchlistOverride("artist-001", WatchlistAction.MUTE, NOW))
    catalog.put_watchlist_override(WatchlistOverride("artist-051", WatchlistAction.ADD, NOW))
    catalog.put_watchlist_override(WatchlistOverride("artist-zero", WatchlistAction.ADD, NOW))

    entries = catalog.list_watchlist(limit=100)

    assert len(entries) == 52
    assert entries[0].artist.local_id == "artist-000"
    assert entries[0].inclusion_reason is WatchlistInclusionReason.PINNED
    assert entries[1].artist.local_id == "artist-051"
    assert entries[1].inclusion_reason is WatchlistInclusionReason.MANUALLY_ADDED
    assert entries[2].artist.local_id == "artist-zero"
    assert entries[2].inclusion_reason is WatchlistInclusionReason.MANUALLY_ADDED
    assert entries[2].affinity.total_points == 0
    automatic_ids = tuple(entry.artist.local_id for entry in entries[3:])
    assert automatic_ids == tuple(f"artist-{index:03d}" for index in range(2, 51))
    assert all(
        entry.inclusion_reason is WatchlistInclusionReason.AUTOMATIC for entry in entries[3:]
    )
    assert "artist-001" not in {entry.artist.local_id for entry in entries}
    assert len({entry.artist.local_id for entry in entries}) == len(entries)


def test_watchlist_uses_unicode_casefold_then_local_id_for_equal_scores(catalog: Catalog) -> None:
    """Catches SQLite-only ASCII ordering or unstable equal-name ties."""
    for local_id, name in (
        ("artist-b", "strasse"),
        ("artist-c", "Zed"),
        ("artist-a", "Straße"),
    ):
        catalog.put_artist(_artist(local_id, name))
        catalog.put_affinity_evidence(_followed(local_id))

    entries = catalog.list_watchlist(limit=3)

    assert tuple(entry.artist.local_id for entry in entries) == (
        "artist-a",
        "artist-b",
        "artist-c",
    )


def test_remove_override_returns_artist_to_automatic_eligibility(catalog: Catalog) -> None:
    """Catches override deletion leaving a stale muted or manual classification."""
    artist = _artist("artist-1", "Artist")
    catalog.put_artist(artist)
    catalog.put_affinity_evidence(_followed(artist.local_id))
    catalog.put_watchlist_override(WatchlistOverride(artist.local_id, WatchlistAction.MUTE, NOW))
    assert catalog.explain_watchlist(artist.local_id) is None

    catalog.remove_watchlist_override(artist.local_id)

    entry = catalog.explain_watchlist(artist.local_id)
    assert entry is not None
    assert entry.inclusion_reason is WatchlistInclusionReason.AUTOMATIC
