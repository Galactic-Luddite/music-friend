from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from music_friend.errors import InvalidSourceResponseError
from music_friend.providers import Capability, ProviderCapabilities
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import SpotifyTransport

OBSERVED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _Tokens:
    def __init__(self, capabilities: ProviderCapabilities, transport: SpotifyTransport) -> None:
        self._capabilities = capabilities
        self._transport = transport
        self.access_count = 0

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _call_deadline(self) -> float:
        return self._transport._call_deadline()

    def _execute(
        self,
        operation: object,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> dict[str, object]:
        self.access_count += 1
        return self._transport.execute(
            operation,  # type: ignore[arg-type]
            query=query,
            deadline=deadline,
            **{"access_" + "token": "synthetic-access-value"},
        ).data


def _source(
    handler: httpx.MockTransport,
    capabilities: frozenset[Capability],
) -> tuple[SpotifySource, _Tokens]:
    transport = SpotifyTransport(handler)
    tokens = _Tokens(ProviderCapabilities(capabilities, capabilities), transport)
    return (
        SpotifySource(
            settings=SpotifySettings("synthetic-client"),
            tokens=tokens,
            clock=lambda: OBSERVED_AT,
        ),
        tokens,
    )


def test_top_artists_uses_artist_endpoint_and_returns_rank_ordered_canonical_artists() -> None:
    """Catches calling top tracks or losing Spotify's rank order."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "artist2", "name": "Second"},
                    {"id": "artist1", "name": "First"},
                ],
                "next": None,
            },
        )

    capability = Capability.TOP_ARTISTS
    source, _ = _source(httpx.MockTransport(respond), frozenset({capability}))

    page = source.top_artists("short_term", 2)

    assert tuple(artist.display_name for artist in page.items) == ("Second", "First")
    assert page.next_cursor is None
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/me/top/artists")
    ]
    assert tuple(requests[0].url.params.multi_items()) == (
        ("time_range", "short_term"),
        ("limit", "2"),
    )


@pytest.mark.parametrize(
    ("time_range", "limit"),
    (("forever", 1), ("short_term", 0), ("long_term", 51)),
)
def test_top_artists_rejects_invalid_ranges_and_limits_before_transport(
    time_range: str, limit: int
) -> None:
    """Catches invalid top-artist inputs acquiring a token or reaching HTTP."""
    capability = Capability.TOP_ARTISTS
    source, tokens = _source(
        httpx.MockTransport(lambda _request: pytest.fail("transport must not be reached")),
        frozenset({capability}),
    )

    with pytest.raises((ValueError, InvalidSourceResponseError)):
        source.top_artists(time_range, limit)

    assert tokens.access_count == 0
