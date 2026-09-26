"""Direct transport tests: headers, pacing, and HTTP error mapping."""

from __future__ import annotations

import httpx
import pytest

from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError
from music_friend.providers.musicbrainz.transport import MusicBrainzTransport

USER_AGENT = "music-friend/0.1.0 (https://github.com/Galactic-Luddite/music-friend)"


def _transport(handler: object, **kwargs: object) -> MusicBrainzTransport:
    return MusicBrainzTransport(
        user_agent=USER_AGENT,
        connector=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_rejects_a_blank_user_agent() -> None:
    with pytest.raises(ValueError):
        MusicBrainzTransport(user_agent="   ")


def test_sends_the_required_user_agent_and_fmt_json_and_no_credentials() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = _transport(handler)
    result = transport.get("artist", query={"query": 'artist:"Synthetic"'})

    assert result == {"ok": True}
    assert len(captured) == 1
    request = captured[0]
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["accept"] == "application/json"
    assert request.url.params.get("fmt") == "json"
    assert request.url.params.get("query") == 'artist:"Synthetic"'
    assert "Authorization" not in request.headers


def test_batched_list_query_values_send_one_repeated_parameter() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"url-list": []})

    transport = _transport(handler)
    transport.get("url", query={"resource": ["u1", "u2", "u3"], "inc": "artist-rels"})

    resources = captured[0].url.params.get_list("resource")
    assert resources == ["u1", "u2", "u3"]


def test_rejects_a_non_string_non_list_query_value() -> None:
    transport = _transport(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        transport.get("artist", query={"limit": 5})  # type: ignore[dict-item]


def test_pacing_sleeps_when_the_next_call_is_too_soon() -> None:
    # get() reads the clock once after the first call (recording pacing state) and
    # twice around the second call (elapsed check, then recording pacing state again).
    clock_values = iter([0.0, 0.2, 0.2])
    sleeps: list[float] = []
    transport = _transport(
        lambda request: httpx.Response(200, json={}),
        clock=lambda: next(clock_values),
        sleeper=sleeps.append,
    )
    transport.get("artist")
    transport.get("artist")
    assert sleeps == [pytest.approx(0.8)]


def test_pacing_does_not_sleep_when_enough_time_has_elapsed() -> None:
    clock_values = iter([0.0, 5.0, 5.0])
    sleeps: list[float] = []
    transport = _transport(
        lambda request: httpx.Response(200, json={}),
        clock=lambda: next(clock_values),
        sleeper=sleeps.append,
    )
    transport.get("artist")
    transport.get("artist")
    assert sleeps == []


def test_429_maps_to_rate_limited_with_the_retry_after_header() -> None:
    transport = _transport(
        lambda request: httpx.Response(429, headers={"Retry-After": "17"}, json={})
    )
    with pytest.raises(RateLimitedError) as excinfo:
        transport.get("artist")
    assert excinfo.value.retry_after_seconds == 17


def test_503_without_a_retry_after_header_defaults_to_five_seconds() -> None:
    transport = _transport(lambda request: httpx.Response(503, json={}))
    with pytest.raises(RateLimitedError) as excinfo:
        transport.get("artist")
    assert excinfo.value.retry_after_seconds == 5


def test_malformed_retry_after_header_defaults_to_five_seconds() -> None:
    transport = _transport(
        lambda request: httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={})
    )
    with pytest.raises(RateLimitedError) as excinfo:
        transport.get("artist")
    assert excinfo.value.retry_after_seconds == 5


def test_other_non_200_status_maps_to_source_unavailable() -> None:
    transport = _transport(lambda request: httpx.Response(404))
    with pytest.raises(SourceUnavailableError):
        transport.get("artist")


def test_oversized_body_maps_to_invalid_source_response() -> None:
    oversized = b"[" + b"1," * (1024 * 1024) + b"1]"
    transport = _transport(lambda request: httpx.Response(200, content=oversized))
    with pytest.raises(InvalidSourceResponseError):
        transport.get("artist")


def test_malformed_json_maps_to_invalid_source_response() -> None:
    transport = _transport(lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(InvalidSourceResponseError):
        transport.get("artist")


def test_a_connection_error_maps_to_source_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic network failure", request=request)

    transport = _transport(handler)
    with pytest.raises(SourceUnavailableError):
        transport.get("artist")


def test_close_and_context_manager_close_the_injected_connector() -> None:
    transport = _transport(lambda request: httpx.Response(200, json={}))
    transport.close()
    with _transport(lambda request: httpx.Response(200, json={})) as entered:
        assert entered.get("artist") == {}
