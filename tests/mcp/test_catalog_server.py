"""Contracts for the local catalog-backed MCP surface."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp.client import Client

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxEntry,
    InboxState,
    RefreshKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceReference,
)
from music_friend.mcp import catalog_server, create_music_server
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import RefreshInvocation

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _artist() -> Artist:
    return Artist(
        "artist-1",
        "Artist One",
        (SourceReference("synthetic", "provider-artist-1", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _application(tmp_path: Path) -> MusicFriendApplication:
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    application = MusicFriendApplication(catalog)
    artist = _artist()
    application.put_artist(artist)
    application.put_affinity_evidence(
        AffinityEvidence(
            "evidence-1",
            artist.local_id,
            "synthetic",
            AffinityEvidenceKind.FOLLOWED,
            "followed-1",
            None,
            NOW,
        )
    )
    release = Release(
        "release-1",
        "Release One",
        "album",
        NOW.date(),
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (SourceReference("synthetic", "provider-release-1", None, NOW),),
        NOW,
    )
    application.put_release(release)
    signal = Signal(
        "signal-1",
        SignalKind.RELEASE,
        release.local_id,
        "synthetic",
        "provider-release-1",
        "fingerprint-1",
        "material-1",
        Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, "Release One"),)),
        NOW,
    )
    application.put_signal(signal)
    application.put_inbox_entry(InboxEntry("inbox-1", signal.local_id, InboxState.UNREAD, NOW, NOW))
    return application


def _call(server: object, name: str, arguments: dict[str, object]) -> object:
    async def invoke() -> object:
        async with Client(server) as client:  # type: ignore[arg-type]
            return await client.call_tool(name, arguments)

    return asyncio.run(invoke()).structured_content  # type: ignore[union-attr]


def test_catalog_server_exposes_only_the_stable_local_tool_inventory(tmp_path: Path) -> None:
    """Catches re-exposure of a direct provider tool or an unbounded extra model action."""
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == [
        "music_status",
        "refresh_music",
        "search_catalog",
        "list_watchlist",
        "update_watchlist",
        "list_inbox",
        "update_inbox_item",
        "explain_inbox_item",
        "summarize_listening_history",
        "get_setup",
        "update_setup",
    ]

    async def listed_schemas() -> dict[str, dict[str, object]]:
        async with Client(server) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}

    schemas = asyncio.run(listed_schemas())
    rendered = json.dumps(schemas, sort_keys=True)
    for forbidden in ("spotify", "native_id", "source_refs", "credential", "path"):
        assert forbidden not in rendered
    assert schemas["search_catalog"]["properties"]["limit"] == {
        "description": "Maximum number of matching artists to return (1-50).",
        "minimum": 1,
        "maximum": 50,
        "type": "integer",
    }
    assert schemas["list_inbox"]["properties"]["limit"] == {
        "description": "Maximum number of inbox entries to return (1-100).",
        "minimum": 1,
        "maximum": 100,
        "type": "integer",
    }
    application.close()


def test_every_tool_description_is_rich_and_every_identifier_argument_names_its_source(
    tmp_path: Path,
) -> None:
    """Issue #23 contract: descriptions carry enough for an agent to sequence tools correctly.

    Every tool description must be substantially longer than the old one-liners
    and must say whether the tool contacts a provider. Every local-identifier
    argument (an `artist_id` or `inbox_id`-shaped property using the shared
    local-id schema) must carry a `description` that names the tool that
    produces the value it expects.
    """
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )
    tools = asyncio.run(server.list_tools())

    non_trivial_minimum_length = 120
    for tool in tools:
        description = tool.description
        assert description is not None
        assert len(description) >= non_trivial_minimum_length, tool.name
        lowered = description.lower()
        if tool.name == "refresh_music":
            assert "contacts a provider" in lowered or "only tool" in lowered, tool.name
        else:
            assert "does not contact a provider" in lowered, tool.name

    async def listed_schemas() -> dict[str, dict[str, object]]:
        async with Client(server) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}

    schemas = asyncio.run(listed_schemas())
    identifier_arguments = {
        ("update_watchlist", "artist_id"): "search_catalog",
        ("update_inbox_item", "inbox_id"): "list_inbox",
        ("explain_inbox_item", "inbox_id"): "list_inbox",
    }
    for (tool_name, argument_name), source_tool in identifier_arguments.items():
        argument_schema = schemas[tool_name]["properties"][argument_name]
        assert isinstance(argument_schema, dict)
        argument_description = argument_schema.get("description")
        assert isinstance(argument_description, str)
        assert source_tool in argument_description, (tool_name, argument_name)
    application.close()


def test_mcp_read_boundary_sanitizes_display_names_written_before_the_ingestion_fix(
    tmp_path: Path,
) -> None:
    """Issue #24, read-time half of the fix: legacy rows written before ingestion

    sanitization existed (see `music_friend.domain.text.sanitize_display_name` and
    its call sites) must still come back clean from every MCP tool that surfaces a
    display name, without a schema migration.
    """
    application = _application(tmp_path)
    legacy_artist = Artist(
        "artist-legacy",
        "Legacy‮ Artist",
        (SourceReference("synthetic", "provider-artist-legacy", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    application.put_artist(legacy_artist)
    legacy_release = Release(
        "release-legacy",
        "Legacy⁦ Release⁩",
        "album",
        NOW.date(),
        ReleaseDatePrecision.DAY,
        (legacy_artist.local_id,),
        (SourceReference("synthetic", "provider-release-legacy", None, NOW),),
        NOW,
    )
    application.put_release(legacy_release)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    search_result = _call(server, "search_catalog", {"query": "Legacy", "limit": 5})
    assert isinstance(search_result, dict)
    matched = next(item for item in search_result["items"] if item["local_id"] == "artist-legacy")
    assert matched["display_name"] == "Legacy Artist"
    assert "‮" not in matched["display_name"]
    application.close()


def test_listening_history_summary_names_its_evidence_boundary(tmp_path: Path) -> None:
    application = _application(tmp_path)
    connection = application._catalog._require_connection()
    connection.execute(
        """INSERT INTO listening_history (
            event_id, source, played_at, milliseconds_played, track_uri, track_name,
            artist_name, album_name, archive_digest, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "event-1",
            "spotify-history",
            "2026-01-02T03:04:05Z",
            123000,
            "spotify:track:one",
            "Track One",
            "Artist One",
            "Album One",
            "digest",
            "2026-09-04T00:00:00Z",
        ),
    )
    server = create_music_server(application, refresh=lambda _kind: object(), now=lambda: NOW)

    result = _call(
        server,
        "summarize_listening_history",
        {"since": "2026-01-01T00:00:00Z", "until": "2027-01-01T00:00:00Z", "limit": 5},
    )

    assert result["evidence_boundary"] == "imported Spotify music history"
    assert result["play_count"] == 1
    assert result["top_artists"] == [
        {"name": "Artist One", "play_count": 1, "milliseconds_played": 123000}
    ]
    application.close()


def test_listening_history_summary_accepts_rfc_3339_offsets_and_rejects_naive_timestamps(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    server = create_music_server(application, refresh=lambda _kind: object(), now=lambda: NOW)

    for since in (
        "2026-03-01T08:00:00Z",
        "2026-03-01T09:00:00+01:00",
        "2026-03-01T00:00:00-08:00",
    ):
        result = _call(
            server,
            "summarize_listening_history",
            {"since": since, "until": "2026-04-01T00:00:00Z", "limit": 5},
        )
        assert "category" not in result

    naive = _call(
        server,
        "summarize_listening_history",
        {"since": "2026-03-01T00:00:00", "until": None, "limit": 5},
    )
    assert naive["category"] == "invalid_arguments"
    assert "offset" in naive["message"] or "Z" in naive["message"]
    application.close()


def test_listening_history_summary_rejects_reversed_zero_length_and_impossible_ranges(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    server = create_music_server(application, refresh=lambda _kind: object(), now=lambda: NOW)

    reversed_range = _call(
        server,
        "summarize_listening_history",
        {"since": "2026-04-01T00:00:00Z", "until": "2026-03-01T00:00:00Z", "limit": 5},
    )
    zero_length = _call(
        server,
        "summarize_listening_history",
        {"since": "2026-03-01T00:00:00Z", "until": "2026-03-01T00:00:00Z", "limit": 5},
    )
    impossible_date = _call(
        server,
        "summarize_listening_history",
        {"since": "2026-02-30T00:00:00Z", "until": None, "limit": 5},
    )

    for result in (reversed_range, zero_length, impossible_date):
        assert result["category"] == "invalid_arguments"
        assert result["message"] != "Invalid tool arguments."
    application.close()


def test_invalid_argument_messages_name_the_field_and_constraint_without_echoing_the_value(
    tmp_path: Path,
) -> None:
    """Every `invalid_arguments` rejection must name the offending argument/constraint and
    never echo the caller-supplied value back (a canary secret-shaped value must never
    appear in the message)."""
    application = _application(tmp_path)
    server = create_music_server(application, refresh=lambda _kind: object(), now=lambda: NOW)
    canary = "supersecretcanary-value-should-never-appear"

    cases = [
        ("refresh_music", {"kind": canary}, "kind"),
        ("search_catalog", {"query": "", "limit": 5}, "query"),
        ("list_watchlist", {"limit": 0}, "limit"),
        ("update_watchlist", {"artist_id": canary, "action": "invalid-action"}, "action"),
        ("update_inbox_item", {"inbox_id": "x", "state": canary}, "state"),
        ("explain_inbox_item", {"inbox_id": ""}, "inbox_id"),
    ]
    for name, arguments, field in cases:
        result = _call(server, name, arguments)
        assert result["category"] == "invalid_arguments", name
        message = str(result["message"])
        assert message != "Invalid tool arguments.", name
        assert field in message, name
        assert canary not in message, name
    application.close()


def test_listening_history_summary_reports_internal_error_for_a_non_caller_valueerror(
    tmp_path: Path,
) -> None:
    """A `ValueError` that is not `HistoryArgumentError` (e.g. decoding a corrupt stored
    row) must not be misreported as the caller's mistake, and must not leak exception
    text to the client."""
    application = _application(tmp_path)
    server = create_music_server(application, refresh=lambda _kind: object(), now=lambda: NOW)

    def _raise_internal(**_kwargs: object) -> object:
        raise ValueError("supersecretcanary: corrupt row at offset 42")

    application.summarize_history = _raise_internal  # type: ignore[method-assign]

    result = _call(
        server,
        "summarize_listening_history",
        {"since": None, "until": None, "limit": 5},
    )

    assert result["category"] == "internal_error"
    assert "supersecretcanary" not in json.dumps(result)
    application.close()


def test_catalog_server_publishes_exact_tool_effect_annotations(tmp_path: Path) -> None:
    """Catches tool metadata under-reporting provider access or destructive local mutations."""
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    tools = asyncio.run(server.list_tools())

    assert {
        tool.name: tool.annotations.model_dump(by_alias=True)
        if tool.annotations is not None
        else None
        for tool in tools
    } == {
        "music_status": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "refresh_music": {
            "title": None,
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": True,
        },
        "search_catalog": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "list_watchlist": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "update_watchlist": {
            "title": None,
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "list_inbox": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "update_inbox_item": {
            "title": None,
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "explain_inbox_item": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "summarize_listening_history": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "get_setup": {
            "title": None,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
        "update_setup": {
            "title": None,
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": None,
            "openWorldHint": False,
        },
    }
    application.close()


def test_catalog_server_reads_updates_and_explains_local_records_without_provider_identifiers(
    tmp_path: Path,
) -> None:
    """Catches a model tool that bypasses local state, loses an update, or leaks source identity."""
    application = _application(tmp_path)
    calls: list[str] = []
    server = create_music_server(
        application,
        refresh=lambda kind: calls.append(kind) or {"status": "partial", "kind": kind},
        now=lambda: NOW,
    )

    assert _call(server, "music_status", {}) == {
        "inbox": {"has_unread": True},
        "latest_refresh": None,
        "status": "ready",
        "source_limits": {"spotify": {"ready": True, "state": "available", "retry_at": None}},
    }
    assert _call(server, "search_catalog", {"query": "Artist", "limit": 1}) == {
        "items": [
            {
                "display_name": "Artist One",
                "identity_confidence": "source_only",
                "local_id": "artist-1",
            }
        ]
    }
    assert _call(server, "update_watchlist", {"artist_id": "artist-1", "action": "pin"}) == {
        "artist_id": "artist-1",
        "action": "pin",
    }
    watchlist = _call(server, "list_watchlist", {"limit": 10})
    assert watchlist == {
        "items": [
            {
                "affinity": {"saved_track_count": 0, "total_points": 100},
                "artist": {
                    "display_name": "Artist One",
                    "identity_confidence": "source_only",
                    "local_id": "artist-1",
                },
                "inclusion_reason": "pinned",
            }
        ]
    }
    for kind in ("catalog", "releases", "events", "all"):
        assert _call(server, "refresh_music", {"kind": kind}) == {
            "kind": kind,
            "status": "partial",
        }
    assert calls == ["catalog", "releases", "events", "all"]
    inbox_summary = {
        "artist_names": ["Artist One"],
        "date": NOW.date().isoformat(),
        "kind": "release",
        "title": "Release One",
    }
    assert _call(server, "update_inbox_item", {"inbox_id": "inbox-1", "state": "saved"}) == {
        "created_at": NOW.isoformat(),
        "local_id": "inbox-1",
        "state": "saved",
        "summary": inbox_summary,
        "updated_at": NOW.isoformat(),
    }
    explanation = _call(server, "explain_inbox_item", {"inbox_id": "inbox-1"})
    assert explanation == {
        "entry": {
            "created_at": NOW.isoformat(),
            "local_id": "inbox-1",
            "state": "saved",
            "summary": inbox_summary,
            "updated_at": NOW.isoformat(),
        },
        "record": {
            "artist_ids": ["artist-1"],
            "artist_names": ["Artist One"],
            "date_precision": "day",
            "kind": "release",
            "local_id": "release-1",
            "release_date": NOW.date().isoformat(),
            "release_type": "album",
            "title": "Release One",
        },
        "reasons": [{"detail": "Release One", "kind": "new_release"}],
    }
    rendered = json.dumps(explanation, sort_keys=True)
    for forbidden in ("provider-release-1", "synthetic", "fingerprint-1", "material-1"):
        assert forbidden not in rendered
    application.close()


def test_catalog_server_redacts_invalid_or_failed_model_calls(tmp_path: Path) -> None:
    """Catches reflected tool arguments or local exception details in MCP responses."""
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: (_ for _ in ()).throw(RuntimeError("private path canary")),
        now=lambda: NOW,
    )

    invalid = _call(server, "search_catalog", {"query": " secret-canary ", "limit": 0})
    failed = _call(server, "refresh_music", {"kind": "catalog"})

    assert invalid["category"] == "invalid_arguments"
    assert invalid["message"] != "Invalid tool arguments."
    assert failed == {
        "category": "internal_error",
        "message": "Music Friend could not complete the request.",
    }
    assert "secret-canary" not in json.dumps([invalid, failed])
    assert "private path canary" not in json.dumps([invalid, failed])
    application.close()


def test_catalog_server_validates_construction_and_clock_boundaries(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with pytest.raises(ValueError, match="application and refresh callback are required"):
        create_music_server(object(), refresh=lambda _kind: object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="application and refresh callback are required"):
        create_music_server(application, refresh=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="now must be callable"):
        create_music_server(application, refresh=lambda _kind: object(), now=1)  # type: ignore[arg-type]

    server = create_music_server(
        application,
        refresh=lambda _kind: {"status": "succeeded"},
        now=lambda: datetime(2026, 9, 1),
    )
    result = _call(server, "update_watchlist", {"artist_id": "artist-1", "action": "add"})
    assert result == {
        "category": "internal_error",
        "message": "Music Friend could not complete the request.",
    }
    application.close()


def test_catalog_server_supports_every_watchlist_action_and_missing_artist(tmp_path: Path) -> None:
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    for action in ("add", "pin", "mute", "remove"):
        assert _call(server, "update_watchlist", {"artist_id": "artist-1", "action": action}) == {
            "artist_id": "artist-1",
            "action": action,
        }

    assert _call(server, "update_watchlist", {"artist_id": "missing", "action": "add"}) == {
        "category": "not_found",
        "message": "Music Friend record was not found.",
    }
    application.close()


def test_catalog_server_filters_inbox_and_reports_missing_updates_and_explanations(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    assert _call(server, "list_inbox", {"state": "saved", "limit": 10}) == {"items": []}
    assert _call(server, "update_inbox_item", {"inbox_id": "missing", "state": "saved"}) == {
        "category": "not_found",
        "message": "Music Friend record was not found.",
    }
    assert _call(server, "explain_inbox_item", {"inbox_id": "missing"}) == {
        "category": "not_found",
        "message": "Music Friend record was not found.",
    }
    application.close()


def test_list_inbox_returns_a_compact_summary_without_a_follow_up_call(tmp_path: Path) -> None:
    """AC #21: an agent must be able to summarize the inbox from list_inbox alone."""
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    result = _call(server, "list_inbox", {"state": "unread", "limit": 10})
    assert result == {
        "items": [
            {
                "local_id": "inbox-1",
                "state": "unread",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "summary": {
                    "kind": "release",
                    "title": "Release One",
                    "artist_names": ["Artist One"],
                    "date": NOW.date().isoformat(),
                },
            }
        ]
    }
    application.close()


@pytest.mark.parametrize("kind", ("catalog", "releases", "events", "all"))
def test_catalog_server_accepts_each_bounded_refresh_kind(tmp_path: Path, kind: str) -> None:
    application = _application(tmp_path)
    calls: list[str] = []
    server = create_music_server(
        application,
        refresh=lambda selected: (
            calls.append(selected) or {"kind": selected, "status": "succeeded"}
        ),
        now=lambda: NOW,
    )

    assert _call(server, "refresh_music", {"kind": kind}) == {
        "kind": kind,
        "status": "succeeded",
    }
    assert calls == [kind]
    application.close()


def test_catalog_server_reports_events_only_refresh_skipped_for_unconfigured_area(
    tmp_path: Path,
) -> None:
    """Catches an unconfigured event refresh reaching the MCP client as a misleading success."""
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: RefreshInvocation(
            None, False, skip_reason="event_area_not_configured"
        ),
        now=lambda: NOW,
    )

    assert _call(server, "refresh_music", {"kind": "events"}) == {
        "status": "skipped",
        "reason": "event_area_not_configured",
    }
    application.close()


def test_catalog_server_rejects_invalid_argument_shapes_before_application_work(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    server = create_music_server(
        application, refresh=lambda _kind: {"status": "succeeded"}, now=lambda: NOW
    )

    cases = (
        ("refresh_music", {"kind": "unknown"}),
        ("search_catalog", {"query": " padded ", "limit": 1}),
        ("search_catalog", {"query": "artist", "limit": True}),
        ("list_watchlist", {"limit": 101}),
        ("list_inbox", {"state": "unknown", "limit": 10}),
        ("update_watchlist", {"artist_id": " ", "action": "pin"}),
        ("update_watchlist", {"artist_id": "artist-1", "action": "unknown"}),
        ("update_inbox_item", {"inbox_id": "inbox-1", "state": None}),
        ("explain_inbox_item", {"inbox_id": "inbox-1", "unexpected": True}),
    )
    for tool, arguments in cases:
        result = _call(server, tool, arguments)
        assert result["category"] == "invalid_arguments", tool


def test_get_setup_reports_missing_fields_and_never_returns_the_client_id_value(
    tmp_path: Path,
) -> None:
    from music_friend.configuration import LocalConfigStore

    config_dir = tmp_path / "config"
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: {"status": "succeeded"},
        now=lambda: NOW,
        config_store_factory=lambda: LocalConfigStore(config_dir=config_dir),
    )

    result = _call(server, "get_setup", {})

    assert result["status"] == "incomplete"
    assert result["client_id"] is False
    assert result["event_country_code"] is None
    assert "client_id" in result["missing_fields"]
    assert "event_country_code" in result["missing_fields"]
    application.close()


def test_get_setup_reports_ready_once_every_nonsecret_field_is_set(tmp_path: Path) -> None:
    from music_friend.configuration import LocalConfig, LocalConfigStore

    config_dir = tmp_path / "config"
    store = LocalConfigStore(config_dir=config_dir)
    store.save(
        LocalConfig(
            spotify_client_id="example-client-id",
            event_country_code="US",
            event_postal_code="94110",
            event_radius=25,
            event_radius_unit="miles",
        )
    )
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: {"status": "succeeded"},
        now=lambda: NOW,
        config_store_factory=lambda: LocalConfigStore(config_dir=config_dir),
    )

    result = _call(server, "get_setup", {})

    assert result["status"] == "ready"
    assert result["client_id"] is True
    assert result["missing_fields"] == []
    application.close()


def test_update_setup_persists_nonsecret_fields_and_never_returns_the_client_id_value(
    tmp_path: Path,
) -> None:
    from music_friend.configuration import LocalConfigStore

    config_dir = tmp_path / "config"
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: {"status": "succeeded"},
        now=lambda: NOW,
        config_store_factory=lambda: LocalConfigStore(config_dir=config_dir),
    )

    result = _call(
        server,
        "update_setup",
        {
            "client_id": "example-client-id",
            "event_country_code": "US",
            "event_postal_code": "94110",
            "event_radius": 25,
            "event_radius_unit": "miles",
        },
    )

    assert result["status"] == "updated"
    assert result["client_id"] is True
    assert result["event_country_code"] == "US"
    assert "example-client-id" not in json.dumps(result)
    assert "music-friend setup --ticketmaster-key-env" in result["note"]

    persisted = LocalConfigStore(config_dir=config_dir).load()
    assert persisted.spotify_client_id == "example-client-id"
    application.close()


def test_update_setup_leaves_unspecified_fields_unchanged(tmp_path: Path) -> None:
    from music_friend.configuration import LocalConfig, LocalConfigStore

    config_dir = tmp_path / "config"
    store = LocalConfigStore(config_dir=config_dir)
    store.save(LocalConfig(spotify_client_id="already-set-id", event_country_code="US"))
    application = _application(tmp_path)
    server = create_music_server(
        application,
        refresh=lambda _kind: {"status": "succeeded"},
        now=lambda: NOW,
        config_store_factory=lambda: LocalConfigStore(config_dir=config_dir),
    )

    result = _call(server, "update_setup", {"event_postal_code": "94110"})

    assert result["event_country_code"] == "US"
    assert result["event_postal_code"] == "94110"
    persisted = LocalConfigStore(config_dir=config_dir).load()
    assert persisted.spotify_client_id == "already-set-id"
    application.close()


def test_create_music_server_rejects_a_non_callable_config_store_factory(tmp_path: Path) -> None:
    application = _application(tmp_path)
    with pytest.raises(ValueError, match="config_store_factory"):
        create_music_server(
            application,
            refresh=lambda _kind: {"status": "succeeded"},
            config_store_factory="not-callable",  # type: ignore[arg-type]
        )
    application.close()
    application.close()


def test_refresh_result_carries_reason_retry_after_and_remaining() -> None:
    """Catches the MCP refresh result dropping AC6's partial-outcome detail."""
    run = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.RELEASES,
        RefreshStatus.PARTIAL,
        NOW,
        NOW,
        RefreshSummary(()),
    )
    invocation = RefreshInvocation(
        run,
        already_running=False,
        reason="quota_exhausted",
        retry_after=None,
        remaining=2,
    )

    payload = catalog_server._refresh_result(invocation)

    assert payload["reason"] == "quota_exhausted"
    assert payload["remaining"] == 2
    assert "retry_after" not in payload
