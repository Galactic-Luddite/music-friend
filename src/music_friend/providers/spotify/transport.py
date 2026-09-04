"""Deny-by-default HTTP transport for fixed Spotify read operations."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

import httpx

from music_friend.errors import (
    AdditionalScopeRequiredError,
    AuthenticationRequiredError,
    InvalidSourceResponseError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)

_ACCOUNTS_ORIGIN = "https://accounts.spotify.com"
_API_ORIGIN = "https://api.spotify.com"
_MAX_BODY_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_CALL_SECONDS = 45.0
_MAX_PARAMETER_COUNT = 16
_MAX_PARAMETER_LENGTH = 4096
_MAX_TRACE_ENTRIES = 128
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_JSON_CONTENT_TYPE = "application/json"
_INVALID_JSON = object()
_HTTPX_FAILURES = (httpx.RequestError, httpx.StreamError)

ParameterPairs: TypeAlias = Sequence[tuple[str, str]]
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class SpotifyOperation(str, Enum):
    """The complete set of HTTP operations available to the Spotify adapter."""

    TOKEN = "token"
    HEALTH = "health"
    SEARCH_ARTISTS = "search_artists"
    FOLLOWED_ARTISTS = "followed_artists"
    SAVED_TRACKS = "saved_tracks"
    TOP_TRACKS = "top_tracks"
    TOP_ARTISTS = "top_artists"
    ARTIST_RELEASES = "artist_releases"


class _TransportFailure(str, Enum):
    HTTPX = "httpx_failure"


class _RateLimitKind(str, Enum):
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"


@dataclass(frozen=True, slots=True)
class _RequestDefinition:
    method: str
    origin: str
    path: str
    query_keys: frozenset[str] = frozenset()
    form_keys: frozenset[str] = frozenset()
    fixed_query: tuple[tuple[str, str], ...] = ()


_REQUESTS: Mapping[SpotifyOperation, _RequestDefinition] = MappingProxyType(
    {
        SpotifyOperation.TOKEN: _RequestDefinition(
            "POST",
            _ACCOUNTS_ORIGIN,
            "/api/token",
            form_keys=frozenset(
                {
                    "client_id",
                    "grant_type",
                    "code",
                    "redirect_uri",
                    "code_verifier",
                    "refresh_token",
                }
            ),
        ),
        SpotifyOperation.HEALTH: _RequestDefinition("GET", _API_ORIGIN, "/v1/me"),
        SpotifyOperation.SEARCH_ARTISTS: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/search",
            query_keys=frozenset({"q", "limit"}),
            fixed_query=(("type", "artist"),),
        ),
        SpotifyOperation.FOLLOWED_ARTISTS: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/me/following",
            query_keys=frozenset({"limit", "after"}),
            fixed_query=(("type", "artist"),),
        ),
        SpotifyOperation.SAVED_TRACKS: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/me/tracks",
            query_keys=frozenset({"limit", "offset"}),
        ),
        SpotifyOperation.TOP_TRACKS: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/me/top/tracks",
            query_keys=frozenset({"time_range", "limit"}),
        ),
        SpotifyOperation.TOP_ARTISTS: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/me/top/artists",
            query_keys=frozenset({"time_range", "limit"}),
        ),
        SpotifyOperation.ARTIST_RELEASES: _RequestDefinition(
            "GET",
            _API_ORIGIN,
            "/v1/artists/{artist_id}/albums",
            query_keys=frozenset({"artist_id", "include_groups", "limit", "offset"}),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    """A validated JSON object returned by one fixed operation."""

    data: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _TraceEntry:
    operation: str
    query_keys: tuple[str, ...]
    form_keys: tuple[str, ...]


class SpotifyTransport:
    """Execute the closed Spotify operation table through one injected connector."""

    def __init__(
        self,
        connector: httpx.BaseTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(connector, httpx.BaseTransport):
            raise ValueError("connector must be an httpx.BaseTransport")
        self._client = httpx.Client(
            transport=connector,
            trust_env=False,
            follow_redirects=False,
        )
        self._clock = clock
        self._trace: list[_TraceEntry] = []

    def __enter__(self) -> SpotifyTransport:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned HTTP client and its injected connector."""
        self._client.close()

    @property
    def trace(self) -> tuple[_TraceEntry, ...]:
        """Return a bounded trace containing no parameter or credential values."""
        return tuple(self._trace)

    def execute(
        self,
        operation: SpotifyOperation,
        *,
        query: ParameterPairs = (),
        form: ParameterPairs = (),
        access_token: str | None = None,
        deadline: float | None = None,
    ) -> _TransportResponse:
        """Execute one fixed request with bounded time, retries, and response parsing."""
        if type(operation) is not SpotifyOperation:
            raise InvalidSourceResponseError()
        definition = _REQUESTS[operation]
        query_values = _validated_parameters(query, definition.query_keys)
        form_values = _validated_parameters(form, definition.form_keys)
        path = definition.path
        if operation is SpotifyOperation.ARTIST_RELEASES:
            artist_id = query_values.pop("artist_id", None)
            if artist_id is None or _SPOTIFY_ID.fullmatch(artist_id) is None:
                raise InvalidSourceResponseError()
            path = path.format(artist_id=artist_id)
        _validate_destination(definition.origin, path)
        _validate_access_token(definition, access_token)

        self._trace.append(
            _TraceEntry(operation.value, tuple(sorted(query_values)), tuple(sorted(form_values)))
        )
        if len(self._trace) > _MAX_TRACE_ENTRIES:
            del self._trace[:-_MAX_TRACE_ENTRIES]
        hard_deadline = self._call_deadline()
        if deadline is not None:
            if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                raise InvalidSourceResponseError()
            if not math.isfinite(float(deadline)):
                raise InvalidSourceResponseError()
            hard_deadline = min(hard_deadline, float(deadline))

        attempts = 2 if definition.method == "GET" else 1
        failure_reason: _TransportFailure | None = None
        for attempt in range(attempts):
            remaining = hard_deadline - self._clock()
            if remaining <= 0:
                raise SourceUnavailableError()
            invalid_url = False
            try:
                status, headers, body = self._request_once(
                    definition,
                    path,
                    query_values,
                    form_values,
                    access_token,
                    remaining,
                )
            except httpx.InvalidURL:
                invalid_url = True
            except _HTTPX_FAILURES:
                if attempt + 1 < attempts:
                    continue
                failure_reason = _TransportFailure.HTTPX
                break

            if invalid_url:
                raise InvalidSourceResponseError() from None

            if 500 <= status <= 599 and attempt + 1 < attempts:
                continue
            return _map_response(status, headers, body)
        if failure_reason is _TransportFailure.HTTPX:
            raise SourceUnavailableError()
        raise SourceUnavailableError()

    def _call_deadline(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SourceUnavailableError()
        started = float(value)
        if not math.isfinite(started):
            raise SourceUnavailableError()
        return started + _MAX_CALL_SECONDS

    def _request_once(
        self,
        definition: _RequestDefinition,
        path: str,
        query: Mapping[str, str],
        form: Mapping[str, str],
        access_token: str | None,
        remaining: float,
    ) -> tuple[int, httpx.Headers, bytes]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        params = (*definition.fixed_query, *query.items())
        timeout = httpx.Timeout(
            remaining, connect=remaining, read=remaining, write=remaining, pool=remaining
        )
        with self._client.stream(
            definition.method,
            definition.origin + path,
            params=params,
            data=form or None,
            headers=headers,
            follow_redirects=False,
            timeout=timeout,
        ) as response:
            status = response.status_code
            if 300 <= status <= 399:
                raise InvalidSourceResponseError()
            if status == 401:
                raise AuthenticationRequiredError()
            if status == 403:
                raise AdditionalScopeRequiredError()
            if status == 429:
                rate_limit_kind = _classify_rate_limit(response)
                if rate_limit_kind is _RateLimitKind.QUOTA_EXHAUSTED:
                    raise QuotaExhaustedError()
                raise RateLimitedError(response.headers.get("Retry-After"))
            if 500 <= status <= 599:
                return status, response.headers, b""
            if not 200 <= status <= 299:
                raise InvalidSourceResponseError()
            _validate_content_type(response.headers)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    raise InvalidSourceResponseError()
                chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks)


def _validate_content_type(headers: httpx.Headers) -> None:
    values = headers.get_list("Content-Type")
    if len(values) != 1:
        raise InvalidSourceResponseError()
    parts = values[0].split(";")
    if not 1 <= len(parts) <= 2 or parts[0].strip().lower() != _JSON_CONTENT_TYPE:
        raise InvalidSourceResponseError()
    if len(parts) == 1:
        return
    name, separator, value = parts[1].partition("=")
    if separator != "=" or name.strip().lower() != "charset" or value.strip().lower() != "utf-8":
        raise InvalidSourceResponseError()


def _validated_parameters(values: ParameterPairs, allowed: frozenset[str]) -> dict[str, str]:
    if isinstance(values, (str, bytes)) or len(values) > _MAX_PARAMETER_COUNT:
        raise InvalidSourceResponseError()
    result: dict[str, str] = {}
    for pair in values:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise InvalidSourceResponseError()
        key, value = pair
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or key not in allowed
            or key in result
            or len(value) > _MAX_PARAMETER_LENGTH
        ):
            raise InvalidSourceResponseError()
        result[key] = value
    return result


def _validate_access_token(definition: _RequestDefinition, access_token: str | None) -> None:
    if definition.origin == _ACCOUNTS_ORIGIN:
        if access_token is not None:
            raise InvalidSourceResponseError()
        return
    if not isinstance(access_token, str) or not access_token or len(access_token) > 4096:
        raise AuthenticationRequiredError()


def _validate_destination(origin: str, path: str) -> None:
    if origin not in {_ACCOUNTS_ORIGIN, _API_ORIGIN}:
        raise InvalidSourceResponseError()
    approved_paths = {
        definition.path
        for definition in _REQUESTS.values()
        if "{" not in definition.path and definition.origin == origin
    }
    artist_prefix = "/v1/artists/"
    artist_suffix = "/albums"
    valid_artist_path = False
    if origin == _API_ORIGIN and path.startswith(artist_prefix) and path.endswith(artist_suffix):
        artist_id = path[len(artist_prefix) : -len(artist_suffix)]
        valid_artist_path = _SPOTIFY_ID.fullmatch(artist_id) is not None
    if path not in approved_paths and not valid_artist_path:
        raise InvalidSourceResponseError()


def _map_response(status: int, headers: httpx.Headers, body: bytes) -> _TransportResponse:
    if 300 <= status <= 399:
        raise InvalidSourceResponseError()
    if status == 401:
        raise AuthenticationRequiredError()
    if status == 403:
        raise AdditionalScopeRequiredError()
    if status == 429:
        raise RateLimitedError(headers.get("Retry-After"))
    if 500 <= status <= 599:
        raise SourceUnavailableError()
    if not 200 <= status <= 299:
        raise InvalidSourceResponseError()
    parsed = _decode_json(body)
    if parsed is _INVALID_JSON:
        raise InvalidSourceResponseError()
    if not isinstance(parsed, dict):
        raise InvalidSourceResponseError()
    return _TransportResponse(parsed)


def _classify_rate_limit(response: httpx.Response) -> _RateLimitKind:
    encoding = response.headers.get("Content-Encoding")
    if encoding is not None and encoding.lower().strip() not in {"", "identity"}:
        return _RateLimitKind.RATE_LIMITED
    try:
        if response.is_stream_consumed:
            body = response.content
            if len(body) > _MAX_BODY_BYTES:
                return _RateLimitKind.RATE_LIMITED
        else:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_raw():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    return _RateLimitKind.RATE_LIMITED
                chunks.append(chunk)
            body = b"".join(chunks)
    except _HTTPX_FAILURES:
        return _RateLimitKind.RATE_LIMITED
    parsed = _decode_json(body)
    if _is_quota_response(parsed):
        return _RateLimitKind.QUOTA_EXHAUSTED
    return _RateLimitKind.RATE_LIMITED


def _is_quota_response(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"error"}:
        return False
    error = value["error"]
    return isinstance(error, dict) and error.get("reason") == "QUOTA_EXCEEDED"


def _decode_json(body: bytes) -> object:
    try:
        parsed: object = json.loads(
            body.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        _validate_json(parsed, depth=0)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return _INVALID_JSON
    return parsed


def _validate_json(value: object, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON nesting is too deep")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_json(item, depth=depth + 1)
        return
    raise ValueError("unsupported JSON value")


__all__ = ["SpotifyOperation", "SpotifyTransport"]
