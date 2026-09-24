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
from music_friend.domain.text import sanitize_display_name
from music_friend.store.spotify_history import HistoryArgumentError
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


def _local_id_schema(description: str) -> dict[str, object]:
    """Return `_LOCAL_ID_SCHEMA` with a per-argument `description` naming its source tool.

    A shared base dict is kept so every local-identifier argument stays subject
    to the same length/pattern validation (types, required fields, and enums
    are unchanged); only the description text, which names where the value
    comes from, differs per argument.
    """
    return {**_LOCAL_ID_SCHEMA, "description": description}


_TOOL_SCHEMAS: dict[str, dict[str, object]] = {
    "music_status": _EMPTY_SCHEMA,
    "refresh_music": {
        "additionalProperties": False,
        "properties": {
            "kind": {
                "description": (
                    "Which local record kinds to refresh from the provider: "
                    "'catalog' (watched artists' tracks/releases), 'releases' "
                    "(new release discovery for watched artists), 'events' "
                    "(new Ticketmaster event discovery for watched artists), "
                    "or 'all' for every kind in one bounded run."
                ),
                "enum": ["catalog", "releases", "events", "all"],
                "type": "string",
            }
        },
        "required": ["kind"],
        "type": "object",
    },
    "search_catalog": {
        "additionalProperties": False,
        "properties": {
            "query": {
                "description": "Free-text artist name or fragment to search for in the local catalog.",
                "maxLength": 256,
                "minLength": 1,
                "pattern": r"^\S(?:[\s\S]*\S)?$",
                "type": "string",
            },
            "limit": {
                "description": "Maximum number of matching artists to return (1-50).",
                "minimum": 1,
                "maximum": 50,
                "type": "integer",
            },
        },
        "required": ["query", "limit"],
        "type": "object",
    },
    "list_watchlist": {
        "additionalProperties": False,
        "properties": {
            "limit": {
                "description": "Maximum number of watchlist entries to return (1-100).",
                "minimum": 1,
                "maximum": 100,
                "type": "integer",
            }
        },
        "required": ["limit"],
        "type": "object",
    },
    "update_watchlist": {
        "additionalProperties": False,
        "properties": {
            "artist_id": _local_id_schema(
                "The artist's local_id, from a search_catalog result item or a "
                "list_watchlist entry's artist.local_id."
            ),
            "action": {
                "description": (
                    "'add' starts watching the artist; 'pin' watches it with "
                    "priority; 'mute' keeps it watched but suppresses new inbox "
                    "entries for it; 'remove' stops watching it entirely."
                ),
                "enum": ["add", "pin", "mute", "remove"],
                "type": "string",
            },
        },
        "required": ["artist_id", "action"],
        "type": "object",
    },
    "list_inbox": {
        "additionalProperties": False,
        "properties": {
            "state": {
                "description": (
                    "Filter by inbox decision state ('unread', 'saved', or "
                    "'dismissed'), or omit/null for every state."
                ),
                "enum": ["unread", "saved", "dismissed", None],
                "type": ["string", "null"],
            },
            "limit": {
                "description": "Maximum number of inbox entries to return (1-100).",
                "minimum": 1,
                "maximum": 100,
                "type": "integer",
            },
        },
        "required": ["limit"],
        "type": "object",
    },
    "update_inbox_item": {
        "additionalProperties": False,
        "properties": {
            "inbox_id": _local_id_schema(
                "The inbox entry's local_id, from a list_inbox result item or "
                "the entry.local_id returned by explain_inbox_item."
            ),
            "state": {
                "description": (
                    "New decision state for the entry: 'unread' (undecided), "
                    "'saved' (kept), or 'dismissed' (not interested)."
                ),
                "enum": ["unread", "saved", "dismissed"],
                "type": "string",
            },
        },
        "required": ["inbox_id", "state"],
        "type": "object",
    },
    "explain_inbox_item": {
        "additionalProperties": False,
        "properties": {
            "inbox_id": _local_id_schema(
                "The inbox entry's local_id, from a list_inbox result item."
            )
        },
        "required": ["inbox_id"],
        "type": "object",
    },
    "summarize_listening_history": {
        "additionalProperties": False,
        "properties": {
            "since": {
                "description": (
                    "Inclusive RFC 3339 UTC date-time lower bound for imported "
                    "listening history, or null for no lower bound."
                ),
                "format": "date-time",
                "type": ["string", "null"],
            },
            "until": {
                "description": (
                    "Exclusive RFC 3339 UTC date-time upper bound for imported "
                    "listening history, or null for no upper bound."
                ),
                "format": "date-time",
                "type": ["string", "null"],
            },
            "limit": {
                "description": "Maximum number of top artists/tracks to return in the summary (1-50).",
                "minimum": 1,
                "maximum": 50,
                "type": "integer",
            },
        },
        "required": ["since", "until", "limit"],
        "type": "object",
    },
    "get_setup": _EMPTY_SCHEMA,
    "update_setup": {
        "additionalProperties": False,
        "properties": {
            "spotify_client_id": {
                "description": (
                    "Spotify developer application client ID, or null to leave unchanged."
                ),
                "maxLength": 256,
                "minLength": 1,
                "pattern": r"^\S(?:[\s\S]*\S)?$",
                "type": ["string", "null"],
            },
            "event_country_code": {
                "description": "Two-letter uppercase event discovery country code (e.g., 'US'), or null.",
                "pattern": r"^[A-Z]{2}$",
                "type": ["string", "null"],
            },
            "event_postal_code": {
                "description": "Event discovery postal code (e.g., '94110'), or null.",
                "maxLength": 16,
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9 -]{0,15}$",
                "type": ["string", "null"],
            },
            "event_radius": {
                "description": (
                    "Event discovery search radius, 1-100 km/miles (depends on event_radius_unit), or null."
                ),
                "minimum": 1,
                "maximum": 100,
                "type": ["number", "null"],
            },
            "event_radius_unit": {
                "description": "Event discovery radius unit: 'miles' or 'kilometers', or null.",
                "enum": ["miles", "kilometers", None],
                "type": ["string", "null"],
            },
        },
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
    if context.method == "tools/call":
        error = _invalid_tool_arguments(context.params)
        if error is not None:
            return _tool_result({"category": "invalid_arguments", "message": str(error)})
    result = await call_next(context)
    if context.method != "tools/list" or not isinstance(result, dict):
        return result
    listed = result.get("tools")
    if not isinstance(listed, list):
        return result
    return {**result, "tools": [_with_fixed_input_schema(tool) for tool in listed]}


def _invalid_tool_arguments(params: Mapping[str, Any] | None) -> _InvalidArguments | None:
    """Return the specific violation for an invalid `tools/call`, or `None` if it is valid."""
    if not isinstance(params, Mapping):
        return None
    name = params.get("name")
    if not isinstance(name, str) or name not in _TOOL_SCHEMAS:
        return None
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        return _InvalidArguments("arguments must be an object")
    schema = _TOOL_SCHEMAS[name]
    properties = schema["properties"]
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return _InvalidArguments()
    extra = set(arguments) - set(properties)
    if extra:
        return _InvalidArguments(f"unexpected argument: {sorted(extra)[0]}")
    missing = set(required) - set(arguments)
    if missing:
        return _InvalidArguments(f"{sorted(missing)[0]} is required")
    try:
        if name == "refresh_music":
            _refresh_kind(arguments["kind"])
        elif name == "search_catalog":
            _search_arguments(arguments["query"], arguments["limit"])
        elif name in {"list_watchlist", "list_inbox"}:
            _limit(arguments["limit"], maximum=100, field="limit")
            if name == "list_inbox":
                _inbox_state(arguments.get("state"))
        elif name == "update_watchlist":
            _local_id(arguments["artist_id"], field="artist_id")
            _watchlist_action(arguments["action"])
        elif name == "update_inbox_item":
            _local_id(arguments["inbox_id"], field="inbox_id")
            _inbox_state(arguments["state"], required=True)
        elif name == "explain_inbox_item":
            _local_id(arguments["inbox_id"], field="inbox_id")
        elif name == "summarize_listening_history":
            _history_arguments(arguments["since"], arguments["until"], arguments["limit"])
        elif name == "get_setup":
            pass  # No validation needed
        elif name == "update_setup":
            _update_setup_arguments(arguments)
    except _InvalidArguments as error:
        return error
    return None


def _update_setup_arguments(arguments: Mapping[str, Any]) -> None:
    """Validate update_setup arguments."""
    if "spotify_client_id" in arguments:
        value = arguments["spotify_client_id"]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _InvalidArguments("spotify_client_id must be a non-empty string or null")

    if "event_country_code" in arguments:
        value = arguments["event_country_code"]
        if value is not None and (not isinstance(value, str) or not value or len(value) != 2 or not value.isupper()):
            raise _InvalidArguments("event_country_code must be a two-letter uppercase code or null")

    if "event_postal_code" in arguments:
        value = arguments["event_postal_code"]
        if value is not None and (not isinstance(value, str) or not value):
            raise _InvalidArguments("event_postal_code must be a non-empty string or null")

    if "event_radius" in arguments:
        value = arguments["event_radius"]
        if value is not None and (not isinstance(value, (int, float)) or value < 1 or value > 100):
            raise _InvalidArguments("event_radius must be 1-100 or null")

    if "event_radius_unit" in arguments:
        value = arguments["event_radius_unit"]
        if value is not None and value not in {"miles", "kilometers"}:
            raise _InvalidArguments("event_radius_unit must be 'miles', 'kilometers', or null")


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
        description=(
            "Inspect local Music Friend status: whether the inbox has unread "
            "entries and a summary of the most recent refresh_music run. "
            "Purpose: a cheap first call to orient before deciding what to do "
            "next. When to use: at the start of a session, or after "
            "refresh_music to see whether it produced anything. Call before: "
            "nothing required, but often precedes list_inbox when unread is "
            "true, or refresh_music when there is no recent refresh. Call "
            "after: nothing required. Local-only; does not contact a provider."
        ),
        annotations=_READ_ONLY,
    )
    async def music_status() -> CallToolResult:
        return _safe_call(lambda: _status(application))

    @server.tool(
        name="refresh_music",
        description=(
            "Run one bounded refresh of local music data for watched artists: "
            "'catalog' pulls tracks/releases, 'releases' discovers new "
            "releases, 'events' discovers new Ticketmaster events, and 'all' "
            "runs every kind in one call. Purpose: pull fresh provider data "
            "into the local catalog and inbox. When to use: when data looks "
            "stale, or before search_catalog/list_watchlist/list_inbox if "
            "the user wants current results rather than what was last "
            "imported. Call before: nothing required, though checking "
            "music_status first avoids starting a refresh that is already "
            "running. Call after: list_inbox or explain_inbox_item to see "
            "what the refresh surfaced, or music_status for a summary. "
            "THIS IS THE ONLY TOOL THAT CONTACTS A PROVIDER (read-only); "
            "every other tool in this server is local-only."
        ),
        annotations=_OPEN_WORLD_MUTATING,
    )
    async def refresh_music(
        kind: Literal["catalog", "releases", "events", "all"],
    ) -> CallToolResult:
        return _safe_call(lambda: _refresh_result(refresh(_refresh_kind(kind))))

    @server.tool(
        name="search_catalog",
        description=(
            "Search local catalog artists by name, returning each match's "
            "local_id, display_name, and identity_confidence. Purpose: find "
            "an artist's local_id so it can be passed to update_watchlist. "
            "When to use: before update_watchlist, when the user names an "
            "artist that is not already on the watchlist (check "
            "list_watchlist first if unsure). Call before: nothing required; "
            "run refresh_music('catalog') first only if the catalog is "
            "known to be stale or empty for that artist. Call after: "
            "update_watchlist, using the returned artist local_id. "
            "Local-only; does not contact a provider."
        ),
        annotations=_READ_ONLY,
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
        name="list_watchlist",
        description=(
            "List locally monitored artists with their inclusion reason and "
            "affinity, each including the artist's local_id. Purpose: see "
            "who is currently watched and why, and get artist local_ids for "
            "update_watchlist without a separate search_catalog call. When "
            "to use: to answer 'who am I watching', or before "
            "update_watchlist when the artist is likely already watched. "
            "Call before: nothing required. Call after: update_watchlist "
            "(using an entry's artist.local_id) to pin/mute/remove an entry, "
            "or search_catalog if the artist is not in the results. "
            "Local-only; does not contact a provider."
        ),
        annotations=_READ_ONLY,
    )
    async def list_watchlist(limit: int) -> CallToolResult:
        return _safe_call(
            lambda: {
                "items": [
                    _watchlist(item)
                    for item in application.list_watchlist(
                        limit=_limit(limit, maximum=100, field="limit")
                    )
                ]
            }
        )

    @server.tool(
        name="update_watchlist",
        description=(
            "Add, pin, mute, or remove one artist's local watchlist "
            "decision, identified by artist_id. Purpose: change which "
            "artists Music Friend monitors and how strongly. When to use: "
            "after the user names an artist to watch, prioritize, quiet, or "
            "stop watching. Call before: search_catalog or list_watchlist, "
            "to obtain the artist_id this tool requires (see the artist_id "
            "argument). Call after: nothing required; list_watchlist can "
            "confirm the change. Local-only, mutating; does not contact a "
            "provider."
        ),
        annotations=_DESTRUCTIVE_MUTATING,
    )
    async def update_watchlist(
        artist_id: str, action: Literal["add", "pin", "mute", "remove"]
    ) -> CallToolResult:
        def action_result() -> dict[str, object]:
            local_id = _local_id(artist_id, field="artist_id")
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
        description=(
            "List local inbox entries (new releases/events surfaced for "
            "watched artists), optionally filtered by decision state, each "
            "including a summary and the inbox_id. Purpose: see what is "
            "waiting for a decision or what was already saved/dismissed. "
            "When to use: after refresh_music or music_status reports "
            "unread entries, or whenever the user asks what's new. Call "
            "before: refresh_music first if the inbox is likely stale. Call "
            "after: explain_inbox_item for full detail on one entry, or "
            "update_inbox_item (using an entry's local_id) to decide it. "
            "Local-only; does not contact a provider."
        ),
        annotations=_READ_ONLY,
    )
    async def list_inbox(
        state: Literal["unread", "saved", "dismissed"] | None = None, limit: int = 50
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            parsed_state = _inbox_state(state)
            return {
                "items": [
                    _inbox(application, item)
                    for item in application.list_inbox_entries(
                        parsed_state, limit=_limit(limit, maximum=100, field="limit")
                    )
                ]
            }

        return _safe_call(action)

    @server.tool(
        name="update_inbox_item",
        description=(
            "Set one inbox entry's decision state to unread, saved, or "
            "dismissed, identified by inbox_id. Purpose: record the user's "
            "decision about one surfaced release or event. When to use: "
            "after the user says to keep, dismiss, or reconsider an inbox "
            "item. Call before: list_inbox or explain_inbox_item, to obtain "
            "the inbox_id this tool requires (see the inbox_id argument). "
            "Call after: nothing required. Local-only, mutating; does not "
            "contact a provider."
        ),
        annotations=_MUTATING,
    )
    async def update_inbox_item(
        inbox_id: str, state: Literal["unread", "saved", "dismissed"]
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            local_id = _local_id(inbox_id, field="inbox_id")
            selected = _inbox_state(state, required=True)
            assert selected is not None
            try:
                entry = update_inbox_state(application, local_id, selected, updated_at=_now(clock))
            except ValueError:
                return dict(_NOT_FOUND)
            return _inbox(application, entry)

        return _safe_call(action)

    @server.tool(
        name="explain_inbox_item",
        description=(
            "Return one inbox entry's full record (the release or event "
            "it's about) and the reasons it was surfaced, identified by "
            "inbox_id. Purpose: give the full detail list_inbox's compact "
            "summary omits, so the user can decide. When to use: before "
            "asking the user to decide on an inbox item, or when they ask "
            "'why was I shown this'. Call before: list_inbox, to obtain the "
            "inbox_id this tool requires (see the inbox_id argument). Call "
            "after: update_inbox_item to record the decision. Local-only; "
            "does not contact a provider."
        ),
        annotations=_READ_ONLY,
    )
    async def explain_inbox_item(inbox_id: str) -> CallToolResult:
        return _safe_call(
            lambda: _explain_inbox(application, _local_id(inbox_id, field="inbox_id"))
        )

    @server.tool(
        name="summarize_listening_history",
        description=(
            "Summarize imported Spotify listening history (play counts, "
            "milliseconds played, top artists/tracks) over an optional UTC "
            "date range. Purpose: answer questions about past listening. "
            "This is evidence, not preference, and never feeds watchlist "
            "affinity or update_watchlist decisions automatically -- the "
            "user decides what it implies. When to use: when the user asks "
            "about their listening history or wants a period summarized. "
            "Call before: nothing required; the history must already be "
            "imported via the CLI (`music-friend data import-spotify`), "
            "which MCP cannot do. Call after: nothing required. Local-only; "
            "does not contact a provider."
        ),
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
            except HistoryArgumentError as error:
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

    @server.tool(
        name="get_setup",
        description=(
            "Inspect Music Friend setup state: which configuration fields are set, "
            "which are missing, and what's needed for mcp_ready/doctor compliance. "
            "Secret values (like Ticketmaster key) are never returned. "
            "Purpose: check what agent-driven setup is incomplete. "
            "When to use: before update_setup, or to determine if setup is needed. "
            "Call before: nothing required. Call after: update_setup if fields are missing. "
            "Local-only; does not contact a provider."
        ),
        annotations=_READ_ONLY,
    )
    async def get_setup() -> CallToolResult:
        def action() -> dict[str, object]:
            store = LocalConfigStore()
            config = store.load()
            return {
                "status": "ready" if all(
                    value is not None
                    for value in (
                        config.spotify_client_id,
                        config.event_country_code,
                        config.event_postal_code,
                        config.event_radius,
                        config.event_radius_unit,
                    )
                ) else "incomplete",
                "spotify_client_id": config.spotify_client_id is not None,
                "event_country_code": config.event_country_code,
                "event_postal_code": config.event_postal_code,
                "event_radius": config.event_radius,
                "event_radius_unit": config.event_radius_unit,
                "missing_fields": [
                    name for name, value in [
                        ("spotify_client_id", config.spotify_client_id),
                        ("event_country_code", config.event_country_code),
                        ("event_postal_code", config.event_postal_code),
                        ("event_radius", config.event_radius),
                        ("event_radius_unit", config.event_radius_unit),
                    ]
                    if value is None
                ],
            }

        return _safe_call(action)

    @server.tool(
        name="update_setup",
        description=(
            "Update Music Friend setup configuration fields (non-secret only). "
            "Secret values must be set through the CLI. "
            "Pass null for any field to leave it unchanged. "
            "Purpose: programmatically complete setup from an agent. "
            "When to use: after get_setup reports missing fields. "
            "Call before: nothing required. Call after: get_setup to verify completion. "
            "Local-only, mutating; does not contact a provider."
        ),
        annotations=_MUTATING,
    )
    async def update_setup(
        spotify_client_id: str | None = None,
        event_country_code: str | None = None,
        event_postal_code: str | None = None,
        event_radius: float | None = None,
        event_radius_unit: str | None = None,
    ) -> CallToolResult:
        def action() -> dict[str, object]:
            store = LocalConfigStore()
            config = store.load()

            # Build updated config, keeping unchanged fields
            updated_config = LocalConfig(
                spotify_client_id=spotify_client_id if spotify_client_id is not None else config.spotify_client_id,
                event_country_code=event_country_code if event_country_code is not None else config.event_country_code,
                event_postal_code=event_postal_code if event_postal_code is not None else config.event_postal_code,
                event_radius=event_radius if event_radius is not None else config.event_radius,
                event_radius_unit=event_radius_unit if event_radius_unit is not None else config.event_radius_unit,
            )
            store.save(updated_config)

            return {
                "status": "updated",
                "spotify_client_id": updated_config.spotify_client_id is not None,
                "event_country_code": updated_config.event_country_code,
                "event_postal_code": updated_config.event_postal_code,
                "event_radius": updated_config.event_radius,
                "event_radius_unit": updated_config.event_radius_unit,
                "note": "To set the Ticketmaster key, run: music-friend setup --ticketmaster-key-env VAR_NAME",
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
    raise _InvalidArguments("kind must be one of: catalog, releases, events, all")


def _search_arguments(query: object, limit: object) -> tuple[str, int]:
    if type(query) is not str or not query.strip() or query != query.strip() or len(query) > 256:
        raise _InvalidArguments(
            "query must be a non-empty string of at most 256 characters with no "
            "leading or trailing whitespace"
        )
    return query, _limit(limit, maximum=50, field="limit")


def _limit(value: object, *, maximum: int, field: str = "limit") -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _InvalidArguments(f"{field} must be an integer from 1 through {maximum}")
    return value


def _local_id(value: object, *, field: str = "id") -> str:
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 4096:
        raise _InvalidArguments(
            f"{field} must be a non-empty string of at most 4096 characters with no "
            "leading or trailing whitespace"
        )
    return value


def _watchlist_action(value: object) -> WatchlistAction | Literal["remove"]:
    if type(value) is not str:
        raise _InvalidArguments("action must be a string")
    if value == "remove":
        return "remove"
    try:
        return WatchlistAction(value)
    except ValueError as error:
        raise _InvalidArguments("action must be one of: add, pin, mute, remove") from error


def _inbox_state(value: object, *, required: bool = False) -> InboxState | None:
    if value is None and not required:
        return None
    if type(value) is not str:
        raise _InvalidArguments("state must be a string")
    try:
        return InboxState(value)
    except ValueError as error:
        raise _InvalidArguments("state must be one of: unread, saved, dismissed") from error


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
    return since, until, _limit(limit, maximum=50, field="limit")  # type: ignore[return-value]


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


def _display(value: str) -> str:
    """Sanitize a display name at the MCP read boundary.

    Every write path already sanitizes display names at ingestion (see
    `music_friend.domain.text.sanitize_display_name` and its call sites in
    `providers.spotify.normalize`, `tools.event_discovery`, and
    `store.spotify_history`). This second pass at the MCP boundary is the
    read-time half of the ingestion fix: it cleans any row written before that
    ingestion sanitization existed, without a schema migration.
    """
    return sanitize_display_name(value, limit=4096)


def _optional_display(value: str | None) -> str | None:
    return None if value is None else _display(value)


def _artist(value: Artist) -> dict[str, object]:
    return {
        "local_id": value.local_id,
        "display_name": _display(value.display_name),
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


def _inbox(application: MusicFriendApplication, value: InboxEntry) -> dict[str, object]:
    return {
        "local_id": value.local_id,
        "state": value.state.value,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
        "summary": _inbox_summary(application, value),
    }


def _inbox_summary(
    application: MusicFriendApplication, value: InboxEntry
) -> dict[str, object] | None:
    """Compact kind/title/artist-names/date preview, so listing an inbox needs no follow-up call.

    Returns ``None`` when the entry's signal or underlying record is unexpectedly missing;
    ``local_id``/``state``/timestamps remain populated either way.
    """
    signal = application.get_signal(value.signal_local_id)
    if signal is None:
        return None
    if signal.kind is SignalKind.RELEASE:
        release = application.get_release(signal.record_local_id)
        if release is None:
            return None
        return {
            "kind": "release",
            "title": _display(release.title),
            "artist_names": _artist_names(application, release.artist_refs),
            "date": release.release_date.isoformat(),
        }
    event = application.get_event(signal.record_local_id)
    if event is None:
        return None
    return {
        "kind": "event",
        "title": _display(event.title),
        "artist_names": _artist_names(application, event.artist_refs),
        "date": None if event.starts_at is None else event.starts_at.isoformat(),
    }


def _artist_names(application: MusicFriendApplication, artist_ids: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for artist_id in artist_ids:
        artist = application.get_artist(artist_id)
        if artist is not None:
            names.append(_display(artist.display_name))
    return names


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
        "entry": _inbox(application, entry),
        "record": record,
        "reasons": [
            {"kind": reason.kind.value, "detail": reason.detail}
            for reason in signal.explanation.reasons
        ],
    }


def _signal_record(application: MusicFriendApplication, signal: Signal) -> dict[str, object] | None:
    if signal.kind is SignalKind.RELEASE:
        release = application.get_release(signal.record_local_id)
        return None if release is None else _release(application, release)
    event = application.get_event(signal.record_local_id)
    return None if event is None else _event(application, event)


def _release(application: MusicFriendApplication, value: Release) -> dict[str, object]:
    return {
        "kind": "release",
        "local_id": value.local_id,
        "title": _display(value.title),
        "release_type": value.release_type,
        "release_date": value.release_date.isoformat(),
        "date_precision": value.date_precision.value,
        "artist_ids": list(value.artist_refs),
        "artist_names": _artist_names(application, value.artist_refs),
    }


def _event(application: MusicFriendApplication, value: Event) -> dict[str, object]:
    return {
        "kind": "event",
        "local_id": value.local_id,
        "title": _display(value.title),
        "artist_ids": list(value.artist_refs),
        "artist_names": _artist_names(application, value.artist_refs),
        "venue_name": _optional_display(value.venue_name),
        "locality": _optional_display(value.locality),
        "starts_at": None if value.starts_at is None else value.starts_at.isoformat(),
        "time_precision": value.time_precision,
        "links": list(value.source_links),
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["create_music_server"]
