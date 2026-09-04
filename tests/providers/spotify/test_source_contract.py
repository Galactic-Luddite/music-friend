from __future__ import annotations

import re

import httpx
import pytest

from music_friend.domain import SourceReference
from music_friend.providers import Capability, MusicSource, ProviderCapabilities
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport
from tests.contracts.source_contract import (
    DEFAULT_PROVIDER_TEXT,
    FIXED_OBSERVED_AT,
    MusicSourceContract,
    TransportCall,
)


class _Tokens:
    def __init__(self, capabilities: ProviderCapabilities, transport: SpotifyTransport) -> None:
        self._capabilities = capabilities
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _call_deadline(self) -> float:
        return self._transport._call_deadline()

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> dict[str, object]:
        return self._transport.execute(
            operation,
            query=query,
            deadline=deadline,
            **{"access_" + "token": "synthetic-access-value"},
        ).data


class SpotifySourceFactory:
    def __init__(self) -> None:
        self._calls: list[TransportCall] = []
        self._scenario = "happy_path"
        self._text = DEFAULT_PROVIDER_TEXT

    def create(
        self,
        *,
        capabilities: frozenset[Capability],
        scenario: str = "happy_path",
        raw_text: str | None = None,
    ) -> MusicSource:
        self._calls.clear()
        self._scenario = scenario
        self._text = DEFAULT_PROVIDER_TEXT if raw_text is None else raw_text
        granted = frozenset() if scenario == "missing_scope" else capabilities
        transport = SpotifyTransport(httpx.MockTransport(self._handle))
        source = SpotifySource(
            settings=SpotifySettings("synthetic-client"),
            tokens=_Tokens(ProviderCapabilities(capabilities, granted), transport),
            clock=lambda: FIXED_OBSERVED_AT,
        )
        return source

    def transport_calls(self) -> tuple[TransportCall, ...]:
        return tuple(self._calls)

    def example_artist_reference(self) -> SourceReference:
        return SourceReference(
            source="spotify",
            native_id="artist1",
            canonical_url="https://open.spotify.com/artist/artist1",
            observed_at=FIXED_OBSERVED_AT,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        call = _semantic_call(request)
        if not self._calls or self._calls[-1] != call:
            self._calls.append(call)
        if self._scenario == "timeout":
            raise httpx.ConnectError("synthetic timeout", request=request)
        if self._scenario == "authentication_required":
            return httpx.Response(401)
        if self._scenario == "quota_exhausted":
            return httpx.Response(429, json={"error": {"reason": "QUOTA_EXCEEDED"}})
        if self._scenario == "rate_limited":
            return httpx.Response(429, headers={"Retry-After": "999999"}, json={})
        return httpx.Response(200, json=self._payload(request))

    def _payload(self, request: httpx.Request) -> dict[str, object]:
        malformed = self._scenario == "malformed_record"
        text = self._text
        artist = {} if malformed else {"id": "artist1", "name": text}
        track = (
            {}
            if malformed
            else {
                "id": "track1",
                "name": text,
                "artists": [{"id": "artist1", "name": text}],
            }
        )
        release = (
            {}
            if malformed
            else {
                "id": "album1",
                "name": text,
                "album_type": "album",
                "release_date": "2026-01-01",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            }
        )
        path = request.url.path
        if path == "/v1/me":
            return {"id": "discarded"}
        if path == "/v1/search":
            return {"artists": {"items": [artist, artist], "next": None}}
        if path == "/v1/me/following":
            continued = request.url.params.get("after") is not None
            return {
                "artists": {
                    "items": [artist, artist],
                    "cursors": {"after": None if continued else "artist1"},
                    "next": None if continued else "discarded-next-url",
                }
            }
        if path == "/v1/me/tracks":
            return {"items": [{"track": track}, {"track": track}], "next": None}
        if path == "/v1/me/top/tracks":
            return {"items": [track, track], "next": None}
        if path == "/v1/me/top/artists":
            return {"items": [artist, artist], "next": None}
        return {"items": [release, release], "next": None}


def _semantic_call(request: httpx.Request) -> TransportCall:
    def invalid() -> None:
        raise AssertionError("invalid Spotify contract request")

    if (
        request.method != "GET"
        or request.url.scheme != "https"
        or request.url.host != "api.spotify.com"
        or request.url.port is not None
    ):
        invalid()
    pairs = list(request.url.params.multi_items())
    if len({key for key, _value in pairs}) != len(pairs):
        invalid()
    query = dict(pairs)
    path = request.url.path
    if path == "/v1/me":
        if query:
            invalid()
        return TransportCall("health")
    if path == "/v1/search":
        if set(query) != {"type", "q", "limit"} or query.get("type") != "artist":
            invalid()
        search_query = query.get("q", "")
        limit = _canonical_integer(query.get("limit"), lower=1, upper=10)
        if not search_query or search_query.strip() != search_query or len(search_query) > 256:
            invalid()
        return TransportCall("search_artists", (("limit", limit),))
    if path == "/v1/me/following":
        if set(query) not in ({"type", "limit"}, {"type", "limit", "after"}):
            invalid()
        if query.get("type") != "artist" or query.get("limit") != "50":
            invalid()
        after = query.get("after")
        if after is None:
            return TransportCall("followed_artists", (("cursor_state", "start"),))
        if re.fullmatch(r"[A-Za-z0-9]{1,64}", after) is None:
            invalid()
        return TransportCall("followed_artists", (("cursor_state", "continued"),))
    if path == "/v1/me/tracks":
        if set(query) != {"limit", "offset"} or query.get("limit") != "50":
            invalid()
        offset = _canonical_integer(query.get("offset"), lower=0, upper=1_000_000)
        state = "start" if offset == 0 else "continued"
        return TransportCall("saved_items", (("cursor_state", state),))
    if path == "/v1/me/top/tracks":
        if set(query) != {"time_range", "limit"}:
            invalid()
        time_range = query.get("time_range", "")
        if time_range not in {"short_term", "medium_term", "long_term"}:
            invalid()
        limit = _canonical_integer(query.get("limit"), lower=1, upper=50)
        return TransportCall("top_items", (("limit", limit), ("range", time_range)))
    if path == "/v1/me/top/artists":
        if set(query) != {"time_range", "limit"}:
            invalid()
        time_range = query.get("time_range", "")
        if time_range not in {"short_term", "medium_term", "long_term"}:
            invalid()
        limit = _canonical_integer(query.get("limit"), lower=1, upper=50)
        return TransportCall("top_artists", (("limit", limit), ("range", time_range)))
    match = re.fullmatch(r"/v1/artists/([A-Za-z0-9]{1,64})/albums", path)
    if match is None:
        invalid()
    if (
        set(query) != {"include_groups", "limit", "offset"}
        or query.get("include_groups") != "album,single"
        or query.get("limit") != "10"
    ):
        invalid()
    _canonical_integer(query.get("offset"), lower=0, upper=1_000_000)
    return TransportCall("recent_releases", (("artist_count", 1),))


def _canonical_integer(value: str | None, *, lower: int, upper: int) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise AssertionError("invalid Spotify contract request")
    parsed = int(value)
    if str(parsed) != value or not lower <= parsed <= upper:
        raise AssertionError("invalid Spotify contract request")
    return parsed


@pytest.mark.parametrize(
    "request_value",
    (
        httpx.Request("POST", "https://api.spotify.com/v1/me"),
        httpx.Request("GET", "https://api.spotify.com/v1/unknown"),
        httpx.Request("GET", "https://api.spotify.com/v1/search?type=artist&limit=10"),
        httpx.Request(
            "GET",
            "https://api.spotify.com/v1/search?type=artist&q=sample&limit=10&extra=value",
        ),
        httpx.Request(
            "GET", "https://api.spotify.com/v1/me/top/tracks?time_range=forever&limit=10"
        ),
        httpx.Request("GET", "https://api.spotify.com/v1/me/tracks?limit=50&offset=-1"),
        httpx.Request("GET", "https://api.spotify.com/v1/me/following?type=artist&limit=49"),
        httpx.Request(
            "GET",
            "https://api.spotify.com/v1/artists/bad-id/albums?include_groups=album,single&limit=10",
        ),
        httpx.Request(
            "GET",
            "https://api.spotify.com/v1/artists/artist1/albums?include_groups=album,single&limit=11",
        ),
    ),
    ids=(
        "write-method",
        "unknown-path",
        "search-missing-query",
        "search-extra-key",
        "top-invalid-range",
        "saved-negative-offset",
        "followed-wrong-limit",
        "release-invalid-id",
        "release-wrong-limit",
    ),
)
def test_semantic_trace_rejects_every_malformed_request(request_value: httpx.Request) -> None:
    with pytest.raises(AssertionError, match="invalid Spotify contract request"):
        _semantic_call(request_value)


def test_semantic_trace_derives_safe_values_from_actual_request() -> None:
    search = httpx.Request("GET", "https://api.spotify.com/v1/search?type=artist&q=sample&limit=7")
    top = httpx.Request(
        "GET", "https://api.spotify.com/v1/me/top/tracks?time_range=long_term&limit=23"
    )

    assert _semantic_call(search) == TransportCall("search_artists", (("limit", 7),))
    assert _semantic_call(top) == TransportCall(
        "top_items", (("limit", 23), ("range", "long_term"))
    )


class TestSpotifySourceContract(MusicSourceContract):
    source_factory = SpotifySourceFactory()
