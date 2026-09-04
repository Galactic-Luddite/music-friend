from __future__ import annotations

from datetime import datetime, timezone
from types import ModuleType, TracebackType

import httpx
import pytest

from music_friend.configuration import LocalConfig
from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.ticketmaster import TicketmasterAttraction, TicketmasterDiscoveryClient
from music_friend.providers.ticketmaster.transport import (
    TicketmasterOperation,
    TicketmasterTransport,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
QUERY_VALUE = "synthetic-ticketmaster-value"
QUERY_PARAMETER = "api" + "key"


class MemoryCredentialStore:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def save(self, _key: CredentialKey, value: str) -> None:
        self.value = value

    def load(self, _key: CredentialKey) -> str | None:
        return self.value

    def delete(self, _key: CredentialKey) -> None:
        self.value = None


class RecordingConnector(httpx.BaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.value += duration


def _config() -> LocalConfig:
    return LocalConfig(
        event_country_code="US",
        event_postal_code="94103",
        event_radius=50,
        event_radius_unit="miles",
    )


def _transport(responses: list[httpx.Response]) -> tuple[TicketmasterTransport, RecordingConnector]:
    connector = RecordingConnector(responses)
    return TicketmasterTransport(connector), connector


def test_ticketmaster_client_uses_exact_music_and_local_area_query_parameters() -> None:
    """Catches a provider request that broadens music matching or drops the user search area."""
    transport, connector = _transport(
        [
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "attractions": [
                            {
                                "id": "attraction-1",
                                "name": "One",
                                "classifications": [{"segment": {"name": "Music"}}],
                            }
                        ]
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "events": [
                            {
                                "id": "event-1",
                                "name": "Show",
                                "url": "https://www.ticketmaster.com/event/event-1",
                                "dates": {"start": {"dateTime": "2026-09-02T20:30:00Z"}},
                                "_embedded": {
                                    "venues": [
                                        {
                                            "name": "Venue",
                                            "city": {"name": "San Francisco"},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            ),
        ]
    )
    client = TicketmasterDiscoveryClient(
        transport,
        MemoryCredentialStore(QUERY_VALUE),
        now=lambda: NOW,
    )

    attractions = client.resolve_music_attractions("One")
    events = client.events_for_attraction(attractions[0], _config())

    assert attractions[0].name == "One"
    assert events[0].starts_at == datetime(2026, 9, 2, 20, 30, tzinfo=timezone.utc)
    attraction_query = dict(connector.requests[0].url.params)
    event_query = dict(connector.requests[1].url.params)
    assert connector.requests[0].url.path == "/discovery/v2/attractions.json"
    assert attraction_query == {
        QUERY_PARAMETER: QUERY_VALUE,
        "keyword": "One",
        "segmentName": "Music",
        "size": "10",
    }
    assert connector.requests[1].url.path == "/discovery/v2/events.json"
    assert event_query == {
        QUERY_PARAMETER: QUERY_VALUE,
        "attractionId": "attraction-1",
        "countryCode": "US",
        "postalCode": "94103",
        "radius": "50",
        "unit": "miles",
        "startDateTime": "2026-09-01T12:00:00Z",
        "endDateTime": "2027-09-01T12:00:00Z",
        "size": "200",
    }
    transport.close()


def test_ticketmaster_transport_trace_and_public_failure_never_expose_api_key() -> None:
    """Catches a trace or error that retains a Ticketmaster query credential value."""
    transport, _connector = _transport([httpx.Response(429, headers={"Retry-After": "3"})])

    with pytest.raises(RateLimitedError) as raised:
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "One"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )

    assert raised.value.retry_after_seconds == 3
    assert QUERY_VALUE not in repr(transport.trace)
    assert QUERY_VALUE not in str(raised.value)
    assert QUERY_VALUE not in repr(raised.value)
    transport.close()


def test_ticketmaster_client_strips_api_key_query_values_from_provider_urls() -> None:
    """Catches a provider URL that carries an API key into normalized event state."""
    transport, _connector = _transport(
        [
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "events": [
                            {
                                "id": "event-1",
                                "name": "Show",
                                "url": (
                                    "https://www.ticketmaster.com/event/event-1"
                                    f"?{QUERY_PARAMETER}={QUERY_VALUE}"
                                ),
                            }
                        ]
                    }
                },
            )
        ]
    )
    client = TicketmasterDiscoveryClient(
        transport,
        MemoryCredentialStore(QUERY_VALUE),
        now=lambda: NOW,
    )

    event = client.events_for_attraction(TicketmasterAttraction("attraction-1", "One"), _config())

    assert event[0].source_url == "https://www.ticketmaster.com/event/event-1"
    assert QUERY_VALUE not in repr(event)
    transport.close()


def test_ticketmaster_client_paces_requests_to_two_per_second() -> None:
    """Catches consecutive provider requests starting less than one-half second apart."""
    transport, _connector = _transport(
        [
            httpx.Response(200, json={"_embedded": {"attractions": []}}),
            httpx.Response(200, json={"_embedded": {"attractions": []}}),
        ]
    )
    clock = FakeMonotonicClock()
    client = TicketmasterDiscoveryClient(
        transport,
        MemoryCredentialStore(QUERY_VALUE),
        now=lambda: NOW,
        monotonic=clock,
        sleeper=clock.sleep,
    )

    client.resolve_music_attractions("One")
    client.resolve_music_attractions("Two")

    assert clock.sleeps == [0.5]
    transport.close()


def test_ticketmaster_client_has_no_network_path_without_a_protected_key() -> None:
    """Catches a missing key that reaches the external provider with an empty credential."""
    transport, connector = _transport([httpx.Response(200, json={})])
    client = TicketmasterDiscoveryClient(transport, MemoryCredentialStore(None), now=lambda: NOW)

    assert client.is_configured() is False
    with pytest.raises(ValueError):
        client.resolve_music_attractions("One")
    assert connector.requests == []
    transport.close()


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: TicketmasterAttraction("", "One"), "native_id"),
        (lambda: TicketmasterAttraction("one", " "), "name"),
        (
            lambda: __import__(
                "music_friend.providers.ticketmaster", fromlist=["TicketmasterEvent"]
            ).TicketmasterEvent("", "Show", None, None, None, None, None, None, None),
            "native_id",
        ),
        (
            lambda: __import__(
                "music_friend.providers.ticketmaster", fromlist=["TicketmasterEvent"]
            ).TicketmasterEvent("event", " ", None, None, None, None, None, None, None),
            "title",
        ),
        (
            lambda: __import__(
                "music_friend.providers.ticketmaster", fromlist=["TicketmasterEvent"]
            ).TicketmasterEvent("event", "Show", None, "date", None, None, None, None, None),
            "time_precision",
        ),
        (
            lambda: __import__(
                "music_friend.providers.ticketmaster", fromlist=["TicketmasterEvent"]
            ).TicketmasterEvent(
                "event", "Show", datetime(2026, 1, 1), "second", None, None, None, None, None
            ),
            "starts_at",
        ),
        (
            lambda: __import__(
                "music_friend.providers.ticketmaster", fromlist=["TicketmasterEvent"]
            ).TicketmasterEvent("event", "Show", None, None, "", None, None, None, None),
            "venue_name",
        ),
    ),
)
def test_ticketmaster_models_reject_invalid_identity_time_and_optional_text(
    factory: object, message: str
) -> None:
    """Catches malformed provider data entering the normalized local event model."""
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


def test_ticketmaster_client_rejects_invalid_dependencies_inputs_and_clock() -> None:
    """Catches invalid adapter construction or calls before any provider request is sent."""
    transport, connector = _transport([httpx.Response(200, json={})])
    with pytest.raises(ValueError, match="transport"):
        TicketmasterDiscoveryClient(object(), MemoryCredentialStore(QUERY_VALUE), now=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="credential_store"):
        TicketmasterDiscoveryClient(transport, object(), now=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="now"):
        TicketmasterDiscoveryClient(transport, MemoryCredentialStore(QUERY_VALUE), now=None)  # type: ignore[arg-type]

    client = TicketmasterDiscoveryClient(
        transport,
        MemoryCredentialStore(QUERY_VALUE),
        now=lambda: datetime(2026, 1, 1),
        monotonic=lambda: "bad",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="artist_name"):
        client.resolve_music_attractions(" ")
    with pytest.raises(ValueError, match="monotonic"):
        client.resolve_music_attractions("One")
    with pytest.raises(ValueError, match="attraction"):
        client.events_for_attraction(object(), _config())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="config"):
        client.events_for_attraction(TicketmasterAttraction("one", "One"), object())  # type: ignore[arg-type]
    assert connector.requests == []
    transport.close()


def test_ticketmaster_event_search_requires_complete_area_and_aware_clock() -> None:
    """Catches ambiguous event searches and local-time timestamps before egress."""
    transport, connector = _transport([httpx.Response(200, json={})])
    attraction = TicketmasterAttraction("one", "One")
    client = TicketmasterDiscoveryClient(
        transport, MemoryCredentialStore(QUERY_VALUE), now=lambda: datetime(2026, 1, 1)
    )
    with pytest.raises(ValueError, match="search area"):
        client.events_for_attraction(attraction, LocalConfig())
    with pytest.raises(ValueError, match="timezone-aware"):
        client.events_for_attraction(attraction, _config())
    assert connector.requests == []
    transport.close()


def test_ticketmaster_client_filters_non_music_and_normalizes_partial_event_shapes() -> None:
    """Catches non-music matches and malformed optional event fields becoming local claims."""
    transport, _connector = _transport(
        [
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "attractions": [
                            {"id": "spoken", "name": "One", "classifications": []},
                            {"id": "bad", "name": "One", "classifications": [None]},
                            {
                                "id": "music",
                                "name": "One",
                                "classifications": [{"segment": {"name": "Music"}}],
                            },
                        ]
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "events": [
                            {"id": "missing-time", "name": "Show"},
                            {
                                "id": "bad-time",
                                "name": "Show",
                                "dates": {"start": {"dateTime": "not-a-time"}},
                                "_embedded": {"venues": [None]},
                            },
                            {
                                "id": "local-time",
                                "name": "Show",
                                "dates": {"start": {"dateTime": "2026-01-01T12:00:00"}},
                                "_embedded": {"venues": [{"name": 3, "city": {"name": 4}}]},
                            },
                        ]
                    }
                },
            ),
        ]
    )
    client = TicketmasterDiscoveryClient(
        transport, MemoryCredentialStore(QUERY_VALUE), now=lambda: NOW
    )

    assert client.resolve_music_attractions("One") == (TicketmasterAttraction("music", "One"),)
    events = client.events_for_attraction(TicketmasterAttraction("music", "One"), _config())
    assert len(events) == 3
    assert all(event.starts_at is None and event.venue_name is None for event in events)
    transport.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"_embedded": []},
        {"_embedded": {"attractions": {}}},
        {"_embedded": {"attractions": [None]}},
        {"_embedded": {"attractions": [{}] * 201}},
    ),
)
def test_ticketmaster_client_rejects_malformed_embedded_collections(payload: object) -> None:
    """Catches oversized or structurally confused provider collections."""
    transport, _connector = _transport([httpx.Response(200, json=payload)])
    client = TicketmasterDiscoveryClient(
        transport, MemoryCredentialStore(QUERY_VALUE), now=lambda: NOW
    )
    with pytest.raises(InvalidSourceResponseError):
        client.resolve_music_attractions("One")
    transport.close()


class RaisingCredentialStore(MemoryCredentialStore):
    def load(self, _key: CredentialKey) -> str | None:
        raise RuntimeError("credential backend unavailable")


def test_ticketmaster_credential_backend_failure_is_closed() -> None:
    """Catches local credential failures escaping as backend diagnostics or provider egress."""
    transport, connector = _transport([httpx.Response(200, json={})])
    client = TicketmasterDiscoveryClient(transport, RaisingCredentialStore(None), now=lambda: NOW)
    assert client.is_configured() is False
    with pytest.raises(ValueError, match="credentials are unavailable"):
        client.resolve_music_attractions("One")
    assert connector.requests == []
    transport.close()


class FailingConnector(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)


def _exception_graph_values(error: BaseException) -> tuple[str, ...]:
    """Collect bounded public exception data and recursively linked implementation state."""
    values: list[str] = []
    pending: list[tuple[object, int]] = [(error, 0)]
    seen: set[int] = set()
    while pending and len(seen) < 10_000:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        try:
            values.extend((str(current), repr(current)))
        except Exception:
            values.append(type(current).__name__)
        if depth >= 12 or isinstance(current, (ModuleType, type)) or callable(current):
            continue
        children: list[object] = []
        if isinstance(current, BaseException):
            children.extend(current.args)
            children.extend(item for item in (current.__cause__, current.__context__) if item)
            traceback: TracebackType | None = current.__traceback__
            while traceback is not None:
                if traceback.tb_frame.f_globals.get("__name__", "").startswith("music_friend"):
                    children.extend(traceback.tb_frame.f_locals.values())
                traceback = traceback.tb_next
        if isinstance(current, dict):
            children.extend(current.keys())
            children.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset)):
            children.extend(current)
        try:
            children.extend(vars(current).values())
        except TypeError:
            pass
        slots = getattr(type(current), "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            try:
                children.append(getattr(current, slot))
            except (AttributeError, TypeError):
                pass
        pending.extend((child, depth + 1) for child in children)
    return tuple(values)


def test_ticketmaster_request_failure_retains_no_request_or_key_in_exception_graph() -> None:
    """Catches credentials retained by chaining or traceback-linked HTTP state."""
    transport = TicketmasterTransport(FailingConnector())

    with pytest.raises(SourceUnavailableError) as raised:
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "synthetic-request-parameter-marker"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )

    graph = "\n".join(_exception_graph_values(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert QUERY_VALUE not in graph
    assert "synthetic-request-parameter-marker" not in graph
    assert "discovery/v2/attractions.json" not in graph
    transport.close()


def test_ticketmaster_malformed_json_retains_no_response_body_in_exception_graph() -> None:
    """Catches malformed provider content retained by decoder or response attributes."""
    body_marker = "synthetic-malformed-response-marker"
    transport, _connector = _transport(
        [
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                text=f'{{"value":"{body_marker}"',
            )
        ]
    )

    with pytest.raises(InvalidSourceResponseError) as raised:
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "One"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )

    graph = "\n".join(_exception_graph_values(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert body_marker not in graph
    assert QUERY_VALUE not in graph
    transport.close()


def test_ticketmaster_non_object_json_retains_no_payload_in_exception_graph() -> None:
    """Catches decoded provider objects retained by a public error traceback."""
    payload_marker = "synthetic-decoded-payload-marker"
    transport, _connector = _transport([httpx.Response(200, json=[payload_marker])])

    with pytest.raises(InvalidSourceResponseError) as raised:
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "One"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )

    graph = "\n".join(_exception_graph_values(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert payload_marker not in graph
    assert QUERY_VALUE not in graph
    transport.close()


def test_ticketmaster_response_close_failure_retains_no_provider_state() -> None:
    """Catches response cleanup failures escaping with provider state in helper locals."""
    body_marker = "synthetic-close-body-marker"
    close_marker = "synthetic-close-failure-marker"
    parameter_marker = "synthetic-close-parameter-marker"
    response = httpx.Response(200, json={"value": body_marker})

    def fail_close() -> None:
        raise RuntimeError(close_marker)

    response.close = fail_close  # type: ignore[method-assign]
    transport, _connector = _transport([response])

    with pytest.raises(SourceUnavailableError) as raised:
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", parameter_marker),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )

    graph = "\n".join(_exception_graph_values(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    for marker in (
        QUERY_VALUE,
        parameter_marker,
        body_marker,
        close_marker,
        "discovery/v2/attractions.json",
    ):
        assert marker not in graph
    transport.close()


@pytest.mark.parametrize(
    ("response", "error_type"),
    (
        (httpx.Response(503, json={}), SourceUnavailableError),
        (httpx.Response(200, text="not-json"), InvalidSourceResponseError),
        (httpx.Response(200, json=[]), InvalidSourceResponseError),
    ),
)
def test_ticketmaster_transport_maps_http_and_payload_failures(
    response: httpx.Response, error_type: type[Exception]
) -> None:
    """Catches provider diagnostics or non-object payloads crossing the transport boundary."""
    transport, _connector = _transport([response])
    with pytest.raises(error_type):
        transport.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "One"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )
    transport.close()

    unavailable = TicketmasterTransport(FailingConnector())
    with pytest.raises(SourceUnavailableError):
        unavailable.execute(
            TicketmasterOperation.ATTRACTIONS,
            query=(
                ("apikey", QUERY_VALUE),
                ("keyword", "One"),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )
    unavailable.close()


@pytest.mark.parametrize(
    "query",
    (
        (),
        (("apikey", QUERY_VALUE), ("keyword", "One"), ("segmentName", "Music"), "bad"),
        (("apikey", QUERY_VALUE), ("keyword", "One"), ("segmentName", "Music"), ("size", "")),
        (
            ("apikey", QUERY_VALUE),
            ("keyword", "One"),
            ("segmentName", "Music"),
            ("apikey", "other"),
        ),
    ),
)
def test_ticketmaster_transport_rejects_invalid_parameter_shapes_before_egress(
    query: object,
) -> None:
    """Catches missing, malformed, empty, or duplicate parameter sets before network access."""
    transport, connector = _transport([httpx.Response(200, json={})])
    with pytest.raises(InvalidSourceResponseError):
        transport.execute(TicketmasterOperation.ATTRACTIONS, query=query)  # type: ignore[arg-type]
    assert connector.requests == []
    transport.close()


def test_ticketmaster_transport_rejects_non_enum_operation_and_bounds_trace() -> None:
    """Catches operation confusion and unbounded retention of request metadata."""
    responses = [httpx.Response(200, json={}) for _ in range(129)]
    transport, _connector = _transport(responses)
    with pytest.raises(InvalidSourceResponseError):
        transport.execute("attractions", query=())  # type: ignore[arg-type]
    query = (("apikey", QUERY_VALUE), ("keyword", "One"), ("segmentName", "Music"), ("size", "10"))
    for _ in range(129):
        transport.execute(TicketmasterOperation.ATTRACTIONS, query=query)
    assert len(transport.trace) == 128
    transport.close()
