"""Behavioral tests for the deny-by-default Spotify transport."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from music_friend.errors import (
    AdditionalScopeRequiredError,
    AuthenticationRequiredError,
    InvalidSourceResponseError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)
from music_friend.providers.spotify import transport as transport_module
from music_friend.providers.spotify.transport import (
    SpotifyOperation,
    SpotifyTransport,
    _validate_destination,
)


class RecordingHandler:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ExplodingBody(httpx.SyncByteStream):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or AssertionError("body must not be consumed")
        self.iterations = 0

    def __iter__(self) -> Iterator[bytes]:
        self.iterations += 1
        raise self.error
        yield b""


class RecordingConnector(httpx.BaseTransport):
    def __init__(self) -> None:
        self.closed = False
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    def close(self) -> None:
        self.closed = True


def _transport(
    responses: list[httpx.Response | Exception],
    *,
    clock: Callable[[], float] | None = None,
) -> tuple[SpotifyTransport, RecordingHandler]:
    handler = RecordingHandler(responses)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return SpotifyTransport(httpx.MockTransport(handler), **kwargs), handler


def _public_error_renderings(error: Exception, transport: SpotifyTransport) -> str:
    public_dict = error.to_public_dict() if hasattr(error, "to_public_dict") else {}
    record = logging.LogRecord(
        "music-friend-test",
        logging.ERROR,
        "test_transport.py",
        1,
        "public error",
        (),
        (type(error), error, error.__traceback__),
    )
    return "\n".join(
        (
            str(error),
            repr(error),
            json.dumps(public_dict, sort_keys=True),
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            logging.Formatter("%(message)s").format(record),
            repr(transport.trace),
        )
    )


def _protocol_failure() -> httpx.RemoteProtocolError:
    request = httpx.Request(
        "GET",
        "https://trace-canary.example.invalid/path",
        headers={"X-Synthetic": "header-canary"},
    )
    return httpx.RemoteProtocolError("protocol-canary", request=request)


def test_exact_operation_matrix_matches_independent_literal_oracle() -> None:
    cases = (
        (
            SpotifyOperation.TOKEN,
            "POST",
            "accounts.spotify.com",
            "/api/token",
            (),
            (("grant_type", "authorization_code"),),
            None,
        ),
        (SpotifyOperation.HEALTH, "GET", "api.spotify.com", "/v1/me", (), (), "access-value"),
        (
            SpotifyOperation.SEARCH_ARTISTS,
            "GET",
            "api.spotify.com",
            "/v1/search",
            (("q", "sample"), ("limit", "10")),
            (),
            "access-value",
        ),
        (
            SpotifyOperation.FOLLOWED_ARTISTS,
            "GET",
            "api.spotify.com",
            "/v1/me/following",
            (("limit", "50"), ("after", "artist123")),
            (),
            "access-value",
        ),
        (
            SpotifyOperation.SAVED_TRACKS,
            "GET",
            "api.spotify.com",
            "/v1/me/tracks",
            (("limit", "50"), ("offset", "0")),
            (),
            "access-value",
        ),
        (
            SpotifyOperation.TOP_TRACKS,
            "GET",
            "api.spotify.com",
            "/v1/me/top/tracks",
            (("time_range", "short_term"), ("limit", "10")),
            (),
            "access-value",
        ),
        (
            SpotifyOperation.ARTIST_RELEASES,
            "GET",
            "api.spotify.com",
            "/v1/artists/artist123/albums",
            (("artist_id", "artist123"), ("limit", "10")),
            (),
            "access-value",
        ),
    )
    transport, handler = _transport([httpx.Response(200, json={"ok": True}) for _ in cases])

    for operation, _method, _host, _path, query, form, access_token in cases:
        transport.execute(operation, query=query, form=form, **{"access_" + "token": access_token})

    actual = tuple(
        (request.method, request.url.host, request.url.path) for request in handler.requests
    )
    expected = tuple((method, host, path) for _, method, host, path, *_rest in cases)
    assert actual == expected
    assert handler.requests[2].url.params["type"] == "artist"
    assert handler.requests[3].url.params["type"] == "artist"
    assert "artist_id" not in handler.requests[-1].url.params
    assert handler.requests[0].headers.get("authorization") is None
    assert handler.requests[1].headers["authorization"] == "Bearer access-value"


def test_public_surface_exposes_only_declared_transport_interfaces() -> None:
    assert transport_module.__all__ == ["SpotifyOperation", "SpotifyTransport"]
    assert not hasattr(transport_module, "ACCOUNTS_ORIGIN")
    assert not hasattr(transport_module, "API_ORIGIN")
    assert not hasattr(transport_module, "REQUESTS")


def test_token_request_contains_exact_pkce_form_and_no_secret() -> None:
    form = (
        ("client_id", "public-client"),
        ("grant_type", "authorization_code"),
        ("code", "authorization-code"),
        ("redirect_uri", "http://127.0.0.1:8765/callback"),
        ("code_verifier", "verifier-value"),
    )
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])

    transport.execute(SpotifyOperation.TOKEN, form=form)

    assert parse_qsl(handler.requests[0].content.decode("ascii")) == list(form)
    assert ("client_" + "secret") not in handler.requests[0].content.decode("ascii")


@pytest.mark.parametrize(
    ("origin", "path"),
    (
        ("http://api.spotify.com", "/v1/me"),
        ("https://user@api.spotify.com", "/v1/me"),
        ("https://api.spotify.com:444", "/v1/me"),
        ("https://api.spotify.com.example.invalid", "/v1/me"),
        ("https://api.spotify.com", "//example.invalid/v1/me"),
        ("https://api.spotify.com", "/v1/me?next=https://example.invalid"),
        ("https://api.spotify.com", "/v1/../api/token"),
    ),
)
def test_egress_matrix_rejects_destination_before_connection(origin: str, path: str) -> None:
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])

    with pytest.raises(InvalidSourceResponseError):
        _validate_destination(origin, path)

    assert handler.requests == []


@pytest.mark.parametrize("status", (300, 301, 302, 303, 304, 305, 306, 307, 308, 399))
def test_every_redirect_is_rejected_without_followup(status: int) -> None:
    transport, handler = _transport(
        [httpx.Response(status, headers={"Location": "https://example.invalid/escape"})]
    )

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert len(handler.requests) == 1


def test_malformed_redirect_location_is_nonretryable_and_fully_redacted() -> None:
    location_canary = "javascript:redirect-location-canary"
    transport, handler = _transport(
        [
            httpx.Response(302, headers={"Location": location_canary}),
            httpx.Response(200, json={"unexpected": True}),
        ]
    )

    with pytest.raises(InvalidSourceResponseError) as raised:
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "access-value"},
        )

    assert len(handler.requests) == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert location_canary not in _public_error_renderings(raised.value, transport)


def test_malformed_redirect_redaction_control_detects_raw_chaining() -> None:
    transport, _ = _transport([])
    location_canary = "redirect-location-canary"
    try:
        try:
            raise httpx.InvalidURL(location_canary)
        except httpx.InvalidURL as raw_error:
            raise InvalidSourceResponseError() from raw_error
    except InvalidSourceResponseError as exposed:
        rendered = _public_error_renderings(exposed, transport)

    assert location_canary in rendered


@pytest.mark.parametrize(
    ("status", "error_type", "copies"),
    (
        (302, InvalidSourceResponseError, 1),
        (401, AuthenticationRequiredError, 1),
        (403, AdditionalScopeRequiredError, 1),
        (500, SourceUnavailableError, 2),
    ),
)
def test_status_classification_precedes_body_consumption(
    status: int, error_type: type[Exception], copies: int
) -> None:
    bodies = [ExplodingBody() for _ in range(copies)]
    transport, _ = _transport([httpx.Response(status, stream=body) for body in bodies])

    with pytest.raises(error_type):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert [body.iterations for body in bodies] == [0] * copies


def test_response_body_is_streamed_and_bounded_to_one_mebibyte() -> None:
    transport, _ = _transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"{" + b'"x":"' + b"a" * (1024 * 1024) + b'"}',
            )
        ]
    )

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})


@pytest.mark.parametrize(
    "content",
    (
        b"not-json",
        b"[]",
        b'{"nested":' + b"[" * 34 + b"0" + b"]" * 34 + b"}",
        b'{"number": NaN}',
    ),
)
def test_invalid_json_shape_or_depth_is_rejected(content: bytes) -> None:
    transport, _ = _transport(
        [httpx.Response(200, headers={"Content-Type": "application/json"}, content=content)]
    )
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})


def test_malformed_response_does_not_survive_in_exception_chain() -> None:
    transport, _ = _transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"response-canary",
            )
        ]
    )

    with pytest.raises(InvalidSourceResponseError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-canary"})

    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "headers",
    (
        (),
        (("Content-Type", "application/json"), ("content-type", "application/json")),
        (("Content-Type", ""),),
        (("Content-Type", "text/html"),),
        (("Content-Type", "application/problem+json"),),
        (("Content-Type", "application/json, text/html"),),
        (("Content-Type", "application/json; charset"),),
        (("Content-Type", "application/json; charset=latin-1"),),
        (("Content-Type", "application/json; boundary=value"),),
        (("Content-Type", "application/json; charset=utf-8; charset=utf-8"),),
    ),
    ids=(
        "missing",
        "duplicate",
        "empty",
        "non-json",
        "json-suffix",
        "combined",
        "malformed-parameter",
        "wrong-charset",
        "unknown-parameter",
        "duplicate-parameter",
    ),
)
def test_success_requires_one_exact_json_content_type_before_body(
    headers: tuple[tuple[str, str], ...],
) -> None:
    body = ExplodingBody()
    transport, _ = _transport([httpx.Response(200, headers=headers, stream=body)])

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert body.iterations == 0


@pytest.mark.parametrize(
    "content_type",
    (
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "APPLICATION/JSON ; CHARSET = UTF-8",
    ),
)
def test_success_accepts_json_content_type_case_insensitively(
    content_type: str,
) -> None:
    transport, _ = _transport(
        [httpx.Response(200, headers={"content-type": content_type}, content=b'{"ok":true}')]
    )

    response = transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert response.data == {"ok": True}


@pytest.mark.parametrize("error_type", (httpx.ConnectTimeout, httpx.ReadTimeout))
def test_idempotent_get_retries_one_transport_failure(error_type: type[Exception]) -> None:
    transport, handler = _transport(
        [error_type("synthetic-canary"), httpx.Response(200, json={"ok": True})]
    )

    assert transport.execute(
        SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"}
    ).data == {"ok": True}
    assert len(handler.requests) == 2


def test_get_stops_after_one_retry() -> None:
    transport, handler = _transport(
        [httpx.ConnectError("first-canary"), httpx.ReadError("second-canary")]
    )

    with pytest.raises(SourceUnavailableError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert len(handler.requests) == 2
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "canary" not in _public_error_renderings(error.value, transport)


def test_token_post_is_never_retried() -> None:
    transport, handler = _transport(
        [httpx.ConnectError("synthetic-canary"), httpx.Response(200, json={"unexpected": True})]
    )

    with pytest.raises(SourceUnavailableError):
        transport.execute(
            SpotifyOperation.TOKEN,
            form=(("grant_type", "authorization_code"),),
        )

    assert len(handler.requests) == 1


def test_call_deadline_is_clamped_to_forty_five_seconds() -> None:
    times: Iterator[float] = iter((100.0, 100.0, 146.0))
    transport, handler = _transport(
        [httpx.ConnectTimeout("synthetic-canary"), httpx.Response(200, json={"unexpected": True})],
        clock=lambda: next(times),
    )

    with pytest.raises(SourceUnavailableError):
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "access-value"},
            deadline=1000.0,
        )

    assert len(handler.requests) == 1


def test_timeout_object_bounds_every_phase_by_remaining_deadline() -> None:
    captured: list[httpx.Timeout] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    transport = SpotifyTransport(httpx.MockTransport(handler), clock=lambda: 10.0)
    transport.execute(
        SpotifyOperation.HEALTH,
        deadline=22.5,
        **{"access_" + "token": "access-value"},
    )

    assert captured == [{"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5}]


def test_proxy_environment_cannot_change_the_injected_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.invalid:444")
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])

    with transport:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert len(handler.requests) == 1
    assert handler.requests[0].url == "https://api.spotify.com/v1/me"


def test_client_construction_is_fail_closed_against_proxy_and_redirect_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_client = httpx.Client
    captured: list[dict[str, object]] = []

    def recording_client(*args: object, **kwargs: object) -> httpx.Client:
        captured.append(kwargs)
        return actual_client(*args, **kwargs)

    monkeypatch.setattr(transport_module.httpx, "Client", recording_client)
    transport = SpotifyTransport(httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    transport.close()

    assert captured[0]["trust_env"] is False
    assert captured[0]["follow_redirects"] is False


def test_arbitrary_preconfigured_client_is_rejected() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    try:
        with pytest.raises(ValueError):
            SpotifyTransport(client)  # type: ignore[arg-type]
    finally:
        client.close()


def test_explicit_close_and_context_exit_close_the_injected_connector() -> None:
    explicit_connector = RecordingConnector()
    explicit = SpotifyTransport(explicit_connector)
    explicit.close()
    assert explicit_connector.closed is True

    context_connector = RecordingConnector()
    with SpotifyTransport(context_connector) as context:
        context.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})
    assert context_connector.closed is True


def test_rate_limit_is_clamped_and_exact_quota_body_refines_category() -> None:
    limited, _ = _transport([httpx.Response(429, headers={"Retry-After": "999999"})])
    with pytest.raises(RateLimitedError) as error:
        limited.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})
    assert error.value.retry_after_seconds == 900

    quota, _ = _transport([httpx.Response(429, json={"error": {"reason": "QUOTA_EXCEEDED"}})])
    with pytest.raises(QuotaExhaustedError):
        quota.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(429),
        httpx.Response(429, content=b"malformed-canary"),
        httpx.Response(429, content=b"x" * (1024 * 1024 + 1)),
        httpx.Response(
            429,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"not-a-gzip-stream-canary"),
        ),
        httpx.Response(429, stream=ExplodingBody(httpx.DecodingError("decode-canary"))),
    ),
)
def test_malformed_429_bodies_retain_safe_rate_limit_category(response: httpx.Response) -> None:
    transport, _ = _transport([response])

    with pytest.raises(RateLimitedError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "canary" not in _public_error_renderings(error.value, transport)


@pytest.mark.parametrize(
    "error_factory",
    (
        _protocol_failure,
        lambda: httpx.DecodingError("decoding-canary"),
        lambda: httpx.StreamError("stream-canary"),
    ),
)
def test_every_expected_httpx_failure_is_stably_redacted(
    error_factory: Callable[[], Exception],
) -> None:
    transport, _ = _transport([error_factory(), error_factory()])
    sensitive_value = "access-" + "canary"

    with pytest.raises(SourceUnavailableError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": sensitive_value})

    rendered = _public_error_renderings(error.value, transport)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "canary" not in rendered
    assert "example.invalid" not in rendered
    assert "X-Synthetic" not in rendered


def test_success_body_decoding_failures_are_normalized_after_one_get_retry() -> None:
    responses = [
        httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json",
            },
            stream=httpx.ByteStream(b"malformed-gzip-canary"),
        )
        for _ in range(2)
    ]
    transport, _ = _transport(responses)

    with pytest.raises(SourceUnavailableError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "canary" not in _public_error_renderings(error.value, transport)


def test_success_body_stream_failures_are_normalized_after_one_get_retry() -> None:
    bodies = [ExplodingBody(httpx.StreamError("stream-canary")) for _ in range(2)]
    transport, _ = _transport(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=body,
            )
            for body in bodies
        ]
    )

    with pytest.raises(SourceUnavailableError) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert "canary" not in _public_error_renderings(error.value, transport)


def test_traceback_positive_control_detects_raw_exception_chaining() -> None:
    transport, _ = _transport([])
    try:
        try:
            raise _protocol_failure()
        except httpx.RemoteProtocolError as raw_error:
            raise SourceUnavailableError() from raw_error
    except SourceUnavailableError as exposed:
        rendered = _public_error_renderings(exposed, transport)

    assert "protocol-canary" in rendered


@pytest.mark.parametrize(
    ("status", "error_type"),
    (
        (401, AuthenticationRequiredError),
        (403, AdditionalScopeRequiredError),
        (500, SourceUnavailableError),
        (503, SourceUnavailableError),
    ),
)
def test_http_failures_map_to_stable_redacted_errors(
    status: int, error_type: type[Exception]
) -> None:
    response = httpx.Response(status, content=b'{"error":"response-canary"}')
    transport, _ = _transport([response, response] if status >= 500 else [response])

    with pytest.raises(error_type) as error:
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-canary"})

    rendered = repr(error.value) + str(error.value)
    assert "canary" not in rendered


@pytest.mark.parametrize("artist_id", ("", "bad/id", "bad?query", "a" * 65, "with space"))
def test_artist_identifier_is_validated_before_request(artist_id: str) -> None:
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.ARTIST_RELEASES,
            query=(("artist_id", artist_id), ("limit", "10")),
            **{"access_" + "token": "access-value"},
        )

    assert handler.requests == []


def test_unapproved_parameters_and_duplicate_keys_fail_before_request() -> None:
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.HEALTH,
            query=(("next", "https://example.invalid"),),
            **{"access_" + "token": "access-value"},
        )
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.SEARCH_ARTISTS,
            query=(("q", "one"), ("q", "two"), ("limit", "10")),
            **{"access_" + "token": "access-value"},
        )
    assert handler.requests == []


def test_trace_contains_only_operation_and_parameter_shape() -> None:
    transport, _ = _transport([httpx.Response(200, json={"ok": True})])
    transport.execute(
        SpotifyOperation.SEARCH_ARTISTS,
        query=(("q", "query-canary"), ("limit", "10")),
        **{"access_" + "token": "access-canary"},
    )

    trace = repr(transport.trace)
    assert "search_artists" in trace
    assert "query-canary" not in trace
    assert "access-canary" not in trace


def test_trace_retains_only_a_bounded_number_of_safe_entries() -> None:
    transport, _ = _transport([httpx.Response(200, json={"ok": True})] * 129)

    for _ in range(129):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})

    assert len(transport.trace) == 128


def test_operation_must_be_exact_closed_enum_before_request() -> None:
    transport, handler = _transport([httpx.Response(200, json={"ok": True})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(  # type: ignore[arg-type]
            "health", **{"access_" + "token": "access-value"}
        )
    assert handler.requests == []


@pytest.mark.parametrize("deadline", (True, "45", float("nan"), float("inf")))
def test_explicit_deadline_must_be_a_finite_number(deadline: object) -> None:
    """Catches malformed caller deadlines weakening or confusing the transport time bound."""
    transport, handler = _transport([httpx.Response(200, json={})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.HEALTH,
            deadline=deadline,  # type: ignore[arg-type]
            **{"access_" + "token": "access-value"},
        )
    assert handler.requests == []


@pytest.mark.parametrize("clock_value", (True, "zero", float("nan"), float("inf")))
def test_transport_clock_must_return_finite_seconds(clock_value: object) -> None:
    """Catches an invalid monotonic clock disabling the request deadline."""
    transport, handler = _transport(
        [httpx.Response(200, json={})],
        clock=lambda: clock_value,  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(SourceUnavailableError):
        transport.execute(SpotifyOperation.HEALTH, **{"access_" + "token": "access-value"})
    assert handler.requests == []


@pytest.mark.parametrize(
    "query",
    (
        "q=One",
        (("q", "One"), ("limit", "1"), ("extra", "value")) * 6,
        (("q", "One"), ["limit", "1"]),
        ((1, "One"), ("limit", "1")),
        (("q", "One"), ("limit", 1)),
    ),
)
def test_transport_rejects_nonsequence_oversized_or_nontyped_parameters(query: object) -> None:
    """Catches confused parameter containers and key/value types before provider egress."""
    transport, handler = _transport([httpx.Response(200, json={})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.SEARCH_ARTISTS,
            query=query,  # type: ignore[arg-type]
            **{"access_" + "token": "access-value"},
        )
    assert handler.requests == []


def test_transport_rejects_access_token_on_token_endpoint_and_missing_resource_token() -> None:
    """Catches bearer credentials crossing endpoint boundaries in either direction."""
    transport, handler = _transport([httpx.Response(200, json={})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.TOKEN,
            form=(
                ("client_id", "client"),
                ("grant_type", "authorization_code"),
                ("code", "code"),
                ("redirect_uri", "http://127.0.0.1/callback"),
                ("code_verifier", "verifier"),
            ),
            **{"access_" + "token": "access-value"},
        )
    with pytest.raises(AuthenticationRequiredError):
        transport.execute(SpotifyOperation.HEALTH)
    assert handler.requests == []
