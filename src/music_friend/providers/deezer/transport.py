"""HTTP transport for the keyless Deezer public API.

Verified live against the real Deezer API on 2026-09-26 (recon for issue #42, not
part of the automated test suite): ``GET https://api.deezer.com/artist/27/albums?limit=5``
(a real public artist, Daft Punk) returned a paginated body shaped like::

    {"data": [{"id": 494309801, "title": "...", "link": "https://www.deezer.com/album/...",
               "cover": "...", "cover_small": "...", "cover_medium": "...", "cover_big": "...",
               "cover_xl": "...", "md5_image": "...", "genre_id": 106, "fans": 14840,
               "release_date": "2023-11-17", "record_type": "album",
               "tracklist": "https://api.deezer.com/album/.../tracks",
               "explicit_lyrics": false, "type": "album"}, ...],
     "total": 38, "next": "https://api.deezer.com/artist/27/albums?limit=2&index=2"}

An invalid/nonexistent artist id returned ``{"data": [], "total": 0}`` -- an empty
result, not an HTTP error. No ``quota_exceeded`` body was observed live in that
session even after 60 rapid concurrent requests, so the mapping below (a JSON
error body shaped ``{"error": {"type": "...", "message": "...", "code": N}}`` with
``type`` containing ``quota_exceeded`` mapping to ``RateLimitedError``) follows the
design doc's documented Deezer error contract rather than an independently observed
live error response; treat this one mapping as design-doc-sourced, not recon-verified.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import TypeAlias

import httpx

from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError

_BASE_URL = "https://api.deezer.com/"
_MAX_BODY_BYTES = 1024 * 1024
_MAX_CALL_SECONDS = 45.0

#: Local pacing budget: no more than this many requests in any rolling window.
_MAX_REQUESTS_PER_WINDOW = 10
_WINDOW_SECONDS = 5.0

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class DeezerTransport:
    """Keyless, locally paced HTTP transport for the Deezer public API."""

    def __init__(
        self,
        *,
        connector: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleeper
        self._request_times: deque[float] = deque()
        self._client = (
            httpx.Client(timeout=_MAX_CALL_SECONDS)
            if connector is None
            else httpx.Client(transport=connector, timeout=_MAX_CALL_SECONDS)
        )

    def get(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> JsonValue:
        """Make a GET request and return parsed JSON, enforcing local 10-req/5s pacing."""
        if type(path) is not str:
            raise ValueError("path must be a string")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("query must be a Mapping")
        if query is not None:
            for value in query.values():
                if type(value) is not str:
                    raise ValueError("query values must be str")

        self._wait_for_pacing_slot()

        url = _BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        params = dict(query) if query else {}

        try:
            response = self._client.get(url, params=params, headers={"Accept": "application/json"})
        except (httpx.RequestError, httpx.StreamError) as error:
            raise SourceUnavailableError() from error
        finally:
            self._request_times.append(self._clock())

        if response.status_code == 429:
            raise RateLimitedError(retry_after_seconds=_parse_retry_after(response.headers))

        if response.status_code != 200:
            raise SourceUnavailableError()

        try:
            if len(response.content) > _MAX_BODY_BYTES:
                raise InvalidSourceResponseError()
            result = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidSourceResponseError() from error

        _raise_for_error_body(result)
        return result  # type: ignore[no-any-return]

    def _wait_for_pacing_slot(self) -> None:
        now = self._clock()
        while self._request_times and now - self._request_times[0] >= _WINDOW_SECONDS:
            self._request_times.popleft()
        if len(self._request_times) >= _MAX_REQUESTS_PER_WINDOW:
            wait_seconds = _WINDOW_SECONDS - (now - self._request_times[0])
            if wait_seconds > 0:
                self._sleep(wait_seconds)
            now = self._clock()
            while self._request_times and now - self._request_times[0] >= _WINDOW_SECONDS:
                self._request_times.popleft()

    def close(self) -> None:
        """Clean up HTTP client."""
        self._client.close()

    def __enter__(self) -> DeezerTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _raise_for_error_body(result: object) -> None:
    """Map Deezer's documented in-body error shape to a typed error.

    Deezer returns HTTP 200 with ``{"error": {"type": "...", "message": "...",
    "code": N}}`` for several API-level failures rather than always using HTTP
    status codes. ``type`` containing ``quota_exceeded`` (case-insensitive) maps
    to ``RateLimitedError``; any other in-body error maps to
    ``InvalidSourceResponseError`` since it is not a shape this adapter knows how
    to recover from. This mapping is design-doc-sourced (see module docstring);
    it was not independently reproduced against a live 429/quota response.
    """
    if not isinstance(result, Mapping):
        return
    error = result.get("error")
    if not isinstance(error, Mapping):
        return
    error_type = error.get("type")
    if isinstance(error_type, str):
        normalized = error_type.lower().replace("_", "")
        if "quota" in normalized and "exceed" in normalized:
            raise RateLimitedError(retry_after_seconds=5)
    raise InvalidSourceResponseError()


def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("retry-after")
    if not value:
        return 5
    try:
        return int(value)
    except ValueError:
        return 5


__all__ = ["DeezerTransport"]
