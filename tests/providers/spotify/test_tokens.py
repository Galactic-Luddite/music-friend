from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from music_friend.errors import (
    AuthenticationRequiredError,
    InvalidSourceResponseError,
    SourceUnavailableError,
)
from music_friend.providers import spotify
from music_friend.providers.credentials import CredentialKey, CredentialStoreError
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import _decode_credential
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.fail_save = False

    def save(self, key: CredentialKey, value: str) -> None:
        if self.fail_save:
            raise CredentialStoreError()
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


class Responses:
    def __init__(self, values: list[httpx.Response]) -> None:
        self.values = values
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.values.pop(0)


def _manager(
    responses: Responses,
    store: MemoryStore,
    now: list[float],
) -> SpotifyTokenManager:
    return SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=SpotifyTransport(httpx.MockTransport(responses)),
        store=store,
        clock=lambda: now[0],
    )


def _token_response(
    *,
    access: str = "access-value",
    refresh: str | None = "refresh-value",
    token_type: object = "Bearer",
    expires: object = 3600,
    scope: str | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {
        "access_" + "token": access,
        "token_type": token_type,
        "expires_in": expires,
    }
    if refresh is not None:
        payload["refresh_token"] = refresh
    if scope is not None:
        payload["scope"] = scope
    return httpx.Response(200, json=payload)


def test_code_exchange_persists_exact_grant_without_returning_token_payload() -> None:
    responses = Responses(
        [_token_response(scope="future-scope user-read-private", refresh="refresh-value")]
    )
    store = MemoryStore()
    manager = _manager(responses, store, [10.0])

    result = manager._exchange_authorization_code(
        "code-value",
        redirect_uri="http://127.0.0.1:43210/callback",
        verifier="verifier-value",
        granted_scopes=frozenset({"user-read-private", "user-top-read"}),
    )

    assert result is None
    form = parse_qs(responses.requests[0].content.decode("ascii"))
    assert form == {
        "client_id": ["client-a"],
        "grant_type": ["authorization_code"],
        "code": ["code-value"],
        "redirect_uri": ["http://127.0.0.1:43210/callback"],
        "code_verifier": ["verifier-value"],
    }
    stored = _decode_credential(next(iter(store.values.values())))
    assert stored.granted_scopes == frozenset({"future-scope", "user-read-private"})
    assert manager._access_token() == "access-value"


def test_access_expiry_triggers_exactly_one_safe_refresh() -> None:
    responses = Responses(
        [
            _token_response(expires=10),
            _token_response(access="new-access", refresh=None, expires=20),
        ]
    )
    store = MemoryStore()
    now = [100.0]
    manager = _manager(responses, store, now)
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )

    assert manager._access_token() == "access-value"
    now[0] = 110.0
    assert manager._access_token() == "new-access"
    assert len(responses.requests) == 2
    assert parse_qs(responses.requests[1].content.decode("ascii")) == {
        "client_id": ["client-a"],
        "grant_type": ["refresh_token"],
        "refresh_" + "token": ["refresh-value"],
    }


def test_refresh_rotation_persists_before_new_access_is_usable() -> None:
    responses = Responses(
        [
            _token_response(expires=1),
            _token_response(access="rotated-access", refresh="rotated-refresh"),
        ]
    )
    store = MemoryStore()
    now = [0.0]
    manager = _manager(responses, store, now)
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    store.fail_save = True
    now[0] = 2.0

    with pytest.raises(CredentialStoreError):
        manager._access_token()
    store.fail_save = False
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()


def test_omitted_refresh_token_preserves_prior_credential() -> None:
    responses = Responses(
        [_token_response(expires=1), _token_response(access="next", refresh=None)]
    )
    store = MemoryStore()
    now = [0.0]
    manager = _manager(responses, store, now)
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-top-read"}),
    )
    now[0] = 2.0

    assert manager._access_token() == "next"
    assert _decode_credential(next(iter(store.values.values()))).refresh_token == "refresh-value"


@pytest.mark.parametrize(
    "response",
    [
        _token_response(token_type="Basic"),
        _token_response(expires=0),
        _token_response(expires=True),
        _token_response(expires=1.5),
        _token_response(expires=86401),
        _token_response(access=""),
    ],
)
def test_invalid_token_fields_fail_closed(response: httpx.Response) -> None:
    manager = _manager(Responses([response]), MemoryStore(), [0.0])

    with pytest.raises(InvalidSourceResponseError):
        manager._exchange_authorization_code(
            "code",
            redirect_uri="http://127.0.0.1:1/callback",
            verifier="verifier",
            granted_scopes=frozenset({"user-read-private"}),
        )
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()


@pytest.mark.parametrize("status", [400, 401])
def test_refresh_authentication_failure_clears_access_without_leaking_body(status: int) -> None:
    canary = "response-body-canary"
    responses = Responses(
        [
            _token_response(expires=1),
            httpx.Response(status, json={"error": "invalid_grant", "detail": canary}),
        ]
    )
    store = MemoryStore()
    now = [0.0]
    manager = _manager(responses, store, now)
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    now[0] = 2.0

    with pytest.raises(AuthenticationRequiredError) as raised:
        manager._access_token()
    assert canary not in str(raised.value)
    assert canary not in repr(raised.value)
    assert len(responses.requests) == 2


def test_token_response_extra_fields_are_discarded() -> None:
    response = _token_response()
    response._content = json.dumps(
        {
            **json.loads(response.content),
            "account": "response-body-canary",
            "client_" + "secret": "secret-canary",
        }
    ).encode()
    manager = _manager(Responses([response]), MemoryStore(), [0.0])

    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )

    assert "response-body-canary" not in repr(manager.status())
    assert "secret-canary" not in repr(manager.status())


def test_duplicate_response_scopes_are_rejected() -> None:
    manager = _manager(
        Responses([_token_response(scope="user-top-read user-top-read")]),
        MemoryStore(),
        [0.0],
    )

    with pytest.raises(InvalidSourceResponseError):
        manager._exchange_authorization_code(
            "code",
            redirect_uri="http://127.0.0.1:1/callback",
            verifier="verifier",
            granted_scopes=frozenset({"user-top-read"}),
        )


def test_source_401_performs_one_safe_refresh_then_retries() -> None:
    responses = Responses(
        [
            _token_response(expires=3600),
            httpx.Response(401),
            _token_response(access="refreshed-access", refresh=None),
            httpx.Response(200, json={"id": "profile"}),
        ]
    )
    store = MemoryStore()
    now = [0.0]
    transport = SpotifyTransport(httpx.MockTransport(responses))
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=transport,
        store=store,
        clock=lambda: now[0],
    )
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    source = SpotifySource(
        settings=SpotifySettings("client-a"),
        tokens=manager,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    source.health()

    assert len(responses.requests) == 4


def test_source_call_deadline_uses_token_transport_epoch_across_401_refresh_and_retry() -> None:
    class MonotonicClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    transport_clock = MonotonicClock()
    transport_clock.value = 100.0
    token_clock = MonotonicClock()
    token_clock.value = 1_000_000.0
    unused_transport_clock = MonotonicClock()
    unused_transport_clock.value = 2_000_000.0

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return _token_response(expires=3600)
        if len(requests) == 2:
            transport_clock.value = 120.0
            return httpx.Response(401)
        if len(requests) == 3:
            transport_clock.value = 145.0
            return _token_response(access="refreshed-access", refresh=None)
        pytest.fail("source call exceeded its 45-second deadline")

    requests: list[httpx.Request] = []
    transport = SpotifyTransport(httpx.MockTransport(respond), clock=transport_clock)
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=transport,
        store=MemoryStore(),
        clock=token_clock,
    )
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    unused_candidate_transport = SpotifyTransport(
        httpx.MockTransport(lambda _request: pytest.fail("unused transport received a request")),
        clock=unused_transport_clock,
    )
    source = SpotifySource(
        settings=SpotifySettings("client-a"),
        tokens=manager,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(SourceUnavailableError):
        source.health()

    assert len(requests) == 3
    assert unused_candidate_transport.trace == ()


def test_public_token_and_package_surface_exposes_only_safe_operations() -> None:
    assert not hasattr(SpotifyTokenManager, "access_token")
    assert not hasattr(SpotifyTokenManager, "exchange_authorization_code")
    assert {
        "AuthorizationMode",
        "AuthorizationResult",
        "CredentialStatus",
        "SpotifyAuthorization",
        "SpotifySettings",
        "SpotifySource",
        "SpotifyTokenManager",
    } == set(spotify.__all__)


def test_source_401_after_expiry_refresh_clears_access_without_second_refresh() -> None:
    responses = Responses(
        [
            _token_response(expires=1),
            _token_response(access="refreshed-access", refresh=None),
            httpx.Response(401),
        ]
    )
    store = MemoryStore()
    now = [0.0]
    transport = SpotifyTransport(httpx.MockTransport(responses))
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=transport,
        store=store,
        clock=lambda: now[0],
    )
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    source = SpotifySource(
        settings=SpotifySettings("client-a"),
        tokens=manager,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    now[0] = 2.0

    with pytest.raises(AuthenticationRequiredError):
        source.health()
    assert len(responses.requests) == 3
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()


def test_token_manager_rejects_invalid_dependencies_and_credential_backend_failure() -> None:
    """Catches invalid token-manager wiring before a credential or network operation."""
    transport = SpotifyTransport(httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    store = MemoryStore()
    with pytest.raises(ValueError, match="settings"):
        SpotifyTokenManager(settings=object(), transport=transport, store=store)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transport"):
        SpotifyTokenManager(settings=SpotifySettings("client-a"), transport=object(), store=store)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="store"):
        SpotifyTokenManager(
            settings=SpotifySettings("client-a"), transport=transport, store=object()
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="clock"):
        SpotifyTokenManager(
            settings=SpotifySettings("client-a"),
            transport=transport,
            store=store,
            clock=None,  # type: ignore[arg-type]
        )

    class BrokenStore(MemoryStore):
        def load(self, key: CredentialKey) -> str | None:
            raise RuntimeError("backend diagnostic")

    with pytest.raises(CredentialStoreError):
        SpotifyTokenManager(
            settings=SpotifySettings("client-a"), transport=transport, store=BrokenStore()
        )


def test_code_exchange_requires_refresh_credential_and_valid_bounded_inputs() -> None:
    """Catches unusable authorization responses or unbounded private fields entering token state."""
    manager = _manager(Responses([_token_response(refresh=None)]), MemoryStore(), [0.0])
    with pytest.raises(InvalidSourceResponseError):
        manager._exchange_authorization_code(
            "code",
            redirect_uri="http://127.0.0.1:1/callback",
            verifier="verifier",
            granted_scopes=frozenset({"user-read-private"}),
        )
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()

    empty = _manager(Responses([]), MemoryStore(), [0.0])
    invalid_calls = (
        ("", "http://127.0.0.1:1/callback", "verifier", frozenset()),
        ("code", "", "verifier", frozenset()),
        ("code", "http://127.0.0.1:1/callback", "", frozenset()),
        ("code", "http://127.0.0.1:1/callback", "verifier", {"user-read-private"}),
    )
    for code, redirect, verifier, scopes in invalid_calls:
        with pytest.raises(InvalidSourceResponseError):
            empty._exchange_authorization_code(
                code,
                redirect_uri=redirect,
                verifier=verifier,
                granted_scopes=scopes,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("clock_value", (True, "bad", float("nan"), float("inf")))
def test_token_manager_rejects_invalid_clock_values(clock_value: object) -> None:
    """Catches an invalid monotonic clock producing unsafe access-token lifetime decisions."""
    manager = _manager(Responses([]), MemoryStore(), [clock_value])  # type: ignore[list-item]
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()


def test_token_operation_cannot_be_invoked_through_authenticated_source_boundary() -> None:
    """Catches a token endpoint operation being confused with an authenticated resource request."""
    manager = _manager(Responses([]), MemoryStore(), [0.0])
    with pytest.raises(InvalidSourceResponseError):
        manager._execute(SpotifyOperation.TOKEN, deadline=45.0)


def test_disconnect_clears_memory_even_when_credential_delete_fails() -> None:
    """Catches a failed local deletion leaving reusable in-memory access state."""

    class DeleteFailStore(MemoryStore):
        def delete(self, key: CredentialKey) -> None:
            raise CredentialStoreError()

    store = DeleteFailStore()
    manager = _manager(Responses([_token_response()]), store, [0.0])
    manager._exchange_authorization_code(
        "code",
        redirect_uri="http://127.0.0.1:1/callback",
        verifier="verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    with pytest.raises(CredentialStoreError):
        manager.disconnect()
    assert manager.status().connected is False
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()
