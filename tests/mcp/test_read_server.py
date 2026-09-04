"""Public MCP read-tool contract tests."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.errors import (
    AdditionalScopeRequiredError,
    AmbiguousIdentityError,
    AuthenticationRequiredError,
    CapabilityUnsupportedError,
    CatalogUnavailableError,
    InvalidSourceResponseError,
    MusicFriendError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)
from music_friend.mcp.read_server import create_read_server
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport

FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _reference(native_id: str = "artist-1") -> SourceReference:
    return SourceReference("synthetic", native_id, "https://example.test/artist", FIXED_TIME)


def _artist() -> Artist:
    return Artist(
        "artist-1",
        "Artist One",
        (_reference(),),
        IdentityConfidence.EXTERNAL_ID,
        FIXED_TIME,
    )


def _serialize_expected_artist() -> dict[str, object]:
    return {
        "local_id": "artist-1",
        "display_name": "Artist One",
        "source_refs": [
            {
                "source": "synthetic",
                "native_id": "artist-1",
                "canonical_url": "https://example.test/artist",
                "observed_at": "2026-01-02T03:04:05+00:00",
            }
        ],
        "identity_confidence": "external_id",
        "observed_at": "2026-01-02T03:04:05+00:00",
    }


def _item() -> CatalogItem:
    return CatalogItem(
        "track",
        "item-1",
        "Item One",
        ("artist-1",),
        (_reference("item-1"),),
        FIXED_TIME,
    )


def _release() -> Release:
    return Release(
        "release-1",
        "Release One",
        "album",
        date(2026, 1, 1),
        ReleaseDatePrecision.DAY,
        ("artist-1",),
        (_reference("release-1"),),
        FIXED_TIME,
    )


class _RecordingSource:
    def __init__(self, error: MusicFriendError | Exception | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error = error
        self.capabilities_value = ProviderCapabilities(
            supported=frozenset(Capability),
            granted=frozenset({Capability.HEALTH, Capability.SEARCH_ARTISTS}),
        )

    def _result(self, value: object) -> object:
        if self.error is not None:
            raise self.error
        return value

    def capabilities(self) -> ProviderCapabilities:
        self.calls.append(("capabilities",))
        return self._result(self.capabilities_value)  # type: ignore[return-value]

    def health(self) -> ProviderHealth:
        self.calls.append(("health",))
        return self._result(ProviderHealth(HealthStatus.DEGRADED, self.capabilities_value))  # type: ignore[return-value]

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        self.calls.append(("search_artists", query, limit))
        return self._result(Page((_artist(),), "artists-next"))  # type: ignore[return-value]

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        self.calls.append(("followed_artists", cursor))
        return self._result(Page((_artist(),), "followed-next"))  # type: ignore[return-value]

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        self.calls.append(("saved_items", cursor))
        return self._result(CatalogItemBatch((_item(),), (_artist(),), "saved-next"))  # type: ignore[return-value]

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        self.calls.append(("top_items", time_range, limit))
        return self._result(CatalogItemBatch((_item(),), (_artist(),), "top-next"))  # type: ignore[return-value]

    def recent_releases(
        self, artist_refs: tuple[SourceReference, ...], since: datetime
    ) -> Page[Release]:
        self.calls.append(("recent_releases", artist_refs, since))
        return self._result(Page((_release(),), None))  # type: ignore[return-value]


def _call(server: object, name: str, arguments: dict[str, object]) -> object:
    async def invoke() -> object:
        async with Client(server) as client:  # type: ignore[arg-type]
            return await client.call_tool(name, arguments)

    result = asyncio.run(invoke())
    return result.structured_content  # type: ignore[union-attr]


class _CapabilitiesOnlySource:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported=frozenset(Capability),
            granted=frozenset(Capability),
        )


class _RealSourceTokenProvider:
    def __init__(self, transport: SpotifyTransport) -> None:
        self._transport = transport
        self.deadline_calls = 0
        self.execute_calls: list[SpotifyOperation] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported=frozenset(Capability),
            granted=frozenset({Capability.SEARCH_ARTISTS}),
        )

    def _call_deadline(self) -> float:
        self.deadline_calls += 1
        return self._transport._call_deadline()

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> dict[str, object]:
        self.execute_calls.append(operation)
        return self._transport.execute(
            operation,
            query=query,
            deadline=deadline,
            **{"access_" + "token": "synthetic-access-value"},
        ).data


def test_read_server_registers_the_fixed_provider_neutral_tool_catalog() -> None:
    server = create_read_server(_CapabilitiesOnlySource())

    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == [
        "music_capabilities",
        "music_health",
        "search_artists",
        "followed_artists",
        "saved_items",
        "top_items",
        "recent_releases",
    ]
    assert all(
        tool.annotations is not None and tool.annotations.read_only_hint is True for tool in tools
    )
    assert [tool.description for tool in tools] == [
        "Inspect available music source capabilities.",
        "Inspect music source health and capabilities.",
        "Search normalized artists from the connected music source.",
        "List normalized artists followed by the user.",
        "List normalized music items saved by the user.",
        "List normalized top music items for a time range.",
        "List normalized recent releases for artist references.",
    ]
    assert all(
        tool.annotations is not None
        and tool.annotations.model_dump(by_alias=True)
        == {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        }
        for tool in tools
    )


def test_read_server_publishes_fixed_argument_schemas() -> None:
    server = create_read_server(_CapabilitiesOnlySource())

    async def list_schemas() -> dict[str, dict[str, object]]:
        async with Client(server) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}

    schemas = asyncio.run(list_schemas())

    assert schemas == {
        "music_capabilities": {"additionalProperties": False, "properties": {}, "type": "object"},
        "music_health": {"additionalProperties": False, "properties": {}, "type": "object"},
        "search_artists": {
            "additionalProperties": False,
            "properties": {
                "query": {
                    "description": "Must not begin or end with whitespace.",
                    "maxLength": 256,
                    "minLength": 1,
                    "pattern": (
                        "^" + chr(92) + "S(?:[" + chr(92) + "s" + chr(92) + "S]*" + chr(92) + "S)?$"
                    ),
                    "type": "string",
                },
                "limit": {"maximum": 10, "minimum": 1, "type": "integer"},
            },
            "required": ["query", "limit"],
            "type": "object",
        },
        "followed_artists": {
            "additionalProperties": False,
            "properties": {"cursor": {"type": ["string", "null"]}},
            "type": "object",
        },
        "saved_items": {
            "additionalProperties": False,
            "properties": {"cursor": {"type": ["string", "null"]}},
            "type": "object",
        },
        "top_items": {
            "additionalProperties": False,
            "properties": {
                "time_range": {
                    "enum": ["short_term", "medium_term", "long_term"],
                    "type": "string",
                },
                "limit": {"maximum": 50, "minimum": 1, "type": "integer"},
            },
            "required": ["time_range", "limit"],
            "type": "object",
        },
        "recent_releases": {
            "additionalProperties": False,
            "properties": {
                "artist_refs": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "source": {"type": "string"},
                            "native_id": {"type": "string"},
                            "canonical_url": {"type": ["string", "null"]},
                            "observed_at": {"format": "date-time", "type": "string"},
                        },
                        "required": ["source", "native_id", "canonical_url", "observed_at"],
                        "type": "object",
                    },
                    "maxItems": 10,
                    "minItems": 1,
                    "type": "array",
                },
                "since": {"format": "date-time", "type": "string"},
            },
            "required": ["artist_refs", "since"],
            "type": "object",
        },
    }


def test_read_server_delegates_and_serializes_every_read_operation() -> None:
    source = _RecordingSource()
    server = create_read_server(source)

    assert _call(server, "music_capabilities", {}) == {
        "supported": sorted(capability.value for capability in Capability),
        "granted": ["health", "search_artists"],
        "effective": ["health", "search_artists"],
    }
    assert _call(server, "music_health", {}) == {
        "status": "degraded",
        "capabilities": {
            "supported": sorted(capability.value for capability in Capability),
            "granted": ["health", "search_artists"],
            "effective": ["health", "search_artists"],
        },
    }
    assert _call(server, "search_artists", {"query": "artist", "limit": 3}) == {
        "items": [
            {
                "local_id": "artist-1",
                "display_name": "Artist One",
                "source_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": "https://example.test/artist",
                        "observed_at": "2026-01-02T03:04:05+00:00",
                    }
                ],
                "identity_confidence": "external_id",
                "observed_at": "2026-01-02T03:04:05+00:00",
            }
        ],
        "next_cursor": "artists-next",
    }
    assert (
        _call(server, "followed_artists", {"cursor": "cursor-1"})["next_cursor"] == "followed-next"
    )
    assert _call(server, "saved_items", {"cursor": None}) == {
        "items": [
            {
                "kind": "track",
                "local_id": "item-1",
                "title": "Item One",
                "artist_refs": ["artist-1"],
                "source_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "item-1",
                        "canonical_url": "https://example.test/artist",
                        "observed_at": "2026-01-02T03:04:05+00:00",
                    }
                ],
                "observed_at": "2026-01-02T03:04:05+00:00",
            }
        ],
        "artists": [
            {
                "local_id": "artist-1",
                "display_name": "Artist One",
                "source_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": "https://example.test/artist",
                        "observed_at": "2026-01-02T03:04:05+00:00",
                    }
                ],
                "identity_confidence": "external_id",
                "observed_at": "2026-01-02T03:04:05+00:00",
            }
        ],
        "next_cursor": "saved-next",
    }
    top = _call(server, "top_items", {"time_range": "medium_term", "limit": 20})
    assert top["artists"] == [_serialize_expected_artist()]
    assert top["next_cursor"] == "top-next"
    assert _call(
        server,
        "recent_releases",
        {
            "artist_refs": [
                {
                    "source": "synthetic",
                    "native_id": "artist-1",
                    "canonical_url": "https://example.test/artist",
                    "observed_at": "2026-01-02T03:04:05Z",
                }
            ],
            "since": "2026-01-01T00:00:00+00:00",
        },
    )["items"][0] == {
        "local_id": "release-1",
        "title": "Release One",
        "release_type": "album",
        "release_date": "2026-01-01",
        "date_precision": "day",
        "artist_refs": ["artist-1"],
        "source_refs": [
            {
                "source": "synthetic",
                "native_id": "release-1",
                "canonical_url": "https://example.test/artist",
                "observed_at": "2026-01-02T03:04:05+00:00",
            }
        ],
        "observed_at": "2026-01-02T03:04:05+00:00",
    }
    assert source.calls == [
        ("capabilities",),
        ("health",),
        ("search_artists", "artist", 3),
        ("followed_artists", "cursor-1"),
        ("saved_items", None),
        ("top_items", "medium_term", 20),
        (
            "recent_releases",
            (_reference(),),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]


def test_read_server_uses_public_mcp_server_registration_and_sdk_dispatch() -> None:
    source = _RecordingSource()
    server = create_read_server(source)

    assert type(server) is MCPServer

    async def invoke() -> object:
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "music_capabilities",
                "music_health",
                "search_artists",
                "followed_artists",
                "saved_items",
                "top_items",
                "recent_releases",
            ]
            result = await client.call_tool("search_artists", {"query": "artist", "limit": 3})
            invalid = await client.call_tool("search_artists", {"query": 1, "limit": 3})
            assert invalid.structured_content == {
                "category": "invalid_arguments",
                "message": "Invalid tool arguments.",
            }
            token_argument = "".join(("access", "_", "token"))
            assert token_argument == "access_token"
            extra_arguments: dict[str, object] = {"query": "artist", "limit": 3}
            extra_arguments[token_argument] = "credential-canary"
            extra = await client.call_tool(
                "search_artists",
                extra_arguments,
            )
            assert extra.structured_content == {
                "category": "invalid_arguments",
                "message": "Invalid tool arguments.",
            }
            return result

    result = asyncio.run(invoke())

    assert result.structured_content["next_cursor"] == "artists-next"
    assert source.calls == [("search_artists", "artist", 3)]


def test_read_server_real_spotify_source_rejects_padded_queries_before_provider_work() -> None:
    """Removing adapter validation would let a real source turn caller mistakes into internals."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"artists": {"items": [], "next": None}})

    transport = SpotifyTransport(httpx.MockTransport(respond))
    tokens = _RealSourceTokenProvider(transport)
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=tokens,
        clock=lambda: FIXED_TIME,
    )

    async def invoke() -> tuple[object, object, object]:
        async with Client(create_read_server(source)) as client:
            padded = await client.call_tool("search_artists", {"query": " artist ", "limit": 1})
            blank = await client.call_tool("search_artists", {"query": "   ", "limit": 1})
            assert tokens.deadline_calls == 0
            assert tokens.execute_calls == []
            assert requests == []
            valid = await client.call_tool("search_artists", {"query": "Artist", "limit": 1})
            return padded.structured_content, blank.structured_content, valid.structured_content

    try:
        padded, blank, valid = asyncio.run(invoke())
    finally:
        transport.close()

    assert padded == {"category": "invalid_arguments", "message": "Invalid tool arguments."}
    assert blank == {"category": "invalid_arguments", "message": "Invalid tool arguments."}
    assert tokens.deadline_calls == 1
    assert tokens.execute_calls == [SpotifyOperation.SEARCH_ARTISTS]
    assert len(requests) == 1
    assert valid == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "error",
    [
        AdditionalScopeRequiredError(),
        AmbiguousIdentityError(),
        AuthenticationRequiredError(),
        CapabilityUnsupportedError(),
        CatalogUnavailableError(),
        InvalidSourceResponseError(),
        QuotaExhaustedError(),
        RateLimitedError(12),
        SourceUnavailableError(),
    ],
)
def test_read_server_returns_only_public_music_friend_errors(error: MusicFriendError) -> None:
    source = _RecordingSource(error)

    result = _call(create_read_server(source), "music_capabilities", {})

    assert result == error.to_public_dict()
    assert source.calls == [("capabilities",)]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("music_capabilities", {"credential": "credential-canary"}),
        ("search_artists", {"query": 1, "limit": 1}),
        ("search_artists", {"query": " artist", "limit": 1}),
        ("search_artists", {"query": "artist ", "limit": 1}),
        ("search_artists", {"query": "   ", "limit": 1}),
        ("search_artists", {"query": "artist", "limit": True}),
        ("search_artists", {"query": "artist", "limit": 11}),
        ("search_artists", {"query": "a" * 257, "limit": 1}),
        ("search_artists", {"limit": 1}),
        ("followed_artists", {"cursor": 1}),
        ("saved_items", {"cursor": 1}),
        ("top_items", {"time_range": "unbounded", "limit": 1}),
        ("top_items", {"time_range": 1, "limit": 1}),
        ("top_items", {"time_range": "long_term", "limit": 51}),
        ("recent_releases", {"artist_refs": [], "since": "2026-01-01T00:00:00+00:00"}),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": str(index),
                        "canonical_url": None,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                    for index in range(11)
                ],
                "since": "2026-01-01T00:00:00+00:00",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [{"source": "synthetic"}],
                "since": "2026-01-01T00:00:00+00:00",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": 1,
                        "native_id": "artist-1",
                        "canonical_url": None,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "since": "2026-01-01T00:00:00+00:00",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": 1,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "since": "2026-01-01T00:00:00+00:00",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": None,
                        "observed_at": "bad",
                    }
                ],
                "since": "2026-01-01T00:00:00+00:00",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": None,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "since": 1,
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": None,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "since": "not-a-date",
            },
        ),
        (
            "recent_releases",
            {
                "artist_refs": [
                    {
                        "source": "synthetic",
                        "native_id": "artist-1",
                        "canonical_url": None,
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "since": "2026-01-01T00:00:00",
            },
        ),
    ],
)
def test_read_server_rejects_invalid_arguments_without_delegating(
    name: str, arguments: dict[str, object]
) -> None:
    source = _RecordingSource()

    result = _call(create_read_server(source), name, arguments)

    assert result == {"category": "invalid_arguments", "message": "Invalid tool arguments."}
    assert source.calls == []


def test_read_server_hides_unexpected_exception_text_and_canaries() -> None:
    source = _RecordingSource(RuntimeError("credential-canary /" + "private" + "/path-canary"))

    result = _call(create_read_server(source), "music_capabilities", {})

    assert result == {
        "category": "internal_error",
        "message": "Music Friend could not complete the request.",
    }
    assert source.calls == [("capabilities",)]
