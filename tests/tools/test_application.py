from datetime import datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    IdentityConfidence,
    LocalPreference,
    LocalPreferenceKey,
    SourceReference,
    WatchlistAction,
    WatchlistInclusionReason,
    WatchlistOverride,
)
from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _artist() -> Artist:
    return Artist(
        "artist-1",
        "Artist",
        (SourceReference("spotify", "artist-native", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def test_application_requires_an_explicit_catalog_and_delegates_repository_operations(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        MusicFriendApplication()  # type: ignore[call-arg]
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        application.put_artist(_artist())

        assert application.get_artist("artist-1") == _artist()
        assert application.search_artists("Artist", limit=10) == (_artist(),)


def test_application_delegates_portable_lifecycle_without_opening_default_paths(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    catalog = Catalog.open(catalog_path)
    application = MusicFriendApplication(catalog)
    application.put_artist(_artist())
    destination = tmp_path / "export.json"

    result = application.export_data(destination, exported_at=NOW)

    assert result.path == destination
    application.delete_data()
    assert not catalog_path.exists()
    with pytest.raises(CatalogUnavailableError):
        application.get_artist("artist-1")


def test_application_exposes_explicit_watchlist_actions_and_explanations(tmp_path: Path) -> None:
    """Catches action wiring drift or application-layer score reconstruction."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist()
        application.put_artist(artist)
        application.put_affinity_evidence(
            AffinityEvidence(
                "evidence-1",
                artist.local_id,
                "spotify",
                AffinityEvidenceKind.SAVED_TRACK,
                "track-1",
                None,
                NOW,
            )
        )

        application.set_watchlist_pin(artist.local_id, updated_at=NOW)
        pinned = application.explain_watchlist(artist.local_id)

        assert pinned is not None
        assert pinned.inclusion_reason is WatchlistInclusionReason.PINNED
        assert pinned.affinity == application.get_affinity_score(artist.local_id)
        assert application.list_watchlist(limit=1) == (pinned,)

        application.set_watchlist_mute(artist.local_id, updated_at=NOW)
        assert application.explain_watchlist(artist.local_id) is None

        application.set_watchlist_add(artist.local_id, updated_at=NOW)
        added = application.explain_watchlist(artist.local_id)
        assert added is not None
        assert added.inclusion_reason is WatchlistInclusionReason.MANUALLY_ADDED

        application.remove_watchlist_override(artist.local_id)
        automatic = application.explain_watchlist(artist.local_id)
        assert automatic is not None
        assert automatic.inclusion_reason is WatchlistInclusionReason.AUTOMATIC


def test_application_delegates_preferences_evidence_overrides_and_transactions(
    tmp_path: Path,
) -> None:
    """Catches the provider-neutral application boundary dropping durable local user state."""
    with pytest.raises(ValueError, match="catalog"):
        MusicFriendApplication(object())  # type: ignore[arg-type]
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist()
        evidence = AffinityEvidence(
            "evidence-1",
            artist.local_id,
            "spotify",
            AffinityEvidenceKind.FOLLOWED,
            "artist-native",
            None,
            NOW,
        )
        override = WatchlistOverride(artist.local_id, WatchlistAction.PIN, NOW)
        preference = LocalPreference(LocalPreferenceKey.EVENT_COUNTRY_CODE, "US", NOW)

        with application.transaction():
            application.put_artist(artist)
            application.put_affinity_evidence(evidence)
            application.put_watchlist_override(override)
            application.put_local_preference(preference)

        assert application.get_affinity_evidence(evidence.local_id) == evidence
        assert application.list_affinity_evidence(artist.local_id, limit=10) == (evidence,)
        assert application.get_watchlist_override(artist.local_id) == override
        assert application.list_watchlist_overrides(limit=10) == (override,)
        assert application.get_local_preference(preference.key) == preference
        assert application.list_local_preferences(limit=10) == (preference,)

        application.replace_affinity_evidence("spotify", AffinityEvidenceKind.FOLLOWED, ())
        application.remove_local_preference(preference.key)
        assert application.get_affinity_evidence(evidence.local_id) is None
        assert application.get_local_preference(preference.key) is None
