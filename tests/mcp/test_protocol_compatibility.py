"""MCP 2.1.1 compatibility tests using only synthetic MusicSource implementations."""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)

FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_CANARY_SECRET = "canary-secret-value"
_CANARY_PATH = "/synthetic/canary-path"
_CLEAN_ROOM_BOUNDARY_ACTIVE = (
    os.environ.get("MF_CLEAN_ROOM_ACTIVE") == "1" or "clean_room.boundaries" in sys.modules
)


def _reference(native_id: str = "artist-1") -> SourceReference:
    return SourceReference("synthetic", native_id, "https://example.test/artist", FIXED_TIME)


class _SyntheticSource:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error
        self.capabilities_value = ProviderCapabilities(
            supported=frozenset(Capability), granted=frozenset(Capability)
        )

    def _result(self, value: object) -> object:
        if self.error is not None:
            raise self.error
        return value

    def capabilities(self) -> ProviderCapabilities:
        self.calls.append("music_capabilities")
        return self._result(self.capabilities_value)  # type: ignore[return-value]

    def health(self) -> ProviderHealth:
        self.calls.append("music_health")
        return self._result(ProviderHealth(HealthStatus.HEALTHY, self.capabilities_value))  # type: ignore[return-value]

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        self.calls.append("search_artists")
        return self._result(
            Page(
                (
                    Artist(
                        "artist-1",
                        "Artist One",
                        (_reference(),),
                        IdentityConfidence.EXTERNAL_ID,
                        FIXED_TIME,
                    ),
                ),
                "artists-next",
            )
        )  # type: ignore[return-value]

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        self.calls.append("followed_artists")
        return self._result(
            Page(
                (
                    Artist(
                        "artist-1",
                        "Artist One",
                        (_reference(),),
                        IdentityConfidence.EXTERNAL_ID,
                        FIXED_TIME,
                    ),
                ),
                "artists-next",
            )
        )  # type: ignore[return-value]

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        self.calls.append("saved_items")
        return self._result(
            CatalogItemBatch(
                (
                    CatalogItem(
                        "track",
                        "item-1",
                        "Item One",
                        ("artist-1",),
                        (_reference("item-1"),),
                        FIXED_TIME,
                    ),
                ),
                (
                    Artist(
                        "artist-1",
                        "Artist One",
                        (_reference(),),
                        IdentityConfidence.EXTERNAL_ID,
                        FIXED_TIME,
                    ),
                ),
                "items-next",
            )
        )  # type: ignore[return-value]

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        self.calls.append("top_items")
        return self._result(
            CatalogItemBatch(
                (
                    CatalogItem(
                        "track",
                        "item-1",
                        "Item One",
                        ("artist-1",),
                        (_reference("item-1"),),
                        FIXED_TIME,
                    ),
                ),
                (
                    Artist(
                        "artist-1",
                        "Artist One",
                        (_reference(),),
                        IdentityConfidence.EXTERNAL_ID,
                        FIXED_TIME,
                    ),
                ),
                "items-next",
            )
        )  # type: ignore[return-value]

    def recent_releases(
        self, artist_refs: tuple[SourceReference, ...], since: datetime
    ) -> Page[Release]:
        self.calls.append("recent_releases")
        return self._result(
            Page(
                (
                    Release(
                        "release-1",
                        "Release One",
                        "album",
                        date(2026, 1, 1),
                        ReleaseDatePrecision.DAY,
                        ("artist-1",),
                        (_reference("release-1"),),
                        FIXED_TIME,
                    ),
                ),
                None,
            )
        )  # type: ignore[return-value]


async def _in_memory_protocol_results(source: _SyntheticSource) -> dict[str, object]:
    from mcp.client import Client

    server = _create_read_server(source)
    async with Client(server, mode="legacy") as client:
        listed = await client.list_tools()
        return {
            "tools": [tool.name for tool in listed.tools],
            "music_capabilities": (
                await client.call_tool("music_capabilities", {})
            ).structured_content,
            "music_health": (await client.call_tool("music_health", {})).structured_content,
            "search_artists": (
                await client.call_tool("search_artists", {"query": "artist", "limit": 1})
            ).structured_content,
            "followed_artists": (
                await client.call_tool("followed_artists", {"cursor": None})
            ).structured_content,
            "saved_items": (
                await client.call_tool("saved_items", {"cursor": None})
            ).structured_content,
            "top_items": (
                await client.call_tool("top_items", {"time_range": "medium_term", "limit": 1})
            ).structured_content,
            "recent_releases": (
                await client.call_tool(
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
                        "since": "2026-01-01T00:00:00Z",
                    },
                )
            ).structured_content,
        }


def _create_read_server(source: _SyntheticSource) -> object:
    from music_friend.mcp.read_server import create_read_server

    return create_read_server(source)


@pytest.mark.skipif(
    _CLEAN_ROOM_BOUNDARY_ACTIVE,
    reason="the artifact clean-room command owns MCP imports while its subprocess boundary is active",
)
def test_mcp_211_in_memory_client_discovers_and_calls_every_synthetic_tool() -> None:
    """Removing a tool or breaking its MCP dispatch fails this protocol-level contract."""
    source = _SyntheticSource()

    results = asyncio.run(_in_memory_protocol_results(source))

    assert results["tools"] == [
        "music_capabilities",
        "music_health",
        "search_artists",
        "followed_artists",
        "saved_items",
        "top_items",
        "recent_releases",
    ]
    assert results["music_capabilities"] == {
        "supported": sorted(capability.value for capability in Capability),
        "granted": sorted(capability.value for capability in Capability),
        "effective": sorted(capability.value for capability in Capability),
    }
    assert results["music_health"] == {
        "status": "healthy",
        "capabilities": results["music_capabilities"],
    }
    for name, cursor in (
        ("search_artists", "artists-next"),
        ("followed_artists", "artists-next"),
        ("saved_items", "items-next"),
        ("top_items", "items-next"),
        ("recent_releases", None),
    ):
        result = results[name]
        assert isinstance(result, dict)
        assert result["next_cursor"] == cursor
        assert isinstance(result["items"], list) and len(result["items"]) == 1
    assert source.calls == [
        "music_capabilities",
        "music_health",
        "search_artists",
        "followed_artists",
        "saved_items",
        "top_items",
        "recent_releases",
    ]


@pytest.mark.skipif(
    _CLEAN_ROOM_BOUNDARY_ACTIVE,
    reason="the artifact clean-room command owns MCP imports while its subprocess boundary is active",
)
def test_mcp_protocol_hides_malicious_arguments_and_unexpected_source_errors() -> None:
    """Echoing an argument or exception would expose a secret, path, traceback, or provider detail."""
    canary = f"{_CANARY_SECRET} {_CANARY_PATH} Spotify traceback"

    async def invoke() -> tuple[object, object]:
        from mcp.client import Client

        async with Client(
            _create_read_server(_SyntheticSource(RuntimeError(canary))), mode="legacy"
        ) as client:
            invalid = await client.call_tool("search_artists", {"query": canary, "limit": 0})
            failed = await client.call_tool("music_capabilities", {})
            return invalid.structured_content, failed.structured_content

    invalid, failed = asyncio.run(invoke())
    rendered = json.dumps([invalid, failed], sort_keys=True)

    assert invalid == {"category": "invalid_arguments", "message": "Invalid tool arguments."}
    assert failed == {
        "category": "internal_error",
        "message": "Music Friend could not complete the request.",
    }
    for forbidden in (_CANARY_SECRET, _CANARY_PATH, "Spotify", "traceback"):
        assert forbidden not in rendered


def _synthetic_stdio_server() -> list[str]:
    program = """
from music_friend.mcp.read_server import create_read_server
from music_friend.providers import Capability, ProviderCapabilities

class Source:
    def capabilities(self):
        return ProviderCapabilities(supported=frozenset(Capability), granted=frozenset(Capability))

create_read_server(Source()).run(\"stdio\")
"""
    return [sys.executable, "-c", program]


def _assert_protocol_only_stdout(stdout: str) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("stdio emitted non-protocol stdout") from error
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("stdio emitted non-protocol stdout")
        messages.append(message)
    if not messages:
        raise ValueError("stdio emitted no protocol output")
    return messages


def _write_request(process: subprocess.Popen[str], request: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_protocol_response(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=5):
            raise TimeoutError("MCP stdio response exceeded the fixed timeout")
    line = process.stdout.readline()
    if not line:
        raise ValueError("MCP stdio closed before responding")
    return line


@contextmanager
def _stdio_process() -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        _synthetic_stdio_server(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path.cwd(),
    )
    try:
        yield process
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def test_mcp_protocol_positive_control_rejects_non_protocol_stdout() -> None:
    """The clean-room positive control proves the protocol-output assertion is not vacuous."""
    with pytest.raises(ValueError, match="non-protocol stdout"):
        _assert_protocol_only_stdout("diagnostic output\\n")


@pytest.mark.skipif(
    _CLEAN_ROOM_BOUNDARY_ACTIVE,
    reason="the artifact clean-room command owns the isolated subprocess protocol gate",
)
def test_mcp_211_stdio_subprocess_initializes_discovers_calls_and_exits_cleanly() -> None:
    """A non-stdio transport, noisy stdout, or stuck session breaks this synthetic subprocess smoke test."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-test", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "music_capabilities", "arguments": {}},
        },
    ]
    with _stdio_process() as process:
        _write_request(process, requests[0])
        initialize = _read_protocol_response(process)
        _write_request(process, requests[1])
        _write_request(process, requests[2])
        tools = _read_protocol_response(process)
        _write_request(process, requests[3])
        called = _read_protocol_response(process)
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=5)
        assert process.stdout is not None and process.stderr is not None
        stdout = initialize + tools + called + process.stdout.read()
        stderr = process.stderr.read()

    messages = _assert_protocol_only_stdout(stdout)

    assert process.returncode == 0
    assert stderr == ""
    assert [message.get("id") for message in messages if "id" in message] == [1, 2, 3]
    assert messages[1]["result"]["tools"][0]["name"] == "music_capabilities"
    assert messages[2]["result"]["structuredContent"] == {
        "supported": sorted(capability.value for capability in Capability),
        "granted": sorted(capability.value for capability in Capability),
        "effective": sorted(capability.value for capability in Capability),
    }
