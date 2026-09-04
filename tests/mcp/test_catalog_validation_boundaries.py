"""Closed argument and result-shape tests for the catalog MCP surface."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from music_friend.mcp import catalog_server


@pytest.mark.parametrize(
    "params",
    (
        {"name": "refresh_music", "arguments": {"kind": "wrong"}},
        {"name": "search_catalog", "arguments": {"query": " spaced ", "limit": 1}},
        {"name": "search_catalog", "arguments": {"query": "valid", "limit": 0}},
        {"name": "list_watchlist", "arguments": {"limit": 101}},
        {"name": "list_inbox", "arguments": {"limit": 1, "state": "wrong"}},
        {"name": "update_watchlist", "arguments": {"artist_id": "", "action": "pin"}},
        {"name": "update_watchlist", "arguments": {"artist_id": "id", "action": "wrong"}},
        {"name": "update_inbox_item", "arguments": {"inbox_id": "id", "state": None}},
        {"name": "explain_inbox_item", "arguments": {"inbox_id": "id", "extra": 1}},
        {"name": "refresh_music", "arguments": []},
    ),
)
def test_tool_contract_rejects_invalid_argument_shapes(params: object) -> None:
    assert catalog_server._has_valid_tool_arguments(params) is False  # type: ignore[arg-type]


def test_tool_contract_ignores_unknown_protocol_messages_and_names() -> None:
    assert catalog_server._has_valid_tool_arguments(None) is True
    assert catalog_server._has_valid_tool_arguments({"name": "unknown"}) is True
    assert catalog_server._with_fixed_input_schema(1) == 1
    assert catalog_server._with_fixed_input_schema({"name": "unknown", "extra": 1}) == {
        "name": "unknown",
        "extra": 1,
    }


@pytest.mark.parametrize("value", (None, 1, "bad"))
def test_inbox_state_rejects_required_or_invalid_values(value: object) -> None:
    with pytest.raises(catalog_server._InvalidArguments):
        catalog_server._inbox_state(value, required=True)


@pytest.mark.parametrize("value", (None, 1, " x", "x ", "x" * 4097))
def test_local_id_rejects_unbounded_or_noncanonical_values(value: object) -> None:
    with pytest.raises(catalog_server._InvalidArguments):
        catalog_server._local_id(value)


def test_clock_and_refresh_result_reject_invalid_callback_results() -> None:
    with pytest.raises(ValueError):
        catalog_server._now(lambda: datetime(2026, 9, 2))
    with pytest.raises(ValueError):
        catalog_server._refresh_result({"status": 1})
    with pytest.raises(ValueError):
        catalog_server._refresh_result(SimpleNamespace(already_running=False, run=object()))
    assert catalog_server._refresh_result(SimpleNamespace(already_running=True, run=None)) == {
        "status": "partial"
    }
    assert (
        catalog_server._now(lambda: datetime(2026, 9, 2, tzinfo=timezone.utc)).tzinfo
        is timezone.utc
    )


def test_safe_call_maps_argument_and_internal_failures_without_details() -> None:
    invalid = catalog_server._safe_call(
        lambda: (_ for _ in ()).throw(catalog_server._InvalidArguments())
    )
    internal = catalog_server._safe_call(lambda: (_ for _ in ()).throw(RuntimeError("private")))
    assert invalid.is_error is True
    assert invalid.structured_content == {
        "category": "invalid_arguments",
        "message": "Invalid tool arguments.",
    }
    assert internal.is_error is True
    assert "private" not in internal.content[0].text
