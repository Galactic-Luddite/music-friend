from datetime import datetime, timezone

import pytest

from music_friend.errors import InvalidSourceResponseError
from music_friend.providers import Capability, ProviderCapabilities, RecentPlay, RecentPlaySource
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import SpotifyOperation


class Tokens:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[SpotifyOperation, tuple[tuple[str, str], ...]]] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(frozenset(Capability), frozenset(Capability))

    def _call_deadline(self) -> float:
        return 10.0

    def _execute(
        self, operation: SpotifyOperation, *, query: tuple[tuple[str, str], ...], deadline: float
    ) -> dict[str, object]:
        self.calls.append((operation, query))
        return self.response


def source(response: dict[str, object]) -> tuple[SpotifySource, Tokens]:
    tokens = Tokens(response)
    settings = SpotifySettings(client_id="synthetic", redirect_uri="http://127.0.0.1:8888/callback")
    return SpotifySource(
        settings=settings, tokens=tokens, clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    ), tokens


def item(played_at: str = "2030-01-01T00:00:00.123456789Z") -> dict[str, object]:
    return {
        "played_at": played_at,
        "context": {"type": "playlist", "uri": "spotify:playlist:abc"},
        "track": {
            "uri": "spotify:track:abc",
            "name": "Track A",
            "artists": [{"name": "Artist A"}],
            "album": {"name": "Album A"},
        },
    }


def test_recent_plays_normalizes_precision_and_reconstructs_cursor() -> None:
    spotify, tokens = source(
        {
            "items": [item()],
            "next": (
                "https://api.spotify.com/v1/me/player/recently-played?before=1893455999000&limit=50"
            ),
            "cursors": {"after": "1893456000123", "before": "1893455999000"},
            "total": 1,
        }
    )
    assert isinstance(spotify, RecentPlaySource)
    page = spotify.recent_plays(after_ms=100)
    assert page.items == (
        RecentPlay(
            "spotify:track:abc",
            "Track A",
            "Artist A",
            "Album A",
            "2030-01-01T00:00:00.123456789Z",
            "spotify:playlist:abc",
        ),
    )
    assert page.next_cursor is not None
    tokens.response = {"items": [], "next": None, "cursors": {}}
    spotify.recent_plays(cursor=page.next_cursor)
    assert tokens.calls[1] == (
        SpotifyOperation.RECENTLY_PLAYED,
        (("limit", "50"), ("before", "1893455999000")),
    )


@pytest.mark.parametrize(
    "next_value",
    (
        "https://evil.invalid/v1/me/player/recently-played?before=1&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?after=2&before=1&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?before=1&limit=49",
        "https://api.spotify.com/v1/me/player/recently-played?offset=1&limit=50",
        "https://api.spotify.com/v1/me/player/recently-played?before=2&limit=50",
    ),
)
def test_recent_plays_rejects_untrusted_continuation_destinations(next_value: str) -> None:
    spotify, _ = source(
        {
            "items": [item()],
            "next": next_value,
            "cursors": {"after": "2", "before": "1"},
        }
    )

    with pytest.raises(InvalidSourceResponseError):
        spotify.recent_plays()


def test_recent_plays_preserves_a_valid_after_continuation_direction() -> None:
    spotify, tokens = source(
        {
            "items": [item()],
            "next": (
                "https://api.spotify.com/v1/me/player/recently-played?limit=50&after=1893456000123"
            ),
            "cursors": {"after": "1893456000123", "before": "1893455999000"},
        }
    )

    page = spotify.recent_plays(after_ms=100)
    assert page.next_cursor is not None
    tokens.response = {"items": [], "next": None, "cursors": {}}
    spotify.recent_plays(cursor=page.next_cursor)

    assert tokens.calls[1] == (
        SpotifyOperation.RECENTLY_PLAYED,
        (("limit", "50"), ("after", "1893456000123")),
    )


def test_recent_plays_rejects_a_nonadvancing_before_continuation() -> None:
    spotify, tokens = source(
        {
            "items": [item()],
            "next": ("https://api.spotify.com/v1/me/player/recently-played?before=100&limit=50"),
            "cursors": {"before": "100"},
        }
    )
    first = spotify.recent_plays()
    assert first.next_cursor is not None
    tokens.response = {
        "items": [item("2029-12-31T23:59:59Z")],
        "next": "https://api.spotify.com/v1/me/player/recently-played?before=100&limit=50",
        "cursors": {"before": "100"},
    }

    with pytest.raises(InvalidSourceResponseError):
        spotify.recent_plays(cursor=first.next_cursor)


@pytest.mark.parametrize(
    "response",
    [
        {"items": [item("not-a-date")], "next": None, "cursors": {}},
        {"items": [item("2030-02-30T00:00:00Z")], "next": None, "cursors": {}},
        {"items": [item("2030-01-01T00:00:00+99:00")], "next": None, "cursors": {}},
        {"items": [], "next": "https://example.invalid", "cursors": {"after": "2"}},
    ],
)
def test_recent_plays_rejects_invalid_pages(response: dict[str, object]) -> None:
    spotify, _ = source(response)
    with pytest.raises(InvalidSourceResponseError):
        spotify.recent_plays()
