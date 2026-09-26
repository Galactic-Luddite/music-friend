"""Behavioral tests for MusicBrainzSource against a fake injected transport."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

import pytest

from music_friend.domain import IdentityConfidence, SourceReference
from music_friend.errors import (
    CapabilityUnsupportedError,
    InvalidSourceResponseError,
    RateLimitedError,
)
from music_friend.providers import Capability
from music_friend.providers.musicbrainz.source import MusicBrainzSource
from tests.providers.musicbrainz.live_fixtures import (
    LIVE_SEARCH_ARTIST_MBID,
    load_live_release_group_search,
)

FIXED_NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


class FakeTransport:
    """Records calls and returns scripted responses for one MusicBrainzSource test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._responses: list[object] = []
        self._raise: Exception | None = None

    def queue(self, response: object) -> None:
        self._responses.append(response)

    def raise_next(self, error: Exception) -> None:
        self._raise = error

    def get(self, path: str, query: Mapping[str, object] | None = None) -> object:
        self.calls.append((path, dict(query or {})))
        if self._raise is not None:
            error, self._raise = self._raise, None
            raise error
        return self._responses.pop(0)

    def close(self) -> None:
        pass


def _source(transport: FakeTransport) -> MusicBrainzSource:
    return MusicBrainzSource(transport=transport, clock=lambda: FIXED_NOW)


def _mb_ref(mbid: str) -> SourceReference:
    return SourceReference(
        source="musicbrainz",
        native_id=mbid,
        canonical_url=f"https://musicbrainz.org/artist/{mbid}",
        observed_at=FIXED_NOW,
        confidence=IdentityConfidence.EXTERNAL_ID,
    )


def test_capabilities_are_recent_releases_only() -> None:
    source = _source(FakeTransport())
    capabilities = source.capabilities()
    assert capabilities.supported == frozenset({Capability.RECENT_RELEASES})
    assert capabilities.granted == frozenset({Capability.RECENT_RELEASES})


@pytest.mark.parametrize(
    "call",
    [
        lambda source: source.search_artists("query", 10),
        lambda source: source.followed_artists(),
        lambda source: source.saved_items(),
        lambda source: source.top_items("medium_term", 10),
        lambda source: source.top_artists("medium_term", 10),
    ],
)
def test_unsupported_methods_raise_before_any_transport_call(call: object) -> None:
    transport = FakeTransport()
    source = _source(transport)
    with pytest.raises(CapabilityUnsupportedError):
        call(source)  # type: ignore[operator]
    assert transport.calls == []


def test_health_does_not_perform_a_transport_call() -> None:
    transport = FakeTransport()
    source = _source(transport)
    health = source.health()
    assert transport.calls == []
    assert health.capabilities.granted == frozenset({Capability.RECENT_RELEASES})


def test_recent_releases_normalizes_a_synthetic_release_group() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    rgid = "22222222-2222-2222-2222-222222222222"
    transport.queue(
        {
            "release-groups": [
                {
                    "id": rgid,
                    "title": "Synthetic Album",
                    "primary-type": "Album",
                    "first-release-date": "2026-05-01",
                    "score": 100,
                    "artist-credit": [{"artist": {"id": mbid}}],
                }
            ],
            "release-group-count": 1,
        }
    )
    source = _source(transport)
    page = source.recent_releases((_mb_ref(mbid),), FIXED_NOW)
    assert len(page.items) == 1
    release = page.items[0]
    assert release.title == "Synthetic Album"
    assert release.release_type == "album"
    assert page.next_cursor is None
    assert transport.calls[0][0] == "release-group"
    query = transport.calls[0][1]
    assert query["query"] == (
        f"arid:{mbid} AND firstreleasedate:[2026-09-26 TO *] "
        "AND status:official AND primarytype:(Album OR Single OR EP)"
    )


def test_recent_releases_filters_out_results_without_a_score() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue(
        {
            "release-groups": [
                {
                    "id": "no-score",
                    "title": "Unscored",
                    "primary-type": "Album",
                    "first-release-date": "2026-05-01",
                    "artist-credit": [{"artist": {"id": mbid}}],
                }
            ],
        }
    )
    source = _source(transport)
    page = source.recent_releases((_mb_ref(mbid),), FIXED_NOW)
    assert page.items == ()


def test_recent_releases_rejects_a_malformed_response() -> None:
    transport = FakeTransport()
    transport.queue({"unexpected": "shape"})
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((_mb_ref("mbid"),), FIXED_NOW)


def test_recent_releases_propagates_rate_limited_error() -> None:
    transport = FakeTransport()
    transport.raise_next(RateLimitedError(retry_after_seconds=5))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.recent_releases((_mb_ref("mbid"),), FIXED_NOW)


def _url_response(*entries: dict[str, object]) -> dict[str, object]:
    """The verified real MusicBrainz /ws/2/url batch response shape."""
    return {"url-count": len(entries), "url-offset": 0, "urls": list(entries)}


def test_lookup_artists_by_spotify_urls_exact_hit_returns_the_single_artist() -> None:
    transport = FakeTransport()
    mbid = "33333333-3333-3333-3333-333333333333"
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(
        _url_response(
            {
                "resource": url,
                "relations": [
                    {"type": "free streaming", "target-type": "artist", "artist": {"id": mbid}}
                ],
            }
        )
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: mbid}


def test_lookup_artists_by_spotify_urls_ambiguous_relation_returns_none() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(
        _url_response(
            {
                "resource": url,
                "relations": [
                    {
                        "type": "free streaming",
                        "target-type": "artist",
                        "artist": {"id": "mbid-one"},
                    },
                    {
                        "type": "social network",
                        "target-type": "artist",
                        "artist": {"id": "mbid-two"},
                    },
                ],
            }
        )
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_no_relation_returns_none() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(_url_response({"resource": url, "relations": []}))
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_omitted_unknown_url_returns_none() -> None:
    """Verified live: an unmatched URL is OMITTED from ``urls`` entirely, not an
    error entry -- "unresolved" must be computed as (requested) minus (present),
    never by looking for a per-input error or empty entry."""
    transport = FakeTransport()
    known_url = "https://open.spotify.com/artist/known"
    unknown_url = "https://open.spotify.com/artist/unknown"
    mbid = "33333333-3333-3333-3333-333333333333"
    transport.queue(
        _url_response(
            {
                "resource": known_url,
                "relations": [
                    {"type": "free streaming", "target-type": "artist", "artist": {"id": mbid}}
                ],
            }
        )
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((known_url, unknown_url))
    assert result == {known_url: mbid, unknown_url: None}


def test_lookup_artists_by_spotify_urls_propagates_rate_limited_error() -> None:
    transport = FakeTransport()
    transport.raise_next(RateLimitedError(retry_after_seconds=5))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.lookup_artists_by_spotify_urls(("https://open.spotify.com/artist/x",))


def test_lookup_artists_by_spotify_urls_batches_at_most_one_hundred_per_call() -> None:
    transport = FakeTransport()
    urls = tuple(f"https://open.spotify.com/artist/{index}" for index in range(150))
    transport.queue(_url_response())
    transport.queue(_url_response())
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls(urls)
    assert len(transport.calls) == 2
    assert len(transport.calls[0][1]["resource"]) == 100  # type: ignore[arg-type]
    assert len(transport.calls[1][1]["resource"]) == 50  # type: ignore[arg-type]
    assert set(result) == set(urls)


def test_search_artist_by_name_accepts_and_sorts_by_score() -> None:
    transport = FakeTransport()
    transport.queue(
        {
            "artists": [
                {"id": "low", "score": 70},
                {"id": "high", "score": 95},
            ]
        }
    )
    source = _source(transport)
    hits = source.search_artist_by_name("Synthetic Artist", limit=3)
    assert hits[0]["id"] == "high"
    assert hits[0]["score"] == 95


def test_search_artist_by_name_rejects_a_malformed_response() -> None:
    transport = FakeTransport()
    transport.queue({"unexpected": "shape"})
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.search_artist_by_name("Synthetic Artist")


def test_search_artist_by_name_rejects_a_non_mapping_response() -> None:
    transport = FakeTransport()
    transport.queue(["not", "a", "mapping"])
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.search_artist_by_name("Synthetic Artist")


def test_recent_releases_rejects_a_non_mapping_response() -> None:
    transport = FakeTransport()
    transport.queue(["not", "a", "mapping"])
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((_mb_ref("mbid"),), FIXED_NOW)


def test_recent_releases_skips_non_mapping_release_group_entries() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue({"release-groups": ["not-a-mapping"]})
    source = _source(transport)
    page = source.recent_releases((_mb_ref(mbid),), FIXED_NOW)
    assert page.items == ()


def test_recent_releases_rejects_more_or_fewer_than_one_artist_ref() -> None:
    source = _source(FakeTransport())
    with pytest.raises(ValueError):
        source.recent_releases((), FIXED_NOW)


def test_recent_releases_rejects_a_non_musicbrainz_artist_ref() -> None:
    source = _source(FakeTransport())
    spotify_ref = SourceReference(
        source="spotify",
        native_id="spotify-id",
        canonical_url=None,
        observed_at=FIXED_NOW,
    )
    with pytest.raises(ValueError):
        source.recent_releases((spotify_ref,), FIXED_NOW)


def test_recent_releases_falls_back_to_offset_zero_for_a_malformed_cursor() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue({"release-groups": []})
    source = _source(transport)
    source.recent_releases((_mb_ref(mbid),), FIXED_NOW, cursor="not-an-int")
    assert transport.calls[0][1]["offset"] == "0"


def test_recent_releases_emits_a_next_cursor_from_the_live_search_count() -> None:
    """Issue #49: the /ws/2/release-group search response carries its total in
    ``count``, not ``release-group-count``. The page below is a verbatim live
    response (2 of 196 rows), so the next cursor must be the next offset."""
    live = load_live_release_group_search()
    assert "release-group-count" not in live
    assert live["count"] > len(live["release-groups"])
    transport = FakeTransport()
    transport.queue(live)
    source = _source(transport)
    page = source.recent_releases((_mb_ref(LIVE_SEARCH_ARTIST_MBID),), FIXED_NOW)
    assert page.next_cursor == str(len(live["release-groups"]))
    assert transport.calls[0][1]["limit"] == "100"


def test_recent_releases_stops_on_an_empty_page_even_when_count_claims_more() -> None:
    live = load_live_release_group_search()
    transport = FakeTransport()
    transport.queue({**live, "offset": 2, "release-groups": []})
    source = _source(transport)
    page = source.recent_releases((_mb_ref(LIVE_SEARCH_ARTIST_MBID),), FIXED_NOW, cursor="2")
    assert page.items == ()
    assert page.next_cursor is None


def test_recent_releases_rejects_a_non_list_release_groups_field() -> None:
    transport = FakeTransport()
    transport.queue({"release-groups": "not-a-list"})
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((_mb_ref("mbid"),), FIXED_NOW)


def test_lookup_artists_by_spotify_urls_rejects_a_non_sequence() -> None:
    source = _source(FakeTransport())
    with pytest.raises(ValueError):
        source.lookup_artists_by_spotify_urls(123)  # type: ignore[arg-type]


def test_lookup_artists_by_spotify_urls_batch_response_not_a_mapping_returns_none() -> None:
    transport = FakeTransport()
    urls = tuple(f"https://open.spotify.com/artist/{index}" for index in range(2))
    transport.queue("not-a-mapping")
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls(urls)
    assert result == {url: None for url in urls}


def test_lookup_artists_by_spotify_urls_batch_missing_urls_field_returns_none() -> None:
    transport = FakeTransport()
    urls = tuple(f"https://open.spotify.com/artist/{index}" for index in range(2))
    transport.queue({"unexpected": "shape"})
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls(urls)
    assert result == {url: None for url in urls}


def test_lookup_artists_by_spotify_urls_batch_unmatched_resource_returns_none() -> None:
    transport = FakeTransport()
    urls = ("https://open.spotify.com/artist/a", "https://open.spotify.com/artist/b")
    transport.queue(_url_response({"resource": "https://open.spotify.com/artist/a"}))
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls(urls)
    assert result["https://open.spotify.com/artist/b"] is None


def test_lookup_artists_by_spotify_urls_non_list_relations_returns_none() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(_url_response({"resource": url, "relations": "not-a-list"}))
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_ignores_malformed_relation_entries() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(
        _url_response(
            {
                "resource": url,
                "relations": [
                    {"type": "official homepage", "artist": "not-a-mapping"},
                    "not-a-mapping",
                ],
            }
        )
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_excludes_relations_with_a_different_target_type() -> None:
    """A relation object with an explicit target-type other than "artist" (verified
    live shape: every artist relation carries both "type": "<name>" and
    "target-type": "artist") must not count toward the ambiguity/exact-hit tally."""
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    mbid = "33333333-3333-3333-3333-333333333333"
    transport.queue(
        _url_response(
            {
                "resource": url,
                "relations": [
                    {"type": "free streaming", "target-type": "artist", "artist": {"id": mbid}},
                    {"type": "part of", "target-type": "release-group", "artist": {"id": "rg"}},
                ],
            }
        )
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: mbid}


def test_lookup_artists_by_spotify_urls_deduplicates_repeated_urls() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(_url_response({"resource": url, "relations": []}))
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url, url, url))
    assert len(transport.calls[0][1]["resource"]) == 1  # type: ignore[arg-type]
    assert result == {url: None}


def test_search_artist_by_name_returns_empty_for_blank_name() -> None:
    transport = FakeTransport()
    source = _source(transport)
    assert source.search_artist_by_name("   ") == []
    assert transport.calls == []


def test_search_artist_by_name_rejects_a_non_list_artists_field() -> None:
    transport = FakeTransport()
    transport.queue({"artists": "not-a-list"})
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.search_artist_by_name("Synthetic Artist")


def test_search_artist_by_name_skips_malformed_entries_and_respects_limit() -> None:
    transport = FakeTransport()
    transport.queue(
        {
            "artists": [
                "not-a-mapping",
                {"id": "no-score"},
                {"id": "a", "score": 80},
                {"id": "b", "score": 95},
                {"id": "c", "score": 60},
            ]
        }
    )
    source = _source(transport)
    hits = source.search_artist_by_name("Synthetic Artist", limit=2)
    assert [hit["id"] for hit in hits] == ["b", "a"]


def test_close_and_context_manager_close_the_injected_transport() -> None:
    transport = FakeTransport()
    source = _source(transport)
    source.close()
    with _source(FakeTransport()):
        pass


def test_now_rejects_a_naive_clock_result() -> None:
    source = MusicBrainzSource(transport=FakeTransport(), clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        source._now()


def test_search_artist_by_name_propagates_rate_limited_error() -> None:
    transport = FakeTransport()
    transport.raise_next(RateLimitedError(retry_after_seconds=5))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.search_artist_by_name("Synthetic Artist")


def test_deezer_artist_id_extracts_a_single_relation() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue(
        {
            "relations": [
                {
                    "type": "free streaming",
                    "url": {"resource": "https://open.spotify.com/artist/x"},
                },
                {
                    "type": "free streaming",
                    "url": {"resource": "https://www.deezer.com/artist/1000001"},
                },
            ]
        }
    )
    source = _source(transport)
    assert source.deezer_artist_id(mbid) == "1000001"
    assert transport.calls[0][0] == f"artist/{mbid}"
    assert transport.calls[0][1]["inc"] == "url-rels"


def test_deezer_artist_id_returns_none_for_no_relation() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue({"relations": []})
    source = _source(transport)
    assert source.deezer_artist_id(mbid) is None


def test_deezer_artist_id_returns_none_for_ambiguous_relations() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue(
        {
            "relations": [
                {"type": "free streaming", "url": {"resource": "https://www.deezer.com/artist/1"}},
                {"type": "free streaming", "url": {"resource": "https://www.deezer.com/artist/2"}},
            ]
        }
    )
    source = _source(transport)
    assert source.deezer_artist_id(mbid) is None


def test_deezer_artist_id_rejects_a_malformed_response() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.queue({"relations": "not-a-list"})
    source = _source(transport)
    with pytest.raises(InvalidSourceResponseError):
        source.deezer_artist_id(mbid)


def test_deezer_artist_id_propagates_rate_limited() -> None:
    transport = FakeTransport()
    mbid = "11111111-1111-1111-1111-111111111111"
    transport.raise_next(RateLimitedError(30))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.deezer_artist_id(mbid)
