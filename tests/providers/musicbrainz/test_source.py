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


def test_lookup_artists_by_spotify_urls_exact_hit_returns_the_single_artist() -> None:
    transport = FakeTransport()
    mbid = "33333333-3333-3333-3333-333333333333"
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(
        {
            "relations": [
                {"target-type": "artist", "artist": {"id": mbid}},
            ]
        }
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: mbid}


def test_lookup_artists_by_spotify_urls_ambiguous_relation_returns_none() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue(
        {
            "relations": [
                {"target-type": "artist", "artist": {"id": "mbid-one"}},
                {"target-type": "artist", "artist": {"id": "mbid-two"}},
            ]
        }
    )
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_no_relation_returns_none() -> None:
    transport = FakeTransport()
    url = "https://open.spotify.com/artist/spotify123"
    transport.queue({"relations": []})
    source = _source(transport)
    result = source.lookup_artists_by_spotify_urls((url,))
    assert result == {url: None}


def test_lookup_artists_by_spotify_urls_propagates_rate_limited_error() -> None:
    transport = FakeTransport()
    transport.raise_next(RateLimitedError(retry_after_seconds=5))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.lookup_artists_by_spotify_urls(("https://open.spotify.com/artist/x",))


def test_lookup_artists_by_spotify_urls_batches_at_most_one_hundred_per_call() -> None:
    transport = FakeTransport()
    urls = tuple(f"https://open.spotify.com/artist/{index}" for index in range(150))
    transport.queue({"url-list": []})
    transport.queue({"url-list": []})
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


def test_search_artist_by_name_propagates_rate_limited_error() -> None:
    transport = FakeTransport()
    transport.raise_next(RateLimitedError(retry_after_seconds=5))
    source = _source(transport)
    with pytest.raises(RateLimitedError):
        source.search_artist_by_name("Synthetic Artist")
