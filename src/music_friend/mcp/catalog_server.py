"""Bounded stdio MCP tools over Music Friend's local catalog."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal, cast

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from music_friend.domain import (
    Artist,
    Event,
    InboxEntry,
    InboxState,
    RefreshRun,
    Release,
    Signal,
    SignalKind,
    WatchlistAction,
    WatchlistEntry,
)
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import update_inbox_state

_INVALID_ARGUMENTS: dict[str, object] = {
    "category": "invalid_arguments",
    "message": "Invalid tool arguments.",
}
_INTERNAL_ERROR: dict[str, object] = {
    "category": "internal_error",
    "message": "Music Friend could not complete the request.",
}
_NOT_FOUND: dict[str, object] = {
    "category": "not_found",
    "message": "Music Friend record was not found.",
}
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_OPEN_WORLD_MUTATING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, open_world_hint=True
)
_DESTRUCTIVE_MUTATING = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False
)
_EMPTY_SCHEMA: dict[str, object] = {
    "additionalProperties": False,
    "properties": {},
    "type": "object",
}
_LOCAL_ID_SCHEMA: dict[str, object] = {
    "maxLength": 4096,
    "minLength": 1,
    "pattern": r"^\S(?:[\s\S]*\S)?$",
    "type": "string",
}
_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "music_status": _EMPTY_SCHEMA,
    "refresh_music": {
        "additionalProperties": False,
        "properties": {
            "kind": {"enum": ["catalog", "releases", "events", "all"], "type": "string"}
        },
        "required": ["kind"],
        "type": "object",
    },
    "search_catalog": {
        "additionalProperties": False,
        "properties": {
            "query": {
                "maxLength": 256,
                "minLength": 1,
                "pattern": r"^\S(?:[\s\S]*\S)?$",
                "type": "string",
            },
            "limit": {"minimum": 1, "maximum": 50, "type": "integer"},
        },
        "required": ["query", "limit"],
        "type": "object",
    },
    "list_watchlist": {
        "additionalProperties": False,
        "properties": {"limit": {"minimum": 1, "maximum": 100, "type": "integer"}},
        "required": ["limit"],
        "type": "object",
    },
    "update_watchlist": {
        "additionalProperties": False,
        "properties": {
            "artist_id": _LOCAL_ID_SCHEMA,
            "action": {"enum": ["add", "pin", "mute", "remove"], "type": "string"},
        },
        "required": ["artist_id", "action"],
        "type": "object",
    },
    "list_inbox": {
        "additionalProperties": False,
        "properties": {
            "state": {"enum": ["unread", "saved", "dismissed", None], "type": ["string", "null"]},
            "limit": {"minimum": 1, "maximum": 100, "type": "integer"},
        },
        "required": ["limit"],
        "type": "object",
    },
    "update_inbox_item": {
        "additionalProperties": False,
        "properties": {
            "inbox_id": _LOCAL_ID_SCHEMA,
            "state": {"enum": ["unread", "saved", "dismissed"], "type": "string"},
        },
        "required": ["inbox_id", "state"],
        "type": "object",
    },
    "explain_inbox_item": {
        "additionalProperties": False,
        "properties": {"inbox_id": _LOCAL_ID_SCHEMA},
        "required": ["inbox_id"],
        "type": "object",
    },
    "summarize_listening_history": {
        "additionalProperties": False,
        "properties": {
            "since": {"format": "date-time", "type": ["string", "null"]},
            "until": {"format": "date-time", "type": ["string", "null"]},
            "limit": {"minimum": 1, "maximum": 50, "type": "integer"},
        },
        "required": ["since", "until", "limit"],
        "type": "object",
    },
}

RefreshCallback = Callable[[Literal["catalog", "releases", "events", "all"]], object]
Clock = Callable[[], datetime]


class _InvalidArguments(ValueError):
    """Internal marker for a closed MCP argument failure.

    Carries an optional caller-facing message (e.g. naming which range or
    date-time rule was violated); falls back to the generic message when none
    is given.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or str(_INVALID_ARGUMENTS["message"]))


async def _enforce_tool_contract(
    context: ServerRequestContext[Any, Any], call_next: CallNext
) -> HandlerResult:
    """Publish and enforce the exact bounded tool schemas."""
    if context.method == "tools/call" and not _has_valid_tool_arguments(context.params):
        return _tool_result(dict(_INVALID_ARGUMENTS))
    result = await call_next(context)
    if context.method != "tools/list" or not isinstance(result, dict):
        return result
    listed = result.get("tools")
    if not isinstance(listed, list):
        return result
    return {**result, "tools": [_with_fixed_input_schema(tool) for tool in listed]}


def _has_valid_tool_arguments(params: Mapping[str, Any] | None) -> bool:
    if not isinstance(params, Mapping):
        return True
    name = params.get("name")
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        return True
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        return False
    schema = _TOOL_SCHEMAS[name]
    properties = schema["properties"]
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return False
    if set(arguments) - set(properties) or not set(required).issubset(arguments):
        return False
    try:
        if name == "refresh_music":
            _refresh_kind(arguments["kind"])
        elif name == "search_catalog":
            _search_arguments(arguments["query"], arguments["limit"])
        elif name in {"list_watchlist", "list_inbox"}:
            _limit(arguments["limit"], maximum=100)
            if name == "list_inbox":
                _inbox_state(arguments.get("state"))
        elif name == "update_watchlist":
            _local_id(arguments["artist_id"])
            _watchlist_action(arguments["action"])
        elif name == "update_inbox_item":
            _local_id(arguments["inbox_id"])
            _inbox_state(arguments["state"], required=True)
        elif name == "explain_inbox_item":
            _local_id(arguments["inbox_id"])
        elif name == "summarize_listening_history":
            _history_arguments(arguments["since"], arguments["until"], arguments["limit"])
    except _InvalidArguments:
        return False
    return True


def _with_fixed_input_schema(tool: object) -> object:
    if not isinstance(tool, Mapping):
        return tool
    name = tool.get("name")
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        return dict(tool)
    return {**tool, "inputSchema": _TOOL_SCHEMAS[name]}


def create_music_server(
    application: MusicFriendApplication,
    *,
    refresh: RefreshCallback,
    now: Clock | None = None,
) -> MCPServer[Any]:
    """Create the fixed provider-neutral local Music Friend MCP surface."""
    if not isinstance(application, MusicFriendApplication) or not callable(refresh):
        raise ValueError("application and refresh callback are required")
    clock = _utc_now if now is None else now
    if not callable(clock):
        raise ValueError("now must be callable")
    server: MCPServer[Any] = MCPServer(
        name="music-friend",
        title="Music Friend",
        description="Local catalog tools for Music Friend.",
        middleware=[cast(ServerMiddleware[Any], _enforce_tool_contract)],
    )

    @server.tool(
        name="music_status",
        description="Inspect local Music Friend status.",
        annotations=_READ_ONLY,
    )
    async def music_status() -> CallToolResult:
        return _safe_call(lambda: _status(application))

    @server.tool(
        name="refresh_music",
        description="Run one bounded local music refresh.",
        annotations=_OPEN_WORLD_MUTATING,
    )
    async def refresh_music(
        kind: Literal["catalog", "releases", "events", "all"],
    ) -> CallToolResult:
        return _safe_call(lambda: _refresh_result(refresh(_refresh_kind(kind))))

    @server.tool(
        name="search_catalog", description="Search local catalog artists.", annotations=_READ_ONLY
    )
    async def search_catalog(query: str, limit: int) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_query, parsed_limit = _search_arguments(query, limit)
            return {
                "items": [
                    _artist(item)
                    for item in application.search_artists(parsed_query, limit=parsed_limit)
                ]
            }

        return _safe_call(action)

    @server.tool(
        name="list_watchlist", description="List monitored local artists.", annotations=_READ_ONLY
    )
    async def list_watchlist(limit: int) -> CallToolResult:
        return _safe_call(
            lambda: {
                "items": [
                    _watchlist(item)
                    for item in application.list_watchlist(limit=_limit(limit, maximum=100))
                ]
            }
        )

    @server.tool(
        name="update_watchlist",
        description="Update one local artist watchlist decision.",
        annotations=_DESTRUCTIVE_MUTATING,
    )
    async def update_watchlist(
        artist_id: str, action: Literal["add", "pin", "mute", "remove"]
    ) -> CallToolResult:
        def action_result() -> dict[str, object]:
            local_id = _local_id(artist_id)
            selected = _watchlist_action(action)
            if application.get_artist(local_id) is None:
                return dict(_NOT_FOUND)
            updated_at = _now(clock)
            if selected is WatchlistAction.ADD:
                application.set_watchlist_add(local_id, updated_at=updated_at)
            elif selected is WatchlistAction.PIN:
                application.set_watchlist_pin(local_id, updated_at=updated_at)
            elif selected is WatchlistAction.MUTE:
                application.set_watchlist_mute(local_id, updated_at=updated_at)
            else:
                application.remove_watchlist_override(local_id)
            return {"artist_id": local_id, "action": action}

        return _safe_call(action_result)

    @server.tool(
        name="list_inbox",
        description="List local Music Friend inbox entries.",
        annotations=_READ_ONLY,
    )
    async def list_inbox(
        state: Literal["unread", "saved", "dismissed"] | None = None, limit: int = 50
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_state = _inbox_state(state)
            return {
                "items": [
                    _inbox(item)
                    for item in application.list_inbox_entries(
                        parsed_state, limit=_limit(limit, maximum=100)
                    )
                ]
            }

        return _safe_call(action)

    @server.tool(
        name="update_inbox_item",
        description="Update one local inbox decision.",
        annotations=_MUTATING,
    )
    async def update_inbox_item(
        inbox_id: str, state: Literal["unread", "saved", "dismissed"]
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            local_id = _local_id(inbox_id)
            selected = _inbox_state(state, required=True)
            assert selected is not None
            try:
                entry = update_inbox_state(application, local_id, selected, updated_at=_now(clock))
            except ValueError:
                return dict(_NOT_FOUND)
            return _inbox(entry)

        return _safe_call(action)

    @server.tool(
        name="explain_inbox_item",
        description="Explain one local inbox entry.",
        annotations=_READ_ONLY,
    )
    async def explain_inbox_item(inbox_id: str) -> CallToolResult:
        return _safe_call(lambda: _explain_inbox(application, _local_id(inbox_id)))

    @server.tool(
        name="summarize_listening_history",
        description="Summarize locally imported listening evidence for a UTC date range.",
        annotations=_READ_ONLY,
    )
    async def summarize_listening_history(
        since: str | None, until: str | None, limit: int
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_since, parsed_until, parsed_limit = _history_arguments(since, until, limit)
            try:
                summary = application.summarize_history(
                    since=parsed_since, until=parsed_until, limit=parsed_limit
                )
            except ValueError as error:
                raise _InvalidArguments(str(error)) from error
            return {
                "evidence_boundary": "imported Spotify music history",
                "since": summary.since,
                "until": summary.until,
                "first_played_at": summary.first_played_at,
                "last_played_at": summary.last_played_at,
                "play_count": summary.play_count,
                "milliseconds_played": summary.milliseconds_played,
                "skipped_count": summary.skipped_count,
                "brief_count": summary.brief_count,
                "top_artists": [_history_ranking(item) for item in summary.top_artists],
                "top_tracks": [_history_ranking(item) for item in summary.top_tracks],
            }

        return _safe_call(action)

    return server


def _safe_call(action: Callable[[], dict[str, object]]) -> CallToolResult:
    try:
        result = action()
    except _InvalidArguments as error:
        result = {"category": "invalid_arguments", "message": str(error)}
    except Exception:
        result = dict(_INTERNAL_ERROR)
    return _tool_result(result)


def _tool_result(result: dict[str, object]) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(result, separators=(",", ":"), sort_keys=True))
        ],
        structured_content=result,
        is_error="category" in result,
    )


def _refresh_kind(value: object) -> Literal["catalog", "releases", "events", "all"]:
    if value == "catalog":
        return "catalog"
    if value == "releases":
        return "releases"
    if value == "events":
        return "events"
    if value == "all":
        return "all"
    raise _InvalidArguments()


def _search_arguments(query: object, limit: object) -> tuple[str, int]:
    if type(query) is not str or not query.strip() or query != query.strip() or len(query) > 256:
        raise _InvalidArguments()
    return query, _limit(limit, maximum=50)


def _limit(value: object, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _InvalidArguments()
    return value


def _local_id(value: object) -> str:
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 4096:
        raise _InvalidArguments()
    return value


def _watchlist_action(value: object) -> WatchlistAction | Literal["remove"]:
    if type(value) is not str:
        raise _InvalidArguments()
    if value == "remove":
        return "remove"
    try:
        return WatchlistAction(value)
    except ValueError as error:
        raise _InvalidArguments() from error


def _inbox_state(value: object, *, required: bool = False) -> InboxState | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise _InvalidArguments()
    try:
        return InboxState(value)
    except ValueError as error:
        raise _InvalidArguments() from error


def _history_arguments(
    since: object, until: object, limit: object
) -> tuple[str | None, str | None, int]:
    # Only a shallow type/length check happens here. The MCP request-validation
    # middleware (`_has_valid_tool_arguments`) runs this same function ahead of
    # the actual tool call, before the domain layer's database connection is
    # necessarily available, so it cannot perform full RFC 3339 parsing or range
    # checks. Real timestamp parsing (accepting any RFC 3339 offset, rejecting
    # naive timestamps and impossible calendar dates) and range validation
    # (reversed or zero-length) happen in `summarize_history`
    # (music_friend.store.spotify_history), whose `ValueError` the tool handler
    # above translates into `_InvalidArguments` with the specific message.
    for value in (since, until):
        if value is not None and (type(value) is not str or not value.strip() or len(value) > 64):
            raise _InvalidArguments("since and until must be RFC 3339 date-time strings")
    return since, until, _limit(limit, maximum=50)  # type: ignore[return-value]


def _history_ranking(value: object) -> dict[str, object]:
    return {
        "name": getattr(value, "name"),
        "play_count": getattr(value, "play_count"),
        "milliseconds_played": getattr(value, "milliseconds_played"),
    }


def _now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock is invalid")
    return value.astimezone(timezone.utc)


def _status(application: MusicFriendApplication) -> dict[str, object]:
    latest = application.list_refresh_runs(limit=1)
    unread = application.list_inbox_entries(InboxState.UNREAD, limit=1)
    return {
        "status": "ready",
        "inbox": {"has_unread": bool(unread)},
        "latest_refresh": None if not latest else _refresh_run(latest[0]),
    }


def _refresh_result(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        result = dict(value)
        if set(result) <= {"status", "kind"} and type(result.get("status")) is str:
            return result
    run = getattr(value, "run", None)
    already_running = getattr(value, "already_running", None)
    skip_reason = getattr(value, "skip_reason", None)
    if type(already_running) is bool:
        if already_running:
            return {"status": "partial"}
        if run is None and skip_reason is not None:
            return {"status": "skipped", "reason": skip_reason}
        if isinstance(run, RefreshRun):
            payload = _refresh_run(run)
            if skip_reason is not None:
                payload["events_skipped_reason"] = skip_reason
            return payload
    raise ValueError("refresh callback returned an invalid result")


def _artist(value: Artist) -> dict[str, object]:
    return {
        "local_id": value.local_id,
        "display_name": value.display_name,
        "identity_confidence": value.identity_confidence.value,
    }


def _watchlist(value: WatchlistEntry) -> dict[str, object]:
    return {
        "artist": _artist(value.artist),
        "inclusion_reason": value.inclusion_reason.value,
        "affinity": {
            "total_points": value.affinity.total_points,
            "saved_track_count": value.affinity.saved_track_count,
        },
    }


def _inbox(value: InboxEntry) -> dict[str, object]:
    return {
        "local_id": value.local_id,
        "state": value.state.value,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def _refresh_run(value: RefreshRun) -> dict[str, object]:
    return {
        "kind": value.kind.value,
        "status": value.status.value,
        "started_at": value.started_at.isoformat(),
        "finished_at": None if value.finished_at is None else value.finished_at.isoformat(),
        "metrics": [
            {"kind": item.kind.value, "count": item.count} for item in value.summary.metrics
        ],
    }


def _explain_inbox(application: MusicFriendApplication, inbox_id: str) -> dict[str, object]:
    entry = application.get_inbox_entry(inbox_id)
    if entry is None:
        return dict(_NOT_FOUND)
    signal = application.get_signal(entry.signal_local_id)
    if signal is None:
        return dict(_NOT_FOUND)
    record = _signal_record(application, signal)
    if record is None:
        return dict(_NOT_FOUND)
    return {
        "entry": _inbox(entry),
        "record": record,
        "reasons": [
            {"kind": reason.kind.value, "detail": reason.detail}
            for reason in signal.explanation.reasons
        ],
    }


def _signal_record(application: MusicFriendApplication, signal: Signal) -> dict[str, object] | None:
    if signal.kind is SignalKind.RELEASE:
        release = application.get_release(signal.record_local_id)
        return None if release is None else _release(release)
    event = application.get_event(signal.record_local_id)
    return None if event is None else _event(event)


def _release(value: Release) -> dict[str, object]:
    return {
        "kind": "release",
        "local_id": value.local_id,
        "title": value.title,
        "release_type": value.release_type,
        "release_date": value.release_date.isoformat(),
        "date_precision": value.date_precision.value,
        "artist_ids": list(value.artist_refs),
    }


def _event(value: Event) -> dict[str, object]:
    return {
        "kind": "event",
        "local_id": value.local_id,
        "title": value.title,
        "artist_ids": list(value.artist_refs),
        "venue_name": value.venue_name,
        "locality": value.locality,
        "starts_at": None if value.starts_at is None else value.starts_at.isoformat(),
        "time_precision": value.time_precision,
        "links": list(value.source_links),
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["create_music_server"]
