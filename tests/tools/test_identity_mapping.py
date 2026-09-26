"""Branch coverage for run_identity_mapping's exact/name-search/retry/user-override paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    Artist,
    IdentityConfidence,
    SourceReference,
    WatchlistAction,
    WatchlistOverride,
)
from music_friend.store import Catalog
from music_friend.tools.identity_mapping import (
    MAPPING_RETRY_INTERVAL,
    run_identity_mapping,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SPOTIFY_ID = "spotify-artist-1"
MBID = "11111111-1111-1111-1111-111111111111"


def _artist(local_id: str, name: str, *, with_spotify_ref: bool = True) -> Artist:
    refs: tuple[SourceReference, ...] = ()
    if with_spotify_ref:
        refs = (
            SourceReference(
                source="spotify",
                native_id=SPOTIFY_ID,
                canonical_url=f"https://open.spotify.com/artist/{SPOTIFY_ID}",
                observed_at=NOW,
            ),
        )
    return Artist(local_id, name, refs, IdentityConfidence.SOURCE_ONLY, NOW)


def _watchlist(catalog: Catalog, artist: Artist) -> None:
    catalog.put_artist(artist)
    catalog.put_watchlist_override(WatchlistOverride(artist.local_id, WatchlistAction.ADD, NOW))


class FakeMusicBrainzSource:
    """A minimal double exposing only the two mapping-relevant methods."""

    def __init__(
        self,
        *,
        url_hits: dict[str, str | None] | None = None,
        name_hits: list[dict[str, object]] | None = None,
        raise_on_url: Exception | None = None,
        raise_on_name: Exception | None = None,
    ) -> None:
        self._url_hits = url_hits or {}
        self._name_hits = name_hits or []
        self._raise_on_url = raise_on_url
        self._raise_on_name = raise_on_name
        self.url_calls: list[tuple[str, ...]] = []
        self.name_calls: list[str] = []

    def lookup_artists_by_spotify_urls(self, urls: object) -> dict[str, str | None]:
        self.url_calls.append(tuple(urls))  # type: ignore[arg-type]
        if self._raise_on_url is not None:
            raise self._raise_on_url
        return {url: self._url_hits.get(url) for url in urls}  # type: ignore[union-attr]

    def search_artist_by_name(self, name: str, limit: int = 3) -> list[dict[str, object]]:
        self.name_calls.append(name)
        if self._raise_on_name is not None:
            raise self._raise_on_name
        return self._name_hits


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog.open(tmp_path / "catalog.sqlite3") as opened:
        yield opened


def test_exact_url_relation_hit_is_mapped_with_external_id_confidence(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist")
    _watchlist(catalog, artist)
    url = f"https://open.spotify.com/artist/{SPOTIFY_ID}"
    source = FakeMusicBrainzSource(url_hits={url: MBID})

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    updated = catalog.get_artist("artist-1")
    assert updated is not None
    mb_refs = [ref for ref in updated.source_refs if ref.source == "musicbrainz"]
    assert len(mb_refs) == 1
    assert mb_refs[0].native_id == MBID
    assert mb_refs[0].confidence == IdentityConfidence.EXTERNAL_ID
    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "mapped"
    assert mapping["method"] == "url_rel"
    assert source.name_calls == []


def test_ambiguous_url_relation_falls_back_to_name_search(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist")
    _watchlist(catalog, artist)
    url = f"https://open.spotify.com/artist/{SPOTIFY_ID}"
    source = FakeMusicBrainzSource(
        url_hits={url: None},  # ambiguous/unresolved batch result
        name_hits=[{"id": MBID, "score": 95}],
    )

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    assert source.name_calls == ["Synthetic Artist"]
    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "mapped"
    assert mapping["method"] == "name_search"
    updated = catalog.get_artist("artist-1")
    assert updated is not None
    mb_refs = [ref for ref in updated.source_refs if ref.source == "musicbrainz"]
    assert mb_refs[0].confidence == IdentityConfidence.SOURCE_ONLY


def test_name_search_accepted_at_ninety_with_a_distant_second_hit(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(
        name_hits=[{"id": MBID, "score": 90}, {"id": "other-mbid", "score": 60}]
    )

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "mapped"
    updated = catalog.get_artist("artist-1")
    assert updated is not None
    assert any(ref.native_id == MBID for ref in updated.source_refs)


def test_name_search_rejected_when_two_hits_are_both_at_or_above_ninety(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(
        name_hits=[{"id": MBID, "score": 92}, {"id": "other-mbid", "score": 91}]
    )

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "unmapped"
    updated = catalog.get_artist("artist-1")
    assert updated is not None
    assert not any(ref.source == "musicbrainz" for ref in updated.source_refs)


def test_unmapped_artist_is_not_retried_before_the_retry_window_elapses(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(name_hits=[])

    run_identity_mapping(catalog, source, "musicbrainz", NOW)
    assert source.name_calls == ["Synthetic Artist"]

    just_before_window = NOW + MAPPING_RETRY_INTERVAL - timedelta(minutes=1)
    run_identity_mapping(catalog, source, "musicbrainz", just_before_window)
    assert source.name_calls == ["Synthetic Artist"]  # not retried yet


def test_unmapped_artist_is_retried_after_the_retry_window_elapses(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(name_hits=[])

    run_identity_mapping(catalog, source, "musicbrainz", NOW)
    assert source.name_calls == ["Synthetic Artist"]

    after_window = NOW + MAPPING_RETRY_INTERVAL + timedelta(minutes=1)
    run_identity_mapping(catalog, source, "musicbrainz", after_window)
    assert source.name_calls == ["Synthetic Artist", "Synthetic Artist"]


def test_user_confirmed_mbid_always_wins_over_an_automated_mapping(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist")
    _watchlist(catalog, artist)
    url = f"https://open.spotify.com/artist/{SPOTIFY_ID}"
    source = FakeMusicBrainzSource(url_hits={url: MBID})
    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    # A user override, applied the way update_watchlist would apply it: replace the
    # musicbrainz ref with USER_CONFIRMED confidence and a "user" mapping method.
    updated = catalog.get_artist("artist-1")
    assert updated is not None
    user_mbid = "22222222-2222-2222-2222-222222222222"
    replaced_refs = tuple(ref for ref in updated.source_refs if ref.source != "musicbrainz") + (
        SourceReference(
            source="musicbrainz",
            native_id=user_mbid,
            canonical_url=f"https://musicbrainz.org/artist/{user_mbid}",
            observed_at=NOW,
            confidence=IdentityConfidence.USER_CONFIRMED,
        ),
    )
    catalog.put_artist(
        Artist(
            updated.local_id, updated.display_name, replaced_refs, updated.identity_confidence, NOW
        )
    )
    catalog.put_artist_identity_mapping(
        artist_local_id="artist-1",
        source="musicbrainz",
        status="mapped",
        method="user",
        attempted_at=NOW,
    )

    # A later mapping run must not overwrite the user-confirmed identity: the artist
    # already carries a musicbrainz SourceReference, so it is excluded from mapping.
    run_identity_mapping(catalog, source, "musicbrainz", NOW + timedelta(days=1))

    final = catalog.get_artist("artist-1")
    assert final is not None
    mb_refs = [ref for ref in final.source_refs if ref.source == "musicbrainz"]
    assert len(mb_refs) == 1
    assert mb_refs[0].native_id == user_mbid
    assert mb_refs[0].confidence == IdentityConfidence.USER_CONFIRMED


def test_batch_url_lookup_failure_falls_back_to_name_search(catalog: Catalog) -> None:
    from music_friend.errors import InvalidSourceResponseError

    artist = _artist("artist-1", "Synthetic Artist")
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(
        raise_on_url=InvalidSourceResponseError(),
        name_hits=[{"id": MBID, "score": 95}],
    )

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    assert source.name_calls == ["Synthetic Artist"]
    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "mapped"


def test_name_search_failure_records_unmapped_and_continues(catalog: Catalog) -> None:
    from music_friend.errors import SourceUnavailableError

    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    source = FakeMusicBrainzSource(raise_on_name=SourceUnavailableError())

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    mapping = catalog.get_artist_identity_mapping("artist-1", "musicbrainz")
    assert mapping is not None
    assert mapping["status"] == "unmapped"
    assert mapping["method"] == "name_search"


def test_duplicate_watchlist_entries_for_the_same_artist_are_deduplicated(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    catalog.put_artist(artist)
    catalog.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, NOW))
    catalog.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.PIN, NOW))
    source = FakeMusicBrainzSource(name_hits=[{"id": MBID, "score": 95}])

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    assert source.name_calls == ["Synthetic Artist"]


def test_a_non_string_attempted_at_is_treated_as_not_recently_attempted(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist", with_spotify_ref=False)
    _watchlist(catalog, artist)
    # A row inserted with a status other than "unmapped" is not retry-gated at all;
    # exercise the mapped-status short-circuit branch of _skip_for_retry_window.
    catalog.put_artist_identity_mapping(
        artist_local_id="artist-1",
        source="musicbrainz",
        status="mapped",
        method="user",
        attempted_at=NOW,
    )
    source = FakeMusicBrainzSource(name_hits=[])

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    # artist-1 already has no musicbrainz SourceReference recorded via put_artist, so
    # it is still a mapping candidate; the "mapped" status row does not gate retry.
    assert source.name_calls == ["Synthetic Artist"]


def test_empty_name_search_hits_are_rejected() -> None:
    from music_friend.tools.identity_mapping import _accept_name_search

    assert _accept_name_search([]) is None


def test_artists_already_mapped_are_excluded_from_the_candidate_set(catalog: Catalog) -> None:
    artist = _artist("artist-1", "Synthetic Artist")
    mapped_refs = artist.source_refs + (
        SourceReference(
            source="musicbrainz",
            native_id=MBID,
            canonical_url=f"https://musicbrainz.org/artist/{MBID}",
            observed_at=NOW,
            confidence=IdentityConfidence.EXTERNAL_ID,
        ),
    )
    already_mapped = Artist(
        artist.local_id, artist.display_name, mapped_refs, artist.identity_confidence, NOW
    )
    _watchlist(catalog, already_mapped)
    source = FakeMusicBrainzSource()

    run_identity_mapping(catalog, source, "musicbrainz", NOW)

    assert source.url_calls == []
    assert source.name_calls == []
