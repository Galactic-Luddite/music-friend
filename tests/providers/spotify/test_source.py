from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from music_friend.domain import SourceReference
from music_friend.errors import (
    AdditionalScopeRequiredError,
    CapabilityUnsupportedError,
    InvalidSourceResponseError,
    SourceUnavailableError,
)
from music_friend.providers import Capability, HealthStatus, ProviderCapabilities
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import SpotifyTransport

OBSERVED_AT = datetime(2026, 1, 2, tzinfo=timezone.utc)


class TokenStub:
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


class ScriptedHandler:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.payloads.pop(0))


def _source(
    payloads: list[dict[str, object]],
    capabilities: frozenset[Capability],
    *,
    granted: frozenset[Capability] | None = None,
) -> tuple[SpotifySource, TokenStub, ScriptedHandler]:
    handler = ScriptedHandler(payloads)
    transport = SpotifyTransport(httpx.MockTransport(handler))
    tokens = TokenStub(
        ProviderCapabilities(
            supported=capabilities,
            granted=capabilities if granted is None else granted,
        ),
        transport,
    )
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=tokens,
        clock=lambda: OBSERVED_AT,
    )
    return source, tokens, handler


def test_operation_request_shapes_and_results(load_spotify_fixture: Any) -> None:
    artists = load_spotify_fixture("artist-pages.json")
    items = load_spotify_fixture("item-pages.json")
    releases = load_spotify_fixture("release-pages.json")
    capabilities = frozenset(Capability)
    source, tokens, handler = _source(
        [
            {"id": "ignored-profile", "display_name": "ignored"},
            artists["search"],
            artists["followed_first"],
            items["saved"],
            items["top"],
            releases["albums"],
        ],
        capabilities,
    )

    health = source.health()
    search = source.search_artists("sample query", 10)
    followed = source.followed_artists()
    saved = source.saved_items()
    top = source.top_items("medium_term", 10)
    recent = source.recent_releases(
        (_reference("artist123"),), datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    assert health.status is HealthStatus.HEALTHY
    assert search.items[0].local_id == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b"
    )
    assert followed.next_cursor is not None
    assert saved.next_cursor is not None
    assert tuple((artist.local_id, artist.display_name) for artist in saved.artists) == (
        (
            "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
            "ignored",
        ),
    )
    assert top.items[0].local_id == (
        "mf:c5ade531ebb238bb710c3cfd1b3146f431368ade3fabd0fcaf0ef70ad1d54c1c"
    )
    assert tuple(artist.local_id for artist in top.artists) == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
    )
    assert recent.items[0].local_id == (
        "mf:350f46692129ac356d34473fcb123a45d52c3b0052a23ed86190ac5cc43ca565"
    )
    assert tokens.access_count == 6
    assert [(request.method, request.url.path) for request in handler.requests] == [
        ("GET", "/v1/me"),
        ("GET", "/v1/search"),
        ("GET", "/v1/me/following"),
        ("GET", "/v1/me/tracks"),
        ("GET", "/v1/me/top/tracks"),
        ("GET", "/v1/artists/artist123/albums"),
    ]
    assert tuple(parse_qsl(handler.requests[1].url.query.decode())) == (
        ("type", "artist"),
        ("q", "sample query"),
        ("limit", "10"),
    )
    assert tuple(parse_qsl(handler.requests[2].url.query.decode())) == (
        ("type", "artist"),
        ("limit", "50"),
    )
    assert tuple(parse_qsl(handler.requests[3].url.query.decode())) == (
        ("limit", "50"),
        ("offset", "0"),
    )
    assert tuple(parse_qsl(handler.requests[4].url.query.decode())) == (
        ("time_range", "medium_term"),
        ("limit", "10"),
    )
    assert tuple(parse_qsl(handler.requests[5].url.query.decode())) == (
        ("include_groups", "album,single"),
        ("limit", "10"),
        ("offset", "0"),
    )


def test_recent_release_aggregation_uses_token_transport_deadline() -> None:
    class MonotonicClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = MonotonicClock()
    unused_transport_clock = MonotonicClock()
    unused_transport_clock.value = 1_000_000.0
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        clock.value += 5.0
        return httpx.Response(200, json={"items": [], "next": None})

    transport = SpotifyTransport(httpx.MockTransport(respond), clock=clock)
    tokens = TokenStub(
        ProviderCapabilities(frozenset(Capability), frozenset(Capability)), transport
    )
    unused_candidate_transport = SpotifyTransport(
        httpx.MockTransport(lambda _request: pytest.fail("unused transport received a request")),
        clock=unused_transport_clock,
    )
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=tokens,
        clock=lambda: OBSERVED_AT,
    )
    references = tuple(_reference(f"artist{index}") for index in range(10))

    with pytest.raises(SourceUnavailableError):
        source.recent_releases(
            references,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert len(requests) == 9
    assert unused_candidate_transport.trace == ()


def test_followed_cursor_is_opaque_operation_bound_and_drives_exact_continuation(
    load_spotify_fixture: Any,
) -> None:
    artists = load_spotify_fixture("artist-pages.json")
    source, _, handler = _source(
        [artists["followed_first"], artists["followed_last"]],
        frozenset({Capability.FOLLOWED_ARTISTS}),
    )

    first = source.followed_artists()
    assert first.next_cursor is not None
    assert len(first.next_cursor) <= 512
    second = source.followed_artists(first.next_cursor)

    assert second.next_cursor is None
    assert handler.requests[1].url.params["after"] == "artist123"


def test_saved_cursor_uses_bounded_offset(load_spotify_fixture: Any) -> None:
    items = load_spotify_fixture("item-pages.json")
    first_payload = items["saved"]
    assert type(first_payload) is dict
    second_payload = dict(first_payload)
    second_payload.update({"offset": 1, "total": 1, "next": None})
    source, _, handler = _source(
        [first_payload, second_payload], frozenset({Capability.SAVED_ITEMS})
    )

    first = source.saved_items()
    assert first.next_cursor is not None
    source.saved_items(first.next_cursor)

    assert handler.requests[1].url.params["offset"] == "1"


@pytest.mark.parametrize(
    ("method", "args"),
    (
        ("search_artists", ("sample", 11)),
        ("search_artists", ("", 1)),
        ("top_items", ("forever", 1)),
        ("top_items", ("short_term", 0)),
        ("followed_artists", ("not-base64",)),
        ("saved_items", ("not-base64",)),
    ),
)
def test_invalid_inputs_reject_before_token_or_transport(
    method: str, args: tuple[object, ...]
) -> None:
    source, tokens, handler = _source([], frozenset(Capability))

    with pytest.raises((ValueError, InvalidSourceResponseError)):
        getattr(source, method)(*args)

    assert tokens.access_count == 0
    assert handler.requests == []


def test_cursor_rejects_wrong_operation_extra_keys_and_oversize_before_io(
    load_spotify_fixture: Any,
) -> None:
    artists = load_spotify_fixture("artist-pages.json")
    source, tokens, handler = _source(
        [artists["followed_first"]], frozenset({Capability.FOLLOWED_ARTISTS})
    )
    cursor = source.followed_artists().next_cursor
    assert cursor is not None
    calls = len(handler.requests)
    token_calls = tokens.access_count

    for invalid in (cursor + "x", "A" * 513):
        with pytest.raises(InvalidSourceResponseError):
            source.followed_artists(invalid)

    with pytest.raises(InvalidSourceResponseError):
        source.saved_items(cursor)
    assert len(handler.requests) == calls
    assert tokens.access_count == token_calls


@pytest.mark.parametrize("supported", (False, True))
def test_capability_gate_precedes_token_and_transport(supported: bool) -> None:
    capability = Capability.HEALTH
    source, tokens, handler = _source(
        [],
        frozenset({capability}) if supported else frozenset(),
        granted=frozenset(),
    )

    expected = AdditionalScopeRequiredError if supported else CapabilityUnsupportedError
    with pytest.raises(expected):
        source.health()
    assert tokens.access_count == 0
    assert handler.requests == []


def test_recent_releases_deduplicates_filters_and_limits_artist_inputs() -> None:
    payload = {
        "items": [
            {
                "id": "album1",
                "name": "one",
                "album_type": "album",
                "release_date": "2026-01-02",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            },
            {
                "id": "oldalbum",
                "name": "old",
                "album_type": "album",
                "release_date": "2025-01-01",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            },
        ],
        "next": None,
    }
    duplicate = {"items": [payload["items"][0]], "next": None}
    source, _, handler = _source([payload, duplicate], frozenset({Capability.RECENT_RELEASES}))

    page = source.recent_releases((_reference("artist1"), _reference("artist2")), OBSERVED_AT)

    assert tuple(item.local_id for item in page.items) == (
        "mf:2f726e39e1f2f3e56a326caf3d6b7c265fbc52a5c8680e12befe546e9cc0cda6",
    )
    assert len(handler.requests) == 2


def test_recent_releases_returns_a_bounded_artist_page_with_an_opaque_continuation() -> None:
    """Catches a release page that cannot be resumed safely by local discovery."""
    payload = {
        "items": [
            {
                "id": "album1",
                "name": "one",
                "album_type": "album",
                "release_date": "2026-01-02",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            }
        ],
        "next": "https://example.invalid/ignored",
    }
    final_payload = dict(payload)
    final_payload["items"] = []
    final_payload["next"] = None
    source, _, handler = _source([payload, final_payload], frozenset({Capability.RECENT_RELEASES}))

    first = source.recent_releases((_reference("artist1"),), OBSERVED_AT)
    assert first.next_cursor is not None
    second = source.recent_releases((_reference("artist1"),), OBSERVED_AT, first.next_cursor)

    assert tuple(item.local_id for item in first.items) == (
        "mf:2f726e39e1f2f3e56a326caf3d6b7c265fbc52a5c8680e12befe546e9cc0cda6",
    )
    assert second.items == ()
    assert second.next_cursor is None
    assert [request.url.params["offset"] for request in handler.requests] == ["0", "1"]
    assert all(request.url.params["limit"] == "10" for request in handler.requests)


def test_recent_releases_keeps_a_continuation_even_when_the_page_is_old() -> None:
    payload = {
        "items": [
            {
                "id": "oldalbum",
                "name": "old",
                "album_type": "album",
                "release_date": "2020-01-01",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            }
        ],
        "next": "https://example.invalid/ignored",
    }
    source, _, handler = _source([payload], frozenset({Capability.RECENT_RELEASES}))

    page = source.recent_releases((_reference("artist1"),), OBSERVED_AT)

    assert page.items == ()
    assert page.next_cursor is not None
    assert len(handler.requests) == 1


def test_recent_releases_keeps_partial_date_that_could_overlap_since() -> None:
    payload = {
        "items": [
            {
                "id": "album1",
                "name": "one",
                "album_type": "album",
                "release_date": "2026",
                "release_date_precision": "year",
                "artists": [{"id": "artist1"}],
            }
        ],
        "next": None,
    }
    source, _, _ = _source([payload], frozenset({Capability.RECENT_RELEASES}))

    page = source.recent_releases(
        (_reference("artist1"),), datetime(2026, 9, 1, tzinfo=timezone.utc)
    )

    assert tuple(item.local_id for item in page.items) == (
        "mf:2f726e39e1f2f3e56a326caf3d6b7c265fbc52a5c8680e12befe546e9cc0cda6",
    )


def test_later_malformed_record_rejects_the_entire_page() -> None:
    payload = {
        "artists": {
            "items": [
                {"id": "artist1", "name": "valid"},
                {"id": "artist2", "name": None},
            ],
            "next": None,
        }
    }
    source, _, _ = _source([payload], frozenset({Capability.SEARCH_ARTISTS}))

    with pytest.raises(InvalidSourceResponseError):
        source.search_artists("sample", 10)


@pytest.mark.parametrize("count", (0, 11))
def test_recent_releases_rejects_artist_count_before_io(count: int) -> None:
    source, tokens, handler = _source([], frozenset({Capability.RECENT_RELEASES}))
    refs = tuple(_reference(f"artist{index}") for index in range(count))

    with pytest.raises(ValueError):
        source.recent_releases(refs, OBSERVED_AT)

    assert tokens.access_count == 0
    assert handler.requests == []


def test_source_constructor_and_clock_reject_noncanonical_dependencies() -> None:
    """Catches adapter construction that would defer invalid dependencies until a user request."""
    transport = SpotifyTransport(httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    tokens = TokenStub(
        ProviderCapabilities(frozenset(Capability), frozenset(Capability)), transport
    )
    with pytest.raises(ValueError, match="settings"):
        SpotifySource(settings=object(), tokens=tokens, clock=lambda: OBSERVED_AT)  # type: ignore[arg-type]

    class BadCapabilities(TokenStub):
        def capabilities(self) -> object:
            return object()

    with pytest.raises(ValueError, match="capabilities"):
        SpotifySource(
            settings=SpotifySettings("synthetic-client"),
            tokens=BadCapabilities(tokens._capabilities, transport),  # type: ignore[arg-type]
            clock=lambda: OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="clock"):
        SpotifySource(settings=SpotifySettings("synthetic-client"), tokens=tokens, clock=None)  # type: ignore[arg-type]

    clock_transport = SpotifyTransport(
        httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"artists": {"items": [], "next": None}})
        )
    )
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=TokenStub(tokens._capabilities, clock_transport),
        clock=lambda: datetime(2026, 1, 1),
    )
    with pytest.raises(ValueError, match="clock result"):
        source.search_artists("One", 1)


def test_top_artists_normalizes_results_and_rejects_invalid_inputs() -> None:
    """Catches top-artist reads using the wrong operation, bounds, or normalized result shape."""
    source, tokens, handler = _source(
        [{"items": [{"id": "artist1", "name": "One"}], "next": None}],
        frozenset({Capability.TOP_ARTISTS}),
    )
    page = source.top_artists("long_term", 1)
    assert tuple(artist.display_name for artist in page.items) == ("One",)
    assert handler.requests[0].url.path == "/v1/me/top/artists"
    assert dict(handler.requests[0].url.params) == {"time_range": "long_term", "limit": "1"}

    calls = tokens.access_count
    for args in (("forever", 1), ("short_term", 0), ("short_term", True)):
        with pytest.raises(ValueError):
            source.top_artists(*args)  # type: ignore[arg-type]
    assert tokens.access_count == calls


@pytest.mark.parametrize(
    ("method", "payload"),
    (
        ("search_artists", {"artists": {"items": [], "next": 3}}),
        (
            "followed_artists",
            {"artists": {"items": [], "next": "present", "cursors": {"after": None}}},
        ),
        ("followed_artists", {"artists": {"items": [], "next": None, "cursors": []}}),
        ("saved_items", {"items": [], "next": "present"}),
        ("top_items", {"items": [], "next": 3}),
        ("top_artists", {"items": [], "next": 3}),
    ),
)
def test_source_rejects_nonadvancing_or_malformed_page_metadata(
    method: str, payload: dict[str, object]
) -> None:
    """Catches malformed pagination metadata being treated as a complete or resumable page."""
    capability = {
        "search_artists": Capability.SEARCH_ARTISTS,
        "followed_artists": Capability.FOLLOWED_ARTISTS,
        "saved_items": Capability.SAVED_ITEMS,
        "top_items": Capability.TOP_ITEMS,
        "top_artists": Capability.TOP_ARTISTS,
    }[method]
    source, _, _ = _source([payload], frozenset({capability}))
    args: tuple[object, ...] = {
        "search_artists": ("One", 1),
        "followed_artists": (),
        "saved_items": (),
        "top_items": ("short_term", 1),
        "top_artists": ("short_term", 1),
    }[method]
    with pytest.raises(InvalidSourceResponseError):
        getattr(source, method)(*args)


def test_recent_release_cursor_is_artist_bound_and_multi_artist_pages_cannot_continue() -> None:
    """Catches a continuation being replayed for another artist or silently losing later artists."""
    payload = {
        "items": [
            {
                "id": "album1",
                "name": "One",
                "album_type": "album",
                "release_date": "2026-01-01",
                "release_date_precision": "day",
                "artists": [{"id": "artist1"}],
            }
        ],
        "next": "present",
    }
    source, _, _ = _source([payload], frozenset({Capability.RECENT_RELEASES}))
    page = source.recent_releases((_reference("artist1"),), OBSERVED_AT)
    assert page.next_cursor is not None

    replay, replay_tokens, replay_handler = _source([], frozenset({Capability.RECENT_RELEASES}))
    with pytest.raises(InvalidSourceResponseError):
        replay.recent_releases((_reference("artist2"),), OBSERVED_AT, page.next_cursor)
    assert replay_tokens.access_count == 0
    assert replay_handler.requests == []

    multiple, _, _ = _source([payload], frozenset({Capability.RECENT_RELEASES}))
    with pytest.raises(InvalidSourceResponseError):
        multiple.recent_releases((_reference("artist1"), _reference("artist2")), OBSERVED_AT)


def test_source_rejects_noncanonical_artist_references_and_cursor_encoding() -> None:
    """Catches duplicate, cross-provider, invalid-id, and noncanonical cursor inputs before egress."""
    source, tokens, handler = _source([], frozenset({Capability.RECENT_RELEASES}))
    other = SourceReference("other", "artist1", None, OBSERVED_AT)
    invalid = SourceReference("spotify", "bad/id", None, OBSERVED_AT)
    for references in (
        (_reference("artist1"), _reference("artist1")),
        (other,),
        (invalid,),
        "artist1",
    ):
        with pytest.raises(ValueError):
            source.recent_releases(references, OBSERVED_AT)  # type: ignore[arg-type]
    assert tokens.access_count == 0
    assert handler.requests == []

    raw = json.dumps({"v": 1, "op": "saved", "offset": 0}, indent=1).encode("ascii")
    noncanonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    saved, saved_tokens, _ = _source([], frozenset({Capability.SAVED_ITEMS}))
    with pytest.raises(InvalidSourceResponseError):
        saved.saved_items(noncanonical)
    assert saved_tokens.access_count == 0


def _reference(native_id: str) -> SourceReference:
    return SourceReference(
        source="spotify",
        native_id=native_id,
        canonical_url=f"https://open.spotify.com/artist/{native_id}",
        observed_at=OBSERVED_AT,
    )
