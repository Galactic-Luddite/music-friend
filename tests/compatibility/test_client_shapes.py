"""Provider-neutral local client configuration and tool-loop fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.client import Client

from music_friend.mcp import create_music_server
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication

from .openai_tool_loop import run_synthetic_tool_loop

ROOT = Path(__file__).parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
EXPECTED_TOOLS = {
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
}


def test_codex_and_claude_stdio_fixture_shapes_select_the_same_local_command() -> None:
    """Both documented client shapes point at the installed stdio server, not a provider endpoint."""
    codex = (FIXTURES / "codex-mcp.toml").read_text(encoding="utf-8")
    claude = json.loads((FIXTURES / "claude-mcp.json").read_text(encoding="utf-8"))

    assert "[mcp_servers.music-friend]" in codex
    assert 'command = "music-friend-mcp"' in codex
    assert claude == {"mcpServers": {"music-friend": {"command": "music-friend-mcp", "args": []}}}


def test_openai_compatible_loop_translates_mcp_schema_and_synthetic_result(tmp_path: Path) -> None:
    """The non-shipping harness proves bridge data shape, not model quality or native MCP support."""
    application = MusicFriendApplication(Catalog.open(tmp_path / "catalog.sqlite3"))
    server = create_music_server(application, refresh=lambda _kind: {"status": "succeeded"})

    async def listed() -> object:
        async with Client(server) as client:
            return await client.list_tools()

    listed_tools = asyncio.run(listed()).tools  # type: ignore[union-attr]
    result = asyncio.run(
        run_synthetic_tool_loop(
            server,
            listed_tools,
            {"name": "music_status", "arguments": "{}"},
        )
    )

    assert {tool["function"]["name"] for tool in result["tools"]} == EXPECTED_TOOLS
    assert result["tool_result"] == {
        "inbox": {"has_unread": False},
        "latest_refresh": None,
        "status": "ready",
    }
    application.close()
