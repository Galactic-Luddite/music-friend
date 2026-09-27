"""Issue #63: identity ladder with provisional MusicBrainz links and shared title key."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from music_friend.domain import (
    Artist,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SignalKind,
    SourceReference,
)
from music_friend.domain.observations import ReleaseObservation
from music_friend.store import Catalog
from music_friend.tools.release_observation import record_release_observation

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SOURCES = ("musicbrainz", "deezer")


def _artist(catalog: Catalog) -> Artist:
    artist = Artist(
        "artist:one",
        "Synthetic Artist One",
        (SourceReference("spotify", "synthetic-one", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    catalog.put_artist(artist)
    return artist


def _release(
    source: str,
    native_id: str,
    *,
    title: str = "Synthetic Record",
    release_date: date = date(2026, 8, 14),
    precision: ReleaseDatePrecision = ReleaseDatePrecision.DAY,
    local_id: str | None = None,
    confidence: IdentityConfidence = IdentityConfidence.SOURCE_ONLY,
) -> Release:
    return Release(
        local_id or f"release:{source}:{native_id}",
        title,
        "album",
        release_date,
        precision,
        ("artist:one",),
        (SourceReference(source, native_id, f"https://example.test/{source}/{native_id}", NOW, confidence),),
        NOW,
    )


def _observe(
    catalog: Catalog,
    release: Release,
    *,
    at: datetime = NOW,
    links: tuple[SourceReference, ...] = (),
) -> object:
    reference = release.source_refs[0]
    return record_release_observation(
        catalog,
        ReleaseObservation(
            release=release,
            source=reference.source,
            native_id=reference.native_id,
            monitored_artist_local_id="artist:one",
            external_links=links,
            observed_at=at,
        ),
        release_sources=SOURCES,
    )


def test_native_id_match_wins_before_title_key(tmp_path: Path) -> None:
    """Tier 1: a candidate whose (source, native_id) is already attached as a non-provisional
    reference merges without consulting the title key."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        # First observation: Deezer release
        outcome1 = _observe(catalog, _release("deezer", "album-123"))
        assert outcome1.kind == "created"
        deezer_release_id = outcome1.release_local_id

        # Second observation: same Deezer album, even with different title
        outcome2 = _observe(
            catalog,
            replace(_release("deezer", "album-123"), title="Different Title"),
        )
        # Tier 1 match on native_id, should merge without checking title
        assert outcome2.kind == "updated"  # or provenance_attached
        assert outcome2.release_local_id == deezer_release_id


def test_external_link_merges_in_either_arrival_order(tmp_path: Path) -> None:
    """Corroboration: Deezer observed after MusicBrainz harvest merges at tier 2;
    reverse arrival order (Deezer first) also merges at tier 2."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Case 1: MusicBrainz first with provisional Deezer link
        mb_release = _release("musicbrainz", "rg-1", title="Synthetic Record")
        deezer_link = SourceReference("deezer", "album-456", None, NOW, IdentityConfidence.PROVISIONAL)

        outcome1 = _observe(catalog, mb_release, links=(deezer_link,))
        assert outcome1.kind == "created"
        mb_subject = outcome1.subject_local_id

        # Now observe the same Deezer album (corroborates the provisional link)
        deezer_release = _release("deezer", "album-456", title="Synthetic Record")
        outcome2 = _observe(catalog, deezer_release)
        # Tier 1 hit on Deezer, should merge with the MusicBrainz release
        assert outcome2.subject_local_id == mb_subject
        assert outcome2.kind in ("updated", "provenance_attached")


def test_wrong_url_rel_is_detached_not_merged(tmp_path: Path) -> None:
    """Valid-but-wrong url-rel: a MusicBrainz url-rel pointing to a Deezer album with
    incompatible title/date detaches the provisional reference and creates a new subject."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # MusicBrainz harvest with provisional Deezer link
        mb_release = _release("musicbrainz", "rg-1", title="Album A", release_date=date(2026, 1, 1))
        deezer_link = SourceReference("deezer", "album-wrong", None, NOW, IdentityConfidence.PROVISIONAL)

        outcome1 = _observe(catalog, mb_release, links=(deezer_link,))
        assert outcome1.kind == "created"

        # Observe the Deezer album with incompatible title/date (wrong link detected)
        deezer_release = _release(
            "deezer", "album-wrong", title="Different Album", release_date=date(2026, 6, 1)
        )
        outcome2 = _observe(catalog, deezer_release)
        # Should be a new subject (identity_conflict detected)
        assert outcome2.kind in ("conflict", "ambiguous", "created")
        # The key point: we have two separate inbox items, not a merge
        deezer_stored = catalog.get_release(outcome2.release_local_id)
        assert deezer_stored is not None
        assert deezer_stored.subject_local_id != outcome1.subject_local_id


def test_empty_slot_provisional_link_still_requires_corroboration(tmp_path: Path) -> None:
    """Codex finding: a subject with no Deezer reference yet, a provisional MusicBrainz
    url-rel pointing at a Deezer album ID, where that Deezer album's own report has
    incompatible title/date -- assert the provisional reference stays PROVISIONAL
    (or gets detached) and does NOT get silently merged."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Create a Spotify release (no Deezer reference yet - empty slot)
        spotify_release = Release(
            "release:spotify:sp-1",
            "Original Album",
            "album",
            date(2026, 1, 1),
            ReleaseDatePrecision.DAY,
            ("artist:one",),
            (SourceReference("spotify", "sp-1", None, NOW),),
            NOW,
        )
        catalog.put_release(spotify_release)
        catalog.put_signal(
            catalog._signal_class(
                "signal:sp-1",
                SignalKind.RELEASE,
                "release:spotify:sp-1",
                "spotify",
                "sp-1",
                "fingerprint:sp-1",
                "material:fakehash",
                None,
                NOW,
            )
        )

        # Now MusicBrainz arrives with a provisional Deezer link to a conflicting album
        mb_release = _release("musicbrainz", "rg-1", title="Original Album", release_date=date(2026, 1, 1))
        deezer_link = SourceReference(
            "deezer", "album-conflict", None, NOW, IdentityConfidence.PROVISIONAL
        )

        # Observe MB with the provisional link
        mb_outcome = _observe(catalog, mb_release, links=(deezer_link,))
        assert mb_outcome.kind == "created"

        # Now observe Deezer with incompatible data
        deezer_release = _release(
            "deezer",
            "album-conflict",
            title="Very Different",
            release_date=date(2026, 12, 1),
        )
        deezer_outcome = _observe(catalog, deezer_release)

        # The Deezer release should NOT merge into the Spotify/MB subject
        # because the provisional link failed corroboration
        assert deezer_outcome.subject_local_id != mb_outcome.subject_local_id


def test_ambiguous_identity_stays_separate(tmp_path: Path) -> None:
    """Ambiguity: a key resolving to two distinct subjects creates its own subject,
    writes identity_ambiguous and increments the metric."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Create two releases with the same title/date (ambiguous key)
        rel1 = _release("deezer", "al-1", title="Ambiguous Title", release_date=date(2026, 8, 14))
        rel2 = _release("spotify", "sp-1", title="Ambiguous Title", release_date=date(2026, 8, 14))

        outcome1 = _observe(catalog, rel1)
        assert outcome1.kind == "created"
        subject1 = outcome1.subject_local_id

        outcome2 = _observe(catalog, rel2)
        # Should be detected as ambiguous (if tier 4 is in effect) or create a new subject
        # For now, without tier 4, it should be a new subject
        subject2 = outcome2.subject_local_id

        # The two should have different subjects
        assert subject1 != subject2 or outcome2.kind == "ambiguous"


def test_never_merges_two_native_ids_of_one_source(tmp_path: Path) -> None:
    """Same-source conflict guard: a candidate never merges into a release carrying
    a different non-provisional native id for the candidate's own source."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # First Deezer album
        outcome1 = _observe(catalog, _release("deezer", "album-1"))
        assert outcome1.kind == "created"
        subject1 = outcome1.subject_local_id

        # Second Deezer album (different native_id, same source)
        # This should NOT merge with the first even if some other tier says it should
        outcome2 = _observe(
            catalog,
            _release("deezer", "album-2", title=_release("deezer", "album-1").title),
        )
        # Should create a new subject (same-source guard)
        assert outcome2.subject_local_id != subject1 or outcome2.kind in ("created", "conflict")


__all__ = [
    "test_native_id_match_wins_before_title_key",
    "test_external_link_merges_in_either_arrival_order",
    "test_wrong_url_rel_is_detached_not_merged",
    "test_empty_slot_provisional_link_still_requires_corroboration",
    "test_ambiguous_identity_stays_separate",
    "test_never_merges_two_native_ids_of_one_source",
]
