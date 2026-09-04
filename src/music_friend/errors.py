"""Stable, redacted public errors for Music Friend boundaries."""

from __future__ import annotations

import re
from typing import ClassVar


class MusicFriendError(Exception):
    """Base class for errors that may safely cross a Music Friend boundary."""

    CATEGORY: ClassVar[str] = "music_friend_error"
    PUBLIC_MESSAGE: ClassVar[str] = "Music Friend could not complete the request."

    def __init__(self, *_diagnostic_context: object) -> None:
        """Discard diagnostic inputs; preserve causes with ``raise ... from`` instead."""
        super().__init__()

    def __str__(self) -> str:
        return self.PUBLIC_MESSAGE

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def to_public_dict(self) -> dict[str, object]:
        """Return the complete safe public representation of this error."""
        return {"category": self.CATEGORY, "message": self.PUBLIC_MESSAGE}


class AuthenticationRequiredError(MusicFriendError):
    CATEGORY = "authentication_required"
    PUBLIC_MESSAGE = "Authentication is required for this source."


class AdditionalScopeRequiredError(MusicFriendError):
    CATEGORY = "additional_scope_required"
    PUBLIC_MESSAGE = "Additional authorization is required for this capability."


class CapabilityUnsupportedError(MusicFriendError):
    CATEGORY = "capability_unsupported"
    PUBLIC_MESSAGE = "This source does not support the requested capability."


class SourceUnavailableError(MusicFriendError):
    CATEGORY = "source_unavailable"
    PUBLIC_MESSAGE = "This source is temporarily unavailable."


class RateLimitedError(MusicFriendError):
    CATEGORY = "rate_limited"
    PUBLIC_MESSAGE = "This source is rate limited."

    def __init__(self, retry_after_seconds: object = None) -> None:
        super().__init__()
        self.retry_after_seconds, self.retry_after_is_exact = _parse_retry_after_seconds(
            retry_after_seconds
        )

    def to_public_dict(self) -> dict[str, object]:
        public = super().to_public_dict()
        public["retry_after_seconds"] = self.retry_after_seconds
        return public


class QuotaExhaustedError(MusicFriendError):
    CATEGORY = "quota_exhausted"
    PUBLIC_MESSAGE = "This source quota is exhausted."


class AmbiguousIdentityError(MusicFriendError):
    CATEGORY = "ambiguous_identity"
    PUBLIC_MESSAGE = "The source identity is ambiguous."


class InvalidSourceResponseError(MusicFriendError):
    CATEGORY = "invalid_source_response"
    PUBLIC_MESSAGE = "This source returned an invalid response."


class CatalogUnavailableError(MusicFriendError):
    CATEGORY = "catalog_unavailable"
    PUBLIC_MESSAGE = "The local catalog is unavailable."


_UNSIGNED_INTEGER = re.compile(r"[0-9]+\Z")
_MAX_RETRY_AFTER_SECONDS = 900


def _parse_retry_after_seconds(value: object) -> tuple[int, bool]:
    if isinstance(value, bool):
        return _MAX_RETRY_AFTER_SECONDS, False
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _UNSIGNED_INTEGER.fullmatch(value):
        normalized = value.lstrip("0") or "0"
        if len(normalized) > 3 or normalized > "900":
            return _MAX_RETRY_AFTER_SECONDS, False
        parsed = int(normalized)
    else:
        return _MAX_RETRY_AFTER_SECONDS, False
    if 0 <= parsed <= _MAX_RETRY_AFTER_SECONDS:
        return parsed, True
    return _MAX_RETRY_AFTER_SECONDS, False


__all__ = [
    "AdditionalScopeRequiredError",
    "AmbiguousIdentityError",
    "AuthenticationRequiredError",
    "CapabilityUnsupportedError",
    "CatalogUnavailableError",
    "InvalidSourceResponseError",
    "MusicFriendError",
    "QuotaExhaustedError",
    "RateLimitedError",
    "SourceUnavailableError",
]
