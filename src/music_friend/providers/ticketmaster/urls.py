"""Safe provider URL normalization for Ticketmaster event records."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "client_secret",
        "key",
        "password",
        "refresh_token",
        "token",
    }
)


def sanitize_ticketmaster_url(value: object) -> str | None:
    """Return a safe HTTPS URL with credential-like query parameters removed."""
    if type(value) is not str:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc or parsed.fragment:
        return None
    safe_query = urlencode(
        tuple(
            (key, query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
            if _normalized_query_key(key) not in _SENSITIVE_QUERY_KEYS
        )
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, ""))


def _normalized_query_key(value: str) -> str:
    return value.casefold().replace("-", "_")


__all__ = ["sanitize_ticketmaster_url"]
