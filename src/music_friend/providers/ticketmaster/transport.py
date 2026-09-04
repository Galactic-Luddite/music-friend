"""Closed, redacted HTTP transport for Ticketmaster Discovery API reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias, cast

import httpx

from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError

_ORIGIN = "https://app.ticketmaster.com"
_MAX_PARAMETER_LENGTH = 4096
_MAX_TRACE_ENTRIES = 128
_JSON_CONTENT_TYPE = "application/json"

ParameterPairs: TypeAlias = Sequence[tuple[str, str]]


class TicketmasterOperation(str, Enum):
    ATTRACTIONS = "attractions"
    EVENTS = "events"


@dataclass(frozen=True, slots=True)
class _RequestDefinition:
    path: str
    query_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TraceEntry:
    operation: str
    query_keys: tuple[str, ...]


class _RequestOutcome(str, Enum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _RequestResult:
    outcome: _RequestOutcome
    trace_entry: _TraceEntry | None = None
    payload: dict[str, object] | None = None
    retry_after: int | None = None


_REQUESTS: Mapping[TicketmasterOperation, _RequestDefinition] = MappingProxyType(
    {
        TicketmasterOperation.ATTRACTIONS: _RequestDefinition(
            "/discovery/v2/attractions.json",
            frozenset({"apikey", "keyword", "segmentName", "size"}),
        ),
        TicketmasterOperation.EVENTS: _RequestDefinition(
            "/discovery/v2/events.json",
            frozenset(
                {
                    "apikey",
                    "attractionId",
                    "countryCode",
                    "postalCode",
                    "radius",
                    "unit",
                    "startDateTime",
                    "endDateTime",
                    "size",
                }
            ),
        ),
    }
)


class TicketmasterTransport:
    """Execute the two fixed Discovery API read operations through an injected connector."""

    def __init__(self, connector: httpx.BaseTransport) -> None:
        if not isinstance(connector, httpx.BaseTransport):
            raise ValueError("connector must be an httpx.BaseTransport")
        self._client = httpx.Client(
            transport=connector,
            trust_env=False,
            follow_redirects=False,
            timeout=10.0,
        )
        self._trace: list[_TraceEntry] = []

    def close(self) -> None:
        self._client.close()

    @property
    def trace(self) -> tuple[_TraceEntry, ...]:
        """Return bounded operation metadata without query values."""
        return tuple(self._trace)

    def execute(
        self, operation: TicketmasterOperation, *, query: ParameterPairs
    ) -> dict[str, object]:
        """Send one whitelisted request and map provider diagnostics to closed errors."""
        result = _perform_request(self._client, operation, query)
        trace = self._trace
        del self, operation, query
        if result.trace_entry is not None:
            trace.append(result.trace_entry)
            if len(trace) > _MAX_TRACE_ENTRIES:
                del trace[:-_MAX_TRACE_ENTRIES]
        del trace
        if result.outcome is _RequestOutcome.SUCCESS:
            assert result.payload is not None
            return result.payload
        if result.outcome is _RequestOutcome.RATE_LIMITED:
            raise RateLimitedError(result.retry_after)
        if result.outcome is _RequestOutcome.UNAVAILABLE:
            raise SourceUnavailableError()
        raise InvalidSourceResponseError()


def _perform_request(client: httpx.Client, operation: object, query: object) -> _RequestResult:
    """Contain all provider request and decoding state below the public raise frame."""
    if type(operation) is not TicketmasterOperation:
        return _RequestResult(_RequestOutcome.INVALID)
    definition = _REQUESTS[operation]
    try:
        parameters = _validate_parameters(cast(ParameterPairs, query), definition.query_keys)
    except InvalidSourceResponseError:
        return _RequestResult(_RequestOutcome.INVALID)
    trace_entry = _TraceEntry(operation.value, tuple(sorted(parameters)))
    try:
        response = client.get(f"{_ORIGIN}{definition.path}", params=parameters)
    except httpx.RequestError:
        return _RequestResult(_RequestOutcome.UNAVAILABLE, trace_entry)
    parameters.clear()
    result: _RequestResult
    payload: object | None = None
    try:
        if response.status_code == 429:
            result = _RequestResult(
                _RequestOutcome.RATE_LIMITED,
                trace_entry,
                retry_after=_safe_retry_after(response.headers.get("Retry-After")),
            )
        elif response.status_code < 200 or response.status_code >= 300:
            result = _RequestResult(_RequestOutcome.UNAVAILABLE, trace_entry)
        elif (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != _JSON_CONTENT_TYPE
        ):
            result = _RequestResult(_RequestOutcome.INVALID, trace_entry)
        else:
            try:
                payload = response.json()
            except ValueError:
                result = _RequestResult(_RequestOutcome.INVALID, trace_entry)
            else:
                result = (
                    _RequestResult(
                        _RequestOutcome.SUCCESS,
                        trace_entry,
                        cast(dict[str, object], payload),
                    )
                    if type(payload) is dict
                    else _RequestResult(_RequestOutcome.INVALID, trace_entry)
                )
    except BaseException:
        try:
            response.close()
        except Exception:
            pass
        raise
    cleanup_failed = False
    try:
        response.close()
    except Exception:
        cleanup_failed = True
    if cleanup_failed and result.outcome is _RequestOutcome.SUCCESS:
        result = _RequestResult(_RequestOutcome.UNAVAILABLE, trace_entry)
    del client, definition, operation, parameters, payload, query, response
    return result


def _safe_retry_after(value: object) -> int | None:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    normalized = value.lstrip("0") or "0"
    if len(normalized) > 3:
        return None
    parsed = int(normalized)
    return parsed if parsed <= 900 else None


def _validate_parameters(query: ParameterPairs, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(query, Sequence) or len(query) != len(allowed):
        raise InvalidSourceResponseError()
    result: dict[str, str] = {}
    for item in query:
        if not isinstance(item, tuple) or len(item) != 2:
            raise InvalidSourceResponseError()
        key, value = item
        if (
            type(key) is not str
            or type(value) is not str
            or key not in allowed
            or not value
            or len(value) > _MAX_PARAMETER_LENGTH
            or key in result
        ):
            raise InvalidSourceResponseError()
        result[key] = value
    if set(result) != set(allowed):
        raise InvalidSourceResponseError()
    return result


__all__ = ["TicketmasterOperation", "TicketmasterTransport"]
