from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import pytest

from music_friend.domain import SourceReference
from music_friend.errors import InvalidSourceResponseError
from music_friend.providers import Capability, ProviderCapabilities
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import (
    SpotifyOperation,
    SpotifyTransport,
    _validate_destination,
)

MALICIOUS_NEXT = "https://unapproved.example.invalid/escape?credential=canary"
OBSERVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class RecordingConnector(httpx.BaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.attempted_hosts: list[str] = []
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        assert host is not None
        self.attempted_hosts.append(host)
        self.requests.append(request)
        if host not in {"accounts.spotify.com", "api.spotify.com"}:
            raise AssertionError("unapproved connection reached connector")
        return self.responses.pop(0)


class StaticTokens:
    def __init__(self, transport: SpotifyTransport) -> None:
        self.snapshot = ProviderCapabilities(frozenset(Capability), frozenset(Capability))
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        return self.snapshot

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


def _source(
    response: httpx.Response,
) -> tuple[SpotifySource, RecordingConnector, SpotifyTransport]:
    connector = RecordingConnector([response])
    transport = SpotifyTransport(connector)
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=StaticTokens(transport),
        clock=lambda: OBSERVED_AT,
    )
    return source, connector, transport


def _artist() -> dict[str, object]:
    return {"id": "artist001", "name": "Synthetic Artist"}


def _track() -> dict[str, object]:
    return {
        "id": "track001",
        "name": "Synthetic Track",
        "artists": [{"id": "artist001", "name": "discarded"}],
    }


def _release() -> dict[str, object]:
    return {
        "id": "release001",
        "name": "Synthetic Release",
        "album_type": "album",
        "release_date": "2026-08-31",
        "release_date_precision": "day",
        "artists": [{"id": "artist001", "name": "discarded"}],
    }


def test_closed_operation_matrix_attempts_only_the_two_approved_hosts() -> None:
    connector = RecordingConnector([httpx.Response(200, json={"ok": True}) for _ in range(7)])
    transport = SpotifyTransport(connector)
    cases = (
        (SpotifyOperation.TOKEN, (), (("grant_type", "authorization_code"),), None),
        (SpotifyOperation.HEALTH, (), (), "access"),
        (SpotifyOperation.SEARCH_ARTISTS, (("q", "query"), ("limit", "1")), (), "access"),
        (SpotifyOperation.FOLLOWED_ARTISTS, (("limit", "50"),), (), "access"),
        (SpotifyOperation.SAVED_TRACKS, (("limit", "50"), ("offset", "0")), (), "access"),
        (SpotifyOperation.TOP_TRACKS, (("time_range", "short_term"), ("limit", "1")), (), "access"),
        (
            SpotifyOperation.ARTIST_RELEASES,
            (("artist_id", "artist001"), ("include_groups", "album,single"), ("limit", "10")),
            (),
            "access",
        ),
    )

    for operation, query, form, access in cases:
        transport.execute(
            operation,
            query=query,
            form=form,
            **{"access_" + "token": access},
        )

    assert connector.attempted_hosts == [
        "accounts.spotify.com",
        "api.spotify.com",
        "api.spotify.com",
        "api.spotify.com",
        "api.spotify.com",
        "api.spotify.com",
        "api.spotify.com",
    ]
    transport.close()


@pytest.mark.parametrize(
    "location",
    (
        "http://api.spotify.com/v1/me",
        "https://api.spotify.com.example.invalid/v1/me",
        "https://user@api.spotify.com/v1/me",
        "//unapproved.example.invalid/escape",
        "file:" + "/" * 3 + "private/escape",
        "javascript:synthetic",
    ),
)
def test_redirect_location_never_reaches_a_second_connection(location: str) -> None:
    connector = RecordingConnector([httpx.Response(302, headers={"Location": location})])
    transport = SpotifyTransport(connector)

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access"})

    assert connector.attempted_hosts == ["api.spotify.com"]
    assert len(connector.requests) == 1
    transport.close()


@pytest.mark.parametrize(
    ("origin", "path"),
    (
        ("http://api.spotify.com", "/v1/me"),
        ("https://api.spotify.com.example.invalid", "/v1/me"),
        ("https://accounts.spotify.com@unapproved.example.invalid", "/api/token"),
        ("https://api.spotify.com", "//unapproved.example.invalid/escape"),
        ("https://api.spotify.com", "/v1/me?next=https://unapproved.example.invalid"),
    ),
)
def test_malicious_destination_is_rejected_before_connector(
    origin: str,
    path: str,
) -> None:
    connector = RecordingConnector([httpx.Response(200, json={"unexpected": True})])

    with pytest.raises(InvalidSourceResponseError):
        _validate_destination(origin, path)

    assert connector.attempted_hosts == []


@pytest.mark.parametrize(
    "artist_id",
    (
        "",
        "../escape",
        "artist/escape",
        "artist?next=https://unapproved.example.invalid",
        "artist%2fescape",
        "a" * 65,
        "artist\N{FULLWIDTH SOLIDUS}escape",
    ),
)
def test_malicious_artist_id_is_rejected_before_connector(artist_id: str) -> None:
    connector = RecordingConnector([httpx.Response(200, json={"unexpected": True})])
    transport = SpotifyTransport(connector)

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.ARTIST_RELEASES,
            query=(("artist_id", artist_id), ("limit", "10")),
            **{"access_" + "token": "access"},
        )

    assert connector.attempted_hosts == []
    transport.close()


def test_response_next_urls_are_never_followed_or_returned() -> None:
    cases: tuple[tuple[httpx.Response, Callable[[SpotifySource], object]], ...] = (
        (
            httpx.Response(
                200,
                json={"artists": {"items": [_artist()], "next": MALICIOUS_NEXT}},
            ),
            lambda source: source.search_artists("Synthetic", 1),
        ),
        (
            httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [_artist()],
                        "next": MALICIOUS_NEXT,
                        "cursors": {"after": "artist001"},
                    }
                },
            ),
            lambda source: source.followed_artists(),
        ),
        (
            httpx.Response(
                200,
                json={"items": [{"track": _track()}], "next": MALICIOUS_NEXT},
            ),
            lambda source: source.saved_items(),
        ),
        (
            httpx.Response(200, json={"items": [_track()], "next": MALICIOUS_NEXT}),
            lambda source: source.top_items("short_term", 1),
        ),
        (
            httpx.Response(200, json={"items": [_release()], "next": MALICIOUS_NEXT}),
            lambda source: source.recent_releases(
                [
                    SourceReference(
                        source="spotify",
                        native_id="artist001",
                        canonical_url="https://open.spotify.com/artist/artist001",
                        observed_at=OBSERVED_AT,
                    )
                ],
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            ),
        ),
    )

    for response, operation in cases:
        source, connector, transport = _source(response)
        try:
            result = operation(source)
            assert MALICIOUS_NEXT not in repr(result)
            assert connector.attempted_hosts == ["api.spotify.com"]
        finally:
            transport.close()
