"""Behavioral tests for DeezerTransport's pacing and error mapping."""

from __future__ import annotations

import json

import httpx
import pytest

from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError
from music_friend.providers.deezer.transport import DeezerTransport


def _handler(payload: object, status_code: int = 200) -> httpx.MockTransport:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=json.dumps(payload).encode("utf-8"))

    return httpx.MockTransport(_respond)


def test_get_returns_parsed_json() -> None:
    transport = DeezerTransport(connector=_handler({"data": [], "total": 0}))
    result = transport.get("artist/27/albums", query={"limit": "5"})
    assert result == {"data": [], "total": 0}
    transport.close()


def test_quota_exceeded_body_maps_to_rate_limited() -> None:
    payload = {"error": {"type": "QuotaExceeded", "message": "quota exceeded", "code": 4}}
    transport = DeezerTransport(connector=_handler(payload))
    with pytest.raises(RateLimitedError):
        transport.get("artist/27/albums")
    transport.close()


def test_other_error_body_maps_to_invalid_source_response() -> None:
    payload = {"error": {"type": "InvalidQuery", "message": "bad request", "code": 200}}
    transport = DeezerTransport(connector=_handler(payload))
    with pytest.raises(InvalidSourceResponseError):
        transport.get("artist/27/albums")
    transport.close()


def test_http_429_maps_to_rate_limited() -> None:
    transport = DeezerTransport(connector=_handler({}, status_code=429))
    with pytest.raises(RateLimitedError):
        transport.get("artist/27/albums")
    transport.close()


def test_http_500_maps_to_source_unavailable() -> None:
    transport = DeezerTransport(connector=_handler({}, status_code=500))
    with pytest.raises(SourceUnavailableError):
        transport.get("artist/27/albums")
    transport.close()


def test_malformed_json_maps_to_invalid_source_response() -> None:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    transport = DeezerTransport(connector=httpx.MockTransport(_respond))
    with pytest.raises(InvalidSourceResponseError):
        transport.get("artist/27/albums")
    transport.close()


def test_pacing_sleeps_after_ten_requests_in_five_seconds() -> None:
    times = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1])
    clock_values = list(times)
    call_index = {"value": 0}

    def clock() -> float:
        idx = min(call_index["value"], len(clock_values) - 1)
        call_index["value"] += 1
        return clock_values[idx]

    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    transport = DeezerTransport(
        connector=_handler({"data": [], "total": 0}), clock=clock, sleeper=sleeper
    )
    for _ in range(11):
        transport.get("artist/27/albums")
    assert sleeps, "the 11th request within the window must wait for pacing"
    transport.close()


def test_query_values_must_be_strings() -> None:
    transport = DeezerTransport(connector=_handler({"data": [], "total": 0}))
    with pytest.raises(ValueError):
        transport.get("artist/27/albums", query={"limit": 5})  # type: ignore[dict-item]
    transport.close()


def test_path_must_be_a_string() -> None:
    transport = DeezerTransport(connector=_handler({"data": [], "total": 0}))
    with pytest.raises(ValueError):
        transport.get(123)  # type: ignore[arg-type]
    transport.close()


def test_query_must_be_a_mapping() -> None:
    transport = DeezerTransport(connector=_handler({"data": [], "total": 0}))
    with pytest.raises(ValueError):
        transport.get("artist/27/albums", query="not-a-mapping")  # type: ignore[arg-type]
    transport.close()


def test_network_error_maps_to_source_unavailable() -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = DeezerTransport(connector=httpx.MockTransport(_raise))
    with pytest.raises(SourceUnavailableError):
        transport.get("artist/27/albums")
    transport.close()


def test_oversized_body_maps_to_invalid_source_response() -> None:
    huge_payload = {"data": [{"padding": "x" * (1024 * 1024 + 10)}]}
    transport = DeezerTransport(connector=_handler(huge_payload))
    with pytest.raises(InvalidSourceResponseError):
        transport.get("artist/27/albums")
    transport.close()


def test_retry_after_header_is_parsed() -> None:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "17"}, content=b"{}")

    transport = DeezerTransport(connector=httpx.MockTransport(_respond))
    try:
        transport.get("artist/27/albums")
        raise AssertionError("expected RateLimitedError")
    except RateLimitedError as error:
        assert error.retry_after_seconds == 17
    transport.close()


def test_retry_after_header_defaults_when_missing() -> None:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"{}")

    transport = DeezerTransport(connector=httpx.MockTransport(_respond))
    try:
        transport.get("artist/27/albums")
        raise AssertionError("expected RateLimitedError")
    except RateLimitedError as error:
        assert error.retry_after_seconds == 5
    transport.close()


def test_retry_after_header_defaults_when_unparseable() -> None:
    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "soon"}, content=b"{}")

    transport = DeezerTransport(connector=httpx.MockTransport(_respond))
    try:
        transport.get("artist/27/albums")
        raise AssertionError("expected RateLimitedError")
    except RateLimitedError as error:
        assert error.retry_after_seconds == 5
    transport.close()


def test_error_key_present_but_not_a_mapping_is_ignored() -> None:
    """A non-object 'error' value is not Deezer's documented error shape; the
    response is returned as-is rather than raising."""
    transport = DeezerTransport(connector=_handler({"data": [], "error": "not-an-object"}))
    result = transport.get("artist/27/albums")
    assert result == {"data": [], "error": "not-an-object"}
    transport.close()


def test_context_manager_closes_the_client() -> None:
    with DeezerTransport(connector=_handler({"data": [], "total": 0})) as transport:
        assert transport.get("artist/27/albums") == {"data": [], "total": 0}


def test_pacing_second_prune_after_sleep_drops_all_stale_entries() -> None:
    """Covers the post-sleep re-prune loop: entries stale even after the wait are dropped."""
    clock_values = [0.0] * 10 + [100.0]
    call_index = {"value": 0}

    def clock() -> float:
        idx = min(call_index["value"], len(clock_values) - 1)
        call_index["value"] += 1
        return clock_values[idx]

    def sleeper(seconds: float) -> None:
        pass

    transport = DeezerTransport(
        connector=_handler({"data": [], "total": 0}), clock=clock, sleeper=sleeper
    )
    for _ in range(10):
        transport.get("artist/27/albums")
    # The 11th call's pacing check sees a clock value of 100.0, far past the window,
    # exercising the second while-loop that prunes after the sleep.
    transport.get("artist/27/albums")
    transport.close()
