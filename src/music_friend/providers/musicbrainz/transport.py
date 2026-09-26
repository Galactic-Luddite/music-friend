"""HTTP transport for MusicBrainz Web Service API."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

import httpx

from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError

_BASE_URL = "https://musicbrainz.org/ws/2/"
_MAX_BODY_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_CALL_SECONDS = 45.0
_MIN_REQUEST_INTERVAL = 1.0  # 1 request per second

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class _PacingState:
    """Track monotonic time for local 1 req/s pacing."""

    last_request_time: float


class MusicBrainzTransport:
    """Keyless, rate-limited HTTP transport for MusicBrainz Web Service."""

    def __init__(self, *, user_agent: str) -> None:
        if type(user_agent) is not str or not user_agent.strip():
            raise ValueError("user_agent must be a non-empty string")
        self._user_agent = user_agent.strip()
        self._pacing: _PacingState | None = None
        self._client = httpx.Client(timeout=_MAX_CALL_SECONDS)

    def get(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> JsonValue:
        """Make a GET request and return parsed JSON, enforcing local 1 req/s pacing."""
        if type(path) is not str:
            raise ValueError("path must be a string")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("query must be a Mapping")

        # Enforce local 1 req/s pacing
        if self._pacing is not None:
            elapsed = time.monotonic() - self._pacing.last_request_time
            if elapsed < _MIN_REQUEST_INTERVAL:
                time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        url = _BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        params = dict(query) if query else {}
        params["fmt"] = "json"

        try:
            response = self._client.get(
                url,
                params=params,
                headers={"Accept": "application/json", "User-Agent": self._user_agent},
            )
        except (httpx.RequestError, httpx.StreamError) as error:
            raise SourceUnavailableError() from error
        finally:
            # Record timing for next pacing calculation
            self._pacing = _PacingState(time.monotonic())

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers)
            raise RateLimitedError(retry_after_seconds=retry_after)

        if response.status_code == 503:
            retry_after = _parse_retry_after(response.headers)
            raise RateLimitedError(retry_after_seconds=retry_after)

        if response.status_code != 200:
            raise SourceUnavailableError()

        try:
            if len(response.content) > _MAX_BODY_BYTES:
                raise InvalidSourceResponseError()
            return json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidSourceResponseError() from error

    def close(self) -> None:
        """Clean up HTTP client."""
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> MusicBrainzTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
    """Parse Retry-After header, defaulting to 5 seconds if absent or unparseable."""
    value = headers.get("retry-after")
    if not value:
        return 5
    try:
        return int(value)
    except ValueError:
        return 5


__all__ = ["MusicBrainzTransport"]
