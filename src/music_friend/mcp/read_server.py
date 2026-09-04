"""MCP read-tool adapter for provider-neutral Music Friend sources."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Literal, Protocol, TypeVar, cast

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from music_friend.domain import Artist, CatalogItem, CatalogItemBatch, Release, SourceReference
from music_friend.errors import MusicFriendError
from music_friend.providers import MusicSource, ProviderCapabilities

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_INVALID_ARGUMENTS: dict[str, object] = {
    "category": "invalid_arguments",
    "message": "Invalid tool arguments.",
}
_INTERNAL_ERROR: dict[str, object] = {
    "category": "internal_error",
    "message": "Music Friend could not complete the request.",
}
_SOURCE_REFERENCE = {
    "additionalProperties": False,
    "properties": {
        "source": {"type": "string"},
        "native_id": {"type": "string"},
        "canonical_url": {"type": ["string", "null"]},
        "observed_at": {"format": "date-time", "type": "string"},
    },
    "required": ["source", "native_id", "canonical_url", "observed_at"],
    "type": "object",
}
_EMPTY_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {},
    "type": "object",
}
_CURSOR_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {"cursor": {"type": ["string", "null"]}},
    "type": "object",
}
_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "music_capabilities": _EMPTY_SCHEMA,
    "music_health": _EMPTY_SCHEMA,
    "search_artists": {
        "additionalProperties": False,
        "properties": {
            "query": {
                "description": "Must not begin or end with whitespace.",
                "maxLength": 256,
                "minLength": 1,
                "pattern": r"^\S(?:[\s\S]*\S)?$",
                "type": "string",
            },
            "limit": {"maximum": 10, "minimum": 1, "type": "integer"},
        },
        "required": ["query", "limit"],
        "type": "object",
    },
    "followed_artists": _CURSOR_SCHEMA,
    "saved_items": _CURSOR_SCHEMA,
    "top_items": {
        "additionalProperties": False,
        "properties": {
            "time_range": {"enum": ["short_term", "medium_term", "long_term"], "type": "string"},
            "limit": {"maximum": 50, "minimum": 1, "type": "integer"},
        },
        "required": ["time_range", "limit"],
        "type": "object",
    },
    "recent_releases": {
        "additionalProperties": False,
        "properties": {
            "artist_refs": {
                "items": _SOURCE_REFERENCE,
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
_REQUIRED_ARGUMENTS: dict[str, frozenset[str]] = {
    "music_capabilities": frozenset(),
    "music_health": frozenset(),
    "search_artists": frozenset({"query", "limit"}),
    "followed_artists": frozenset(),
    "saved_items": frozenset(),
    "top_items": frozenset({"time_range", "limit"}),
    "recent_releases": frozenset({"artist_refs", "since"}),
}

T = TypeVar("T")
_PageT = TypeVar("_PageT", covariant=True)


class _ItemPage(Protocol[_PageT]):
    @property
    def items(self) -> tuple[_PageT, ...]: ...

    @property
    def next_cursor(self) -> str | None: ...


class _InvalidArguments(ValueError):
    """Signal a bad MCP argument shape without exposing its contents."""


async def _enforce_tool_contract(
    context: ServerRequestContext[Any, Any], call_next: CallNext
) -> HandlerResult:
    """Expose and enforce the fixed public tool contract through MCP middleware."""
    if context.method == "tools/call" and not _has_valid_tool_arguments(context.params):
        return _invalid_tool_result()

    result = await call_next(context)
    if context.method != "tools/list" or not isinstance(result, dict):
        return result
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    return {
        **result,
        "tools": [_with_fixed_input_schema(tool) for tool in tools],
    }


def _has_valid_tool_arguments(params: Mapping[str, Any] | None) -> bool:
    """Reject invalid fixed-tool calls before the registered handler can delegate."""
    if not isinstance(params, Mapping):
        return True
    name = params.get("name")
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        return True
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        return False
    properties = _TOOL_SCHEMAS[name]["properties"]
    if not isinstance(properties, Mapping):
        return False
    if set(arguments) - set(properties):
        return False
    if not _REQUIRED_ARGUMENTS[name].issubset(arguments):
        return False
    try:
        if name == "search_artists":
            _parse_search(arguments["query"], arguments["limit"])
        elif name in {"followed_artists", "saved_items"}:
            _parse_cursor(arguments.get("cursor"))
        elif name == "top_items":
            _parse_top_items(arguments["time_range"], arguments["limit"])
        elif name == "recent_releases":
            _parse_recent_releases(arguments["artist_refs"], arguments["since"])
    except _InvalidArguments:
        return False
    return True


def _with_fixed_input_schema(tool: object) -> object:
    """Return a wire tool descriptor with the public fixed input schema."""
    if not isinstance(tool, Mapping):
        return tool
    name = tool.get("name")
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        return dict(tool)
    return {**tool, "inputSchema": _TOOL_SCHEMAS[name]}


def create_read_server(source: MusicSource) -> MCPServer[Any]:
    """Create the fixed provider-neutral MCP read surface for ``source``."""
    server: MCPServer[Any] = MCPServer(
        name="music-friend",
        title="Music Friend",
        description="Provider-neutral music read tools.",
        middleware=[cast(ServerMiddleware[Any], _enforce_tool_contract)],
    )

    @server.tool(
        name="music_capabilities",
        description="Inspect available music source capabilities.",
        annotations=_READ_ONLY,
    )
    async def music_capabilities() -> CallToolResult:
        return _safe_call(lambda: _serialize_capabilities(source.capabilities()))

    @server.tool(
        name="music_health",
        description="Inspect music source health and capabilities.",
        annotations=_READ_ONLY,
    )
    async def music_health() -> CallToolResult:
        def action() -> dict[str, object]:
            health = source.health()
            return {
                "status": health.status.value,
                "capabilities": _serialize_capabilities(health.capabilities),
            }

        return _safe_call(action)

    @server.tool(
        name="search_artists",
        description="Search normalized artists from the connected music source.",
        annotations=_READ_ONLY,
    )
    async def search_artists(
        query: str,
        limit: int,
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_query, parsed_limit = _parse_search(query, limit)
            return _serialize_page(
                source.search_artists(parsed_query, parsed_limit), _serialize_artist
            )

        return _safe_call(action)

    @server.tool(
        name="followed_artists",
        description="List normalized artists followed by the user.",
        annotations=_READ_ONLY,
    )
    async def followed_artists(
        cursor: str | None = None,
    ) -> CallToolResult:
        return _safe_call(
            lambda: _serialize_page(
                source.followed_artists(_parse_cursor(cursor)), _serialize_artist
            )
        )

    @server.tool(
        name="saved_items",
        description="List normalized music items saved by the user.",
        annotations=_READ_ONLY,
    )
    async def saved_items(cursor: str | None = None) -> CallToolResult:
        return _safe_call(
            lambda: _serialize_catalog_item_batch(source.saved_items(_parse_cursor(cursor)))
        )

    @server.tool(
        name="top_items",
        description="List normalized top music items for a time range.",
        annotations=_READ_ONLY,
    )
    async def top_items(
        time_range: Literal["short_term", "medium_term", "long_term"],
        limit: int,
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_time_range, parsed_limit = _parse_top_items(time_range, limit)
            return _serialize_catalog_item_batch(source.top_items(parsed_time_range, parsed_limit))

        return _safe_call(action)

    @server.tool(
        name="recent_releases",
        description="List normalized recent releases for artist references.",
        annotations=_READ_ONLY,
    )
    async def recent_releases(
        artist_refs: list[dict[str, object]],
        since: str,
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_references, parsed_since = _parse_recent_releases(artist_refs, since)
            return _serialize_page(
                source.recent_releases(parsed_references, parsed_since),
                _serialize_release,
            )

        return _safe_call(action)

    return server


def _safe_call(action: Callable[[], dict[str, object]]) -> CallToolResult:
    try:
        result = action()
    except _InvalidArguments:
        result = dict(_INVALID_ARGUMENTS)
    except MusicFriendError as error:
        result = error.to_public_dict()
    except Exception:
        result = dict(_INTERNAL_ERROR)
    return _tool_result(result)


def _invalid_tool_result() -> CallToolResult:
    return _tool_result(dict(_INVALID_ARGUMENTS))


def _tool_result(result: dict[str, object]) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(result, separators=(",", ":"), sort_keys=True))
        ],
        structured_content=result,
        is_error="category" in result,
    )


def _parse_search(query: object, limit: object) -> tuple[str, int]:
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 256
        or not query.strip()
        or query != query.strip()
    ):
        raise _InvalidArguments()
    if type(limit) is not int or not 1 <= limit <= 10:
        raise _InvalidArguments()
    return query, limit


def _parse_cursor(cursor: object) -> str | None:
    if cursor is not None and not isinstance(cursor, str):
        raise _InvalidArguments()
    return cursor


def _parse_top_items(time_range: object, limit: object) -> tuple[str, int]:
    if not isinstance(time_range, str) or time_range not in {
        "short_term",
        "medium_term",
        "long_term",
    }:
        raise _InvalidArguments()
    if type(limit) is not int or not 1 <= limit <= 50:
        raise _InvalidArguments()
    return time_range, limit


def _parse_recent_releases(
    raw_references: object, raw_since: object
) -> tuple[tuple[SourceReference, ...], datetime]:
    if not isinstance(raw_references, list) or not 1 <= len(raw_references) <= 10:
        raise _InvalidArguments()
    references = tuple(_parse_source_reference(value) for value in raw_references)
    return references, _parse_datetime(raw_since)


def _parse_source_reference(value: object) -> SourceReference:
    if not isinstance(value, dict) or set(value) != {
        "source",
        "native_id",
        "canonical_url",
        "observed_at",
    }:
        raise _InvalidArguments()
    source = value["source"]
    native_id = value["native_id"]
    canonical_url = value["canonical_url"]
    if not isinstance(source, str) or not isinstance(native_id, str):
        raise _InvalidArguments()
    if canonical_url is not None and not isinstance(canonical_url, str):
        raise _InvalidArguments()
    try:
        return SourceReference(
            source, native_id, canonical_url, _parse_datetime(value["observed_at"])
        )
    except (TypeError, ValueError) as error:
        raise _InvalidArguments() from error


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise _InvalidArguments()
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise _InvalidArguments() from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidArguments()
    return parsed


def _serialize_capabilities(capabilities: ProviderCapabilities) -> dict[str, object]:
    return {
        "supported": sorted(capability.value for capability in capabilities.supported),
        "granted": sorted(capability.value for capability in capabilities.granted),
        "effective": sorted(capability.value for capability in capabilities.effective),
    }


def _serialize_page(
    page: _ItemPage[T], serializer: Callable[[T], dict[str, object]]
) -> dict[str, object]:
    return {"items": [serializer(item) for item in page.items], "next_cursor": page.next_cursor}


def _serialize_catalog_item_batch(batch: CatalogItemBatch) -> dict[str, object]:
    return {
        "items": [_serialize_catalog_item(item) for item in batch.items],
        "artists": [_serialize_artist(artist) for artist in batch.artists],
        "next_cursor": batch.next_cursor,
    }


def _serialize_source_reference(reference: SourceReference) -> dict[str, object]:
    return {
        "source": reference.source,
        "native_id": reference.native_id,
        "canonical_url": reference.canonical_url,
        "observed_at": reference.observed_at.isoformat(),
    }


def _serialize_artist(artist: Artist) -> dict[str, object]:
    return {
        "local_id": artist.local_id,
        "display_name": artist.display_name,
        "source_refs": [_serialize_source_reference(reference) for reference in artist.source_refs],
        "identity_confidence": artist.identity_confidence.value,
        "observed_at": artist.observed_at.isoformat(),
    }


def _serialize_catalog_item(item: CatalogItem) -> dict[str, object]:
    return {
        "kind": item.kind,
        "local_id": item.local_id,
        "title": item.title,
        "artist_refs": list(item.artist_refs),
        "source_refs": [_serialize_source_reference(reference) for reference in item.source_refs],
        "observed_at": item.observed_at.isoformat(),
    }


def _serialize_release(release: Release) -> dict[str, object]:
    return {
        "local_id": release.local_id,
        "title": release.title,
        "release_type": release.release_type,
        "release_date": release.release_date.isoformat(),
        "date_precision": release.date_precision.value,
        "artist_refs": list(release.artist_refs),
        "source_refs": [
            _serialize_source_reference(reference) for reference in release.source_refs
        ],
        "observed_at": release.observed_at.isoformat(),
    }
