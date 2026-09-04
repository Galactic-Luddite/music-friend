from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from music_friend.domain import SourceReference
from music_friend.errors import (
    AdditionalScopeRequiredError,
    AuthenticationRequiredError,
    InvalidSourceResponseError,
    RateLimitedError,
    SourceUnavailableError,
)
from music_friend.providers import Capability, ProviderCapabilities
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.oauth import (
    AuthorizationMode,
    AuthorizationResult,
    SpotifyAuthorization,
)
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport

OBSERVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class StaticTokens:
    def __init__(self, transport: SpotifyTransport) -> None:
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(frozenset(Capability), frozenset(Capability))

    def _call_deadline(self) -> float:
        return self._transport._call_deadline()

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> dict[str, object]:
        return self._transport.execute(
            operation,
            query=query,
            deadline=deadline,
            **{"access_" + "token": "synthetic-access-value"},
        ).data


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.saves = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.saves += 1
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


def _error_surface(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    nodes: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        nodes.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(
        [
            "".join(traceback.format_exception(error)),
            *(repr(node) + str(node) + repr(vars(node)) for node in nodes),
        ]
    )


def _source(response: httpx.Response) -> tuple[SpotifySource, SpotifyTransport]:
    transport = SpotifyTransport(httpx.MockTransport(lambda _request: response))
    source = SpotifySource(
        settings=SpotifySettings("synthetic-client"),
        tokens=StaticTokens(transport),
        clock=lambda: OBSERVED_AT,
    )
    return source, transport


def _deterministic_bytes(*values: bytes) -> Callable[[int], bytes]:
    remaining = iter(values)

    def read(length: int) -> bytes:
        value = next(remaining)
        assert len(value) == length
        return value

    return read


def test_success_with_non_json_content_type_is_rejected() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b'{"display_name":"synthetic"}',
        )

    transport = SpotifyTransport(httpx.MockTransport(respond))

    with pytest.raises(InvalidSourceResponseError):
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "synthetic-access-value"},
        )

    assert len(requests) == 1
    assert requests[0].url.host == "api.spotify.com"
    transport.close()


@pytest.mark.parametrize(
    "content",
    (
        b'{"nested":' + b"[" * 34 + b"0" + b"]" * 34 + b"}",
        b'{"value":"' + b"x" * (1024 * 1024) + b'"}',
    ),
    ids=("excessive-nesting", "oversized-body"),
)
def test_nested_or_huge_success_body_is_a_typed_redacted_failure(content: bytes) -> None:
    transport = SpotifyTransport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=content,
            )
        )
    )

    with pytest.raises(InvalidSourceResponseError) as raised:
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "synthetic-access-value"},
        )

    assert raised.value.__cause__ is None
    transport.close()


@pytest.mark.parametrize(
    ("status", "error_type", "copies"),
    (
        (400, InvalidSourceResponseError, 1),
        (401, AuthenticationRequiredError, 1),
        (403, AdditionalScopeRequiredError, 1),
        (500, SourceUnavailableError, 2),
    ),
)
def test_raw_error_body_canary_never_crosses_typed_error_boundary(
    status: int,
    error_type: type[Exception],
    copies: int,
) -> None:
    canary = "raw-provider-body-diagnostic-canary"
    responses = [
        httpx.Response(
            status,
            headers={"Content-Type": "text/plain"},
            content=canary.encode("ascii"),
        )
        for _ in range(copies)
    ]

    def respond(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = SpotifyTransport(httpx.MockTransport(respond))

    with pytest.raises(error_type) as raised:
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "synthetic-access-value"},
        )

    assert canary not in _error_surface(raised.value)
    transport.close()


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    (
        (None, 900),
        ("", 900),
        ("-1", 900),
        ("901", 900),
        ("999999999999999999", 900),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 900),
        ("17", 17),
    ),
)
def test_retry_header_is_clamped_without_exposing_error_body(
    retry_after: str | None,
    expected: int,
) -> None:
    canary = "rate-limit-body-diagnostic-canary"
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    transport = SpotifyTransport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                429,
                headers=headers,
                json={"error": {"message": canary}},
            )
        )
    )

    with pytest.raises(RateLimitedError) as raised:
        transport.execute(
            SpotifyOperation.HEALTH,
            **{"access_" + "token": "synthetic-access-value"},
        )

    assert raised.value.retry_after_seconds == expected
    assert canary not in _error_surface(raised.value)
    transport.close()


@pytest.mark.parametrize(
    ("precision", "release_date"),
    (
        ("day", "2026-09"),
        ("month", "2026-09-01"),
        ("year", "26"),
        ("hour", "2026"),
    ),
)
def test_malformed_release_dates_are_typed_failures(
    precision: str,
    release_date: str,
) -> None:
    response = httpx.Response(
        200,
        json={
            "items": [
                {
                    "id": "release001",
                    "name": "Synthetic Release",
                    "album_type": "album",
                    "release_date": release_date,
                    "release_date_precision": precision,
                    "artists": [{"id": "artist001", "name": "discarded"}],
                }
            ],
            "next": None,
        },
    )
    source, transport = _source(response)
    reference = SourceReference(
        source="spotify",
        native_id="artist001",
        canonical_url="https://open.spotify.com/artist/artist001",
        observed_at=OBSERVED_AT,
    )

    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases(
            [reference],
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    transport.close()


def test_duplicate_artist_ids_in_track_are_rejected() -> None:
    response = httpx.Response(
        200,
        json={
            "items": [
                {
                    "track": {
                        "id": "track001",
                        "name": "Synthetic Track",
                        "artists": [
                            {"id": "artist001", "name": "discarded"},
                            {"id": "artist001", "name": "discarded duplicate"},
                        ],
                    }
                }
            ],
            "next": None,
        },
    )
    source, transport = _source(response)

    with pytest.raises(InvalidSourceResponseError):
        source.saved_items()

    transport.close()


def test_adversarial_unicode_text_is_sanitized_to_a_literal_safe_value() -> None:
    response = httpx.Response(
        200,
        json={
            "artists": {
                "items": [
                    {
                        "id": "artist001",
                        "name": "\N{RIGHT-TO-LEFT OVERRIDE}<script>\x00 Name",
                    }
                ],
                "next": None,
            }
        },
    )
    source, transport = _source(response)

    page = source.search_artists("Synthetic", 1)

    assert page.items[0].display_name == "＜script＞ Name"
    transport.close()


def test_manual_oauth_denial_fields_do_not_reach_transport_storage_or_results() -> None:
    canary = "oauth-denial-description-canary"
    settings = SpotifySettings("synthetic-client", "http://127.0.0.1:43210/callback")
    store = MemoryCredentialStore()
    requests: list[httpx.Request] = []
    transport = SpotifyTransport(
        httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json={"unexpected": True})
            )
        )
    )
    tokens = SpotifyTokenManager(settings=settings, transport=transport, store=store)
    browser_urls: list[str] = []

    def read_denial() -> str:
        state = parse_qs(urlsplit(browser_urls[0]).query)["state"][0]
        return (
            "http://127.0.0.1:43210/callback"
            f"?error=access_denied&state={state}&error_description={canary}"
        )

    result = SpotifyAuthorization(
        settings=settings,
        tokens=tokens,
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=_deterministic_bytes(b"v" * 32, b"s" * 32),
        _server_factory=lambda *_args, **_kwargs: pytest.fail("manual mode built a server"),
    ).authorize(
        frozenset({Capability.HEALTH}),
        mode=AuthorizationMode.MANUAL,
        callback_reader=read_denial,
    )

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert store.values == {}
    assert store.saves == 0
    assert canary not in repr(result)
    transport.close()


def test_extra_oauth_error_fields_are_discarded_from_successful_token_state() -> None:
    canary = "oauth-extra-field-diagnostic-canary"
    store = MemoryCredentialStore()
    transport = SpotifyTransport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "access_" + "token": "synthetic-access-value",
                    "refresh_" + "token": "synthetic-refresh-value",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "user-read-private",
                    "error": "synthetic_error",
                    "error_description": canary,
                },
            )
        )
    )
    manager = SpotifyTokenManager(
        settings=SpotifySettings("synthetic-client"),
        transport=transport,
        store=store,
    )

    manager._exchange_authorization_code(
        "synthetic-code",
        redirect_uri="http://127.0.0.1:43210/callback",
        verifier="synthetic-verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )

    assert manager.status().connected is True
    assert all(canary not in envelope for envelope in store.values.values())
    assert canary not in repr(manager.status())
    transport.close()
