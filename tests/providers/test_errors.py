"""Behavioral tests for stable, redacted public Music Friend errors."""

from __future__ import annotations

import pytest

from music_friend.errors import (
    AdditionalScopeRequiredError,
    AmbiguousIdentityError,
    AuthenticationRequiredError,
    CapabilityUnsupportedError,
    CatalogUnavailableError,
    InvalidSourceResponseError,
    MusicFriendError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)

PUBLIC_ERRORS = (
    MusicFriendError,
    AuthenticationRequiredError,
    AdditionalScopeRequiredError,
    CapabilityUnsupportedError,
    SourceUnavailableError,
    QuotaExhaustedError,
    AmbiguousIdentityError,
    InvalidSourceResponseError,
    CatalogUnavailableError,
)


def test_public_errors_serialize_only_safe_class_owned_fields() -> None:
    """Catches a public error response that exposes diagnostic input."""
    diagnostic = {
        "response_body": "access_" + "token=secret",
        "callback": "https://example.test/callback?code=secret",
        "path": "/" + "Users/alice/private/catalog.sqlite",
    }

    for error_type in PUBLIC_ERRORS:
        error = error_type(diagnostic)

        assert error.to_public_dict() == {
            "category": error_type.CATEGORY,
            "message": error_type.PUBLIC_MESSAGE,
        }
        assert str(error) == error_type.PUBLIC_MESSAGE
        assert repr(error) == f"{error_type.__name__}()"


@pytest.mark.parametrize(
    ("value", "expected", "is_exact"),
    [
        (0, 0, True),
        (900, 900, True),
        ("0015", 15, True),
        ("900", 900, True),
        (True, 900, False),
        (-1, 900, False),
        ("-1", 900, False),
        (" 15", 900, False),
        ("15.0", 900, False),
        ("ninety", 900, False),
        (901, 900, False),
        ("901", 900, False),
        ("9" * 901, 900, False),
        ("9" * 5000, 900, False),
        (None, 900, False),
    ],
)
def test_rate_limit_retry_values_are_strictly_bounded(
    value: object, expected: int, is_exact: bool
) -> None:
    """Catches unsafe retry parsing that accepts malformed or unbounded provider input."""
    error = RateLimitedError(value)

    assert error.retry_after_seconds == expected
    assert error.retry_after_is_exact is is_exact
    assert error.to_public_dict() == {
        "category": "rate_limited",
        "message": RateLimitedError.PUBLIC_MESSAGE,
        "retry_after_seconds": expected,
    }


def test_rate_limited_error_never_leaks_response_or_local_context() -> None:
    """Catches rate-limit formatting that includes raw provider context or credentials."""
    authorization = "Authori" + "zation: Bearer very-secret "
    home_path = "/" + "Users/alice/private"
    response_body = authorization + home_path + " https://x.test/?token=1"
    error = RateLimitedError(response_body)

    assert response_body not in str(error)
    assert response_body not in repr(error)
    assert response_body not in str(error.to_public_dict())
    assert repr(error) == "RateLimitedError()"
