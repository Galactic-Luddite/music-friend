from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from music_friend.domain import AffinityEvidence, AffinityEvidenceKind, AffinityScore
from music_friend.domain.affinity import score_affinity

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _evidence(
    local_id: str,
    kind: AffinityEvidenceKind,
    evidence_key: str,
    *,
    rank: int | None = None,
    source: str = "spotify",
) -> AffinityEvidence:
    return AffinityEvidence(local_id, "artist-1", source, kind, evidence_key, rank, NOW)


def test_affinity_v1_counts_unique_saved_tracks_caps_points_and_uses_best_top_rank() -> None:
    """Catches double-counted tracks/top duplicates and any affinity-v1 weight drift."""
    evidence = [
        _evidence("followed-1", AffinityEvidenceKind.FOLLOWED, "artist-1"),
        _evidence(
            "followed-2",
            AffinityEvidenceKind.FOLLOWED,
            "artist-1",
            source="second-source",
        ),
    ]
    evidence.extend(
        _evidence(f"saved-{index}", AffinityEvidenceKind.SAVED_TRACK, f"track-{index}")
        for index in range(25)
    )
    evidence.append(
        _evidence(
            "saved-duplicate",
            AffinityEvidenceKind.SAVED_TRACK,
            "track-0",
        )
    )
    evidence.extend(
        (
            _evidence("short-10", AffinityEvidenceKind.TOP_SHORT_TERM, "duplicate-a", rank=10),
            _evidence("short-1", AffinityEvidenceKind.TOP_SHORT_TERM, "duplicate-b", rank=1),
            _evidence("medium-50", AffinityEvidenceKind.TOP_MEDIUM_TERM, "artist-1", rank=50),
            _evidence("long-50", AffinityEvidenceKind.TOP_LONG_TERM, "artist-1", rank=50),
        )
    )

    score = score_affinity(tuple(evidence))

    assert score == AffinityScore(
        total_points=293,
        followed_count=2,
        followed_points=100,
        saved_track_count=25,
        saved_track_points=40,
        top_short_term_rank=1,
        top_short_term_points=150,
        top_medium_term_rank=50,
        top_medium_term_points=2,
        top_long_term_rank=50,
        top_long_term_points=1,
    )
    with pytest.raises(FrozenInstanceError):
        score.total_points = 0  # type: ignore[misc]


def test_affinity_v1_zero_view_has_exact_zero_components() -> None:
    """Catches an empty-evidence artist receiving default affinity or phantom ranks."""
    assert score_affinity(()) == AffinityScore(
        total_points=0,
        followed_count=0,
        followed_points=0,
        saved_track_count=0,
        saved_track_points=0,
        top_short_term_rank=None,
        top_short_term_points=0,
        top_medium_term_rank=None,
        top_medium_term_points=0,
        top_long_term_rank=None,
        top_long_term_points=0,
    )


def test_affinity_rejects_non_evidence_and_mixed_artist_sequences() -> None:
    with pytest.raises(ValueError):
        score_affinity("evidence")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        score_affinity((object(),))  # type: ignore[arg-type]
    other = AffinityEvidence(
        "other", "artist-2", "spotify", AffinityEvidenceKind.FOLLOWED, "artist-2", None, NOW
    )
    with pytest.raises(ValueError):
        score_affinity((_evidence("first", AffinityEvidenceKind.FOLLOWED, "artist-1"), other))
