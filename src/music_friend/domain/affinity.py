"""Deterministic affinity-v1 calculation over canonical evidence."""

from __future__ import annotations

from collections.abc import Sequence

from music_friend.domain.models import AffinityEvidence, AffinityEvidenceKind, AffinityScore


def score_affinity(evidence: Sequence[AffinityEvidence]) -> AffinityScore:
    """Return an immutable affinity-v1 explanation for one artist's evidence."""
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise ValueError("evidence must be a sequence")
    if not all(isinstance(item, AffinityEvidence) for item in evidence):
        raise ValueError("evidence must contain affinity evidence")
    artist_ids = {item.artist_local_id for item in evidence}
    if len(artist_ids) > 1:
        raise ValueError("evidence must describe one artist")

    followed_count = sum(item.kind is AffinityEvidenceKind.FOLLOWED for item in evidence)
    saved_tracks = {
        (item.source, item.evidence_key)
        for item in evidence
        if item.kind is AffinityEvidenceKind.SAVED_TRACK
    }
    short_rank = _best_rank(evidence, AffinityEvidenceKind.TOP_SHORT_TERM)
    medium_rank = _best_rank(evidence, AffinityEvidenceKind.TOP_MEDIUM_TERM)
    long_rank = _best_rank(evidence, AffinityEvidenceKind.TOP_LONG_TERM)
    followed_points = 100 if followed_count else 0
    saved_points = min(len(saved_tracks) * 2, 40)
    short_points = 0 if short_rank is None else (51 - short_rank) * 3
    medium_points = 0 if medium_rank is None else (51 - medium_rank) * 2
    long_points = 0 if long_rank is None else 51 - long_rank
    return AffinityScore(
        total_points=(followed_points + saved_points + short_points + medium_points + long_points),
        followed_count=followed_count,
        followed_points=followed_points,
        saved_track_count=len(saved_tracks),
        saved_track_points=saved_points,
        top_short_term_rank=short_rank,
        top_short_term_points=short_points,
        top_medium_term_rank=medium_rank,
        top_medium_term_points=medium_points,
        top_long_term_rank=long_rank,
        top_long_term_points=long_points,
    )


def _best_rank(evidence: Sequence[AffinityEvidence], kind: AffinityEvidenceKind) -> int | None:
    ranks = (item.rank for item in evidence if item.kind is kind)
    return min((rank for rank in ranks if rank is not None), default=None)


__all__ = ["score_affinity"]
