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
    RefreshKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    SourceCapability,
    SourceCursor,
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


def test_application_delegates_refresh_run_and_source_cursor_lookups(tmp_path: Path) -> None:
    """Catches the refresh-run and source-cursor delegation methods drifting from the catalog."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        run = RefreshRun(
            "run-1",
            "spotify",
            RefreshKind.CATALOG,
            RefreshStatus.SUCCEEDED,
            NOW,
            NOW,
            RefreshSummary(()),
        )
        cursor = SourceCursor("spotify", SourceCapability.RECENT_RELEASES, "artist-1", NOW)
        application.put_refresh_run(run)
        application.put_source_cursor(cursor)

        assert application.get_refresh_run("run-1") == run
        assert application.list_source_cursors("spotify", limit=10) == (cursor,)


def test_application_delegates_purge_source(tmp_path: Path) -> None:
    """Catches the purge_source delegation drifting from the underlying portable helper."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        result = application.purge_source("spotify")
        assert result is not None


def test_confirm_artist_identity_sets_user_confirmed_source_reference(tmp_path: Path) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist()
        application.put_artist(artist)

        application.confirm_artist_identity(
            artist, source="musicbrainz", native_id="11111111-1111-1111-1111-111111111111", at=NOW
        )

        updated = application.get_artist("artist-1")
        assert updated is not None
        mb_refs = [ref for ref in updated.source_refs if ref.source == "musicbrainz"]
        assert len(mb_refs) == 1
        assert mb_refs[0].confidence == IdentityConfidence.USER_CONFIRMED
        mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
        assert mapping is not None
        assert mapping["status"] == "mapped"
        assert mapping["method"] == "user"


def test_confirm_artist_identity_rolls_back_both_writes_on_partial_failure(
    tmp_path: Path,
) -> None:
    """Catches the artist write and the mapping-table write committing independently:
    if either fails, neither must be visible afterward."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist()
        application.put_artist(artist)

        original_put = catalog.put_artist_identity_mapping

        def failing_put(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic failure after put_artist")

        catalog.put_artist_identity_mapping = failing_put  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError):
                application.confirm_artist_identity(
                    artist,
                    source="musicbrainz",
                    native_id="11111111-1111-1111-1111-111111111111",
                    at=NOW,
                )
        finally:
            catalog.put_artist_identity_mapping = original_put  # type: ignore[method-assign]

        unchanged = application.get_artist("artist-1")
        assert unchanged is not None
        assert not any(ref.source == "musicbrainz" for ref in unchanged.source_refs)
        assert catalog.get_artist_identity_mapping("artist-1", "musicbrainz") is None
