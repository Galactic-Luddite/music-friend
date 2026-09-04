from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from music_friend.providers import Capability
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify import oauth as spotify_oauth
from music_friend.providers.spotify.callback import _CallbackOutcome
from music_friend.providers.spotify.config import SpotifySettings, load_spotify_settings
from music_friend.providers.spotify.oauth import (
    AuthorizationMode,
    AuthorizationResult,
    SpotifyAuthorization,
    _AttemptLifecycle,
    _AuthorizationAttempt,
    _build_authorization_url,
    _pkce_challenge,
    _pkce_verifier,
    _scope_for,
    _state_token,
)
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport

RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def _byte_source(*values: bytes) -> Callable[[int], bytes]:
    remaining = iter(values)

    def read(length: int) -> bytes:
        value = next(remaining)
        assert len(value) == length
        return value

    return read


def test_rfc_7636_s256_vector() -> None:
    assert _pkce_challenge(RFC_7636_VERIFIER) == RFC_7636_CHALLENGE


def test_generated_verifier_has_the_required_length_and_charset() -> None:
    verifier = _pkce_verifier(_byte_source(bytes(range(32))))

    assert len(verifier) == 43
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )


@pytest.mark.parametrize(
    "invalid",
    ("not-bytes", b"s" * 31, b"l" * 33),
    ids=("wrong-type", "short", "long"),
)
def test_entropy_helpers_reject_non_exact_32_byte_values(invalid: object) -> None:
    def entropy(_length: int) -> bytes:
        return invalid  # type: ignore[return-value]

    with pytest.raises(ValueError, match="entropy source must return exactly 32 bytes"):
        _pkce_verifier(entropy)
    with pytest.raises(ValueError, match="entropy source must return exactly 32 bytes"):
        _state_token(entropy)


def test_state_tokens_are_unique_for_distinct_deterministic_secrets() -> None:
    source = _byte_source(b"a" * 32, b"b" * 32)

    first = _state_token(source)
    second = _state_token(source)

    assert first != second
    assert len(first) == len(second) == 43


def test_authorization_rejects_identical_entropy_before_any_external_io() -> None:
    settings = SpotifySettings("client-id", "http://127.0.0.1:43210/callback")
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("code"))
    store = _MemoryStore()
    manager = SpotifyTokenManager(
        settings=settings,
        transport=_token_transport(requests),
        store=store,
    )
    store_counts = (store.loads, store.saves, store.deletes)
    authorizer = SpotifyAuthorization(
        settings=settings,
        tokens=manager,
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=_byte_source(b"x" * 32, b"x" * 32),
        _server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}), mode=AuthorizationMode.FIXED_LOOPBACK
    )

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert browser_urls == []
    assert servers.ports == []
    assert (store.loads, store.saves, store.deletes) == store_counts


def test_scope_union_is_exact_and_stable() -> None:
    capabilities = frozenset(
        {
            Capability.RECENT_RELEASES,
            Capability.TOP_ITEMS,
            Capability.SEARCH_ARTISTS,
            Capability.HEALTH,
            Capability.SAVED_ITEMS,
            Capability.FOLLOWED_ARTISTS,
        }
    )

    assert _scope_for(capabilities) == (
        "user-follow-read user-library-read user-read-private user-top-read"
    )


def test_authorization_url_uses_the_fixed_origin_path_and_pkce_only() -> None:
    url = _build_authorization_url(
        client_id="client-id",
        redirect_uri="http://127.0.0.1:43210/callback",
        state="state-value",
        verifier=RFC_7636_VERIFIER,
        scope="user-read-private user-top-read",
    )

    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) == (
        "https",
        "accounts.spotify.com",
        "/authorize",
        "",
    )
    assert query == {
        "client_id": ["client-id"],
        "code_challenge": [RFC_7636_CHALLENGE],
        "code_challenge_method": ["S256"],
        "redirect_uri": ["http://127.0.0.1:43210/callback"],
        "response_type": ["code"],
        "scope": ["user-read-private user-top-read"],
        "state": ["state-value"],
    }
    assert "client_secret" not in query
    assert RFC_7636_VERIFIER not in url


@pytest.mark.parametrize("terminal", [_AttemptLifecycle.COMPLETED, _AttemptLifecycle.FAILED])
def test_attempt_has_one_valid_lifecycle_path(terminal: _AttemptLifecycle) -> None:
    attempt = _AuthorizationAttempt(
        verifier="verifier-canary",
        state="state-canary",
        redirect_uri="http://127.0.0.1:43210/callback",
        authorization_url="https://accounts.spotify.com/authorize?canary",
    )

    assert attempt._lifecycle is _AttemptLifecycle.NEW
    attempt.wait()
    assert attempt._lifecycle is _AttemptLifecycle.WAITING
    if terminal is _AttemptLifecycle.COMPLETED:
        attempt.complete()
    else:
        attempt.fail()
    assert attempt._lifecycle is terminal
    attempt.close()
    assert attempt._lifecycle is _AttemptLifecycle.CLOSED
    assert attempt._verifier is None
    assert attempt._state is None
    assert attempt._redirect_uri is None
    assert attempt._authorization_url is None


def test_attempt_refuses_a_second_completion() -> None:
    attempt = _AuthorizationAttempt(
        verifier="verifier-canary",
        state="state-canary",
        redirect_uri="http://127.0.0.1:43210/callback",
        authorization_url="https://accounts.spotify.com/authorize?canary",
    )
    attempt.wait()
    attempt.complete()

    with pytest.raises(RuntimeError, match="authorization attempt is not waiting"):
        attempt.complete()


def test_attempt_repr_contains_only_class_and_lifecycle() -> None:
    attempt = _AuthorizationAttempt(
        verifier="verifier-canary",
        state="state-canary",
        redirect_uri="http://127.0.0.1:43210/callback",
        authorization_url="https://accounts.spotify.com/authorize?url-canary",
    )

    assert repr(attempt) == "<_AuthorizationAttempt state=NEW>"


def test_public_authorization_types_are_redacted_by_construction() -> None:
    result = AuthorizationResult(authorized=False, granted_capabilities=frozenset())

    assert AuthorizationMode.DYNAMIC_LOOPBACK.value == "dynamic_loopback"
    assert AuthorizationMode.FIXED_LOOPBACK.value == "fixed_loopback"
    assert AuthorizationMode.MANUAL.value == "manual"
    assert repr(result) == "AuthorizationResult(authorized=False, granted_capabilities=frozenset())"


class _AuthorizationServer:
    def __init__(self, port: int, outcome: _CallbackOutcome) -> None:
        self.server_address = ("127.0.0.1", 49152 if port == 0 else port)
        self._outcome = outcome
        self.timeout: float | None = None
        self.handle_count = 0
        self.close_count = 0

    def handle_request(self) -> None:
        self.handle_count += 1

    def server_close(self) -> None:
        self.close_count += 1


class _ServerFactory:
    def __init__(self, outcome: _CallbackOutcome) -> None:
        self._outcome = outcome
        self.ports: list[int] = []
        self.states: list[str] = []
        self.servers: list[_AuthorizationServer] = []

    def __call__(self, port: int, *, expected_state: str) -> _AuthorizationServer:
        self.ports.append(port)
        self.states.append(expected_state)
        server = _AuthorizationServer(port, self._outcome)
        self.servers.append(server)
        return server


def _token_transport(requests: list[httpx.Request]) -> SpotifyTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_" + "token": "access-token-canary",
                "refresh_" + "token": "refresh-token-canary",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    return SpotifyTransport(httpx.MockTransport(respond))


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.loads = 0
        self.saves = 0
        self.deletes = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.saves += 1
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        self.loads += 1
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.deletes += 1
        self.values.pop(key, None)


def _tokens(settings: SpotifySettings, requests: list[httpx.Request]) -> SpotifyTokenManager:
    return SpotifyTokenManager(
        settings=settings,
        transport=_token_transport(requests),
        store=_MemoryStore(),
    )


def _authorizer(
    *,
    settings: SpotifySettings,
    requests: list[httpx.Request],
    browser_urls: list[str],
    server_factory: _ServerFactory,
) -> SpotifyAuthorization:
    return SpotifyAuthorization(
        settings=settings,
        tokens=_tokens(settings, requests),
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=_byte_source(b"v" * 32, b"s" * 32),
        _server_factory=server_factory,
    )


def test_dynamic_loopback_binds_port_zero_before_opening_browser() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("authorization-code-canary"))
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH, Capability.TOP_ITEMS}),
        mode=AuthorizationMode.DYNAMIC_LOOPBACK,
    )

    assert result == AuthorizationResult(
        authorized=True,
        granted_capabilities=frozenset({Capability.HEALTH, Capability.TOP_ITEMS}),
    )
    assert servers.ports == [0]
    assert len(browser_urls) == 1
    assert parse_qs(urlsplit(browser_urls[0]).query)["redirect_uri"] == [
        "http://127.0.0.1:49152/callback"
    ]
    assert servers.servers[0].handle_count == 1
    assert servers.servers[0].close_count == 1
    assert servers.servers[0].timeout == 180.0


@pytest.mark.parametrize("authorization_client", ["client-b", " client-a "])
def test_authorization_rejects_mismatched_token_client_before_any_io(
    authorization_client: str,
) -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    server_calls: list[int] = []
    store = _MemoryStore()
    manager_settings = SpotifySettings("client-a", "http://127.0.0.1:43210/callback")
    manager = SpotifyTokenManager(
        settings=manager_settings,
        transport=_token_transport(requests),
        store=store,
    )
    store_calls_before = (store.loads, store.saves, store.deletes)

    with pytest.raises(ValueError) as raised:
        SpotifyAuthorization(
            settings=SpotifySettings(authorization_client, "http://127.0.0.1:43210/callback"),
            tokens=manager,
            browser_opener=lambda url: not browser_urls.append(url),
            _server_factory=lambda port, **_kwargs: server_calls.append(port),
        )

    assert str(raised.value) == "authorization settings do not match token manager"
    assert requests == []
    assert browser_urls == []
    assert server_calls == []
    assert (store.loads, store.saves, store.deletes) == store_calls_before


def test_fixed_loopback_binds_the_exact_validated_port() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("authorization-code-canary"))
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.SAVED_ITEMS}),
        mode=AuthorizationMode.FIXED_LOOPBACK,
    )

    assert result.authorized is True
    assert servers.ports == [43210]
    assert parse_qs(urlsplit(browser_urls[0]).query)["redirect_uri"] == [
        "http://127.0.0.1:43210/callback"
    ]


def test_authorize_accepts_a_regular_capability_set_and_freezes_the_result() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("authorization-code-canary"))
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        {Capability.HEALTH},
        mode=AuthorizationMode.FIXED_LOOPBACK,
    )

    assert result.granted_capabilities == frozenset({Capability.HEALTH})


def test_manual_mode_uses_only_the_injected_reader_and_binds_no_listener() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.invalid())
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )
    reader_calls = 0

    def read_callback() -> str:
        nonlocal reader_calls
        reader_calls += 1
        state = parse_qs(urlsplit(browser_urls[0]).query)["state"][0]
        return f"http://127.0.0.1:43210/callback?code=manual-code&state={state}"

    result = authorizer.authorize(
        frozenset({Capability.FOLLOWED_ARTISTS}),
        mode=AuthorizationMode.MANUAL,
        callback_reader=read_callback,
    )

    assert result.authorized is True
    assert reader_calls == 1
    assert servers.ports == []


def test_manual_mode_rejects_an_origin_form_callback() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.invalid())
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}),
        mode=AuthorizationMode.MANUAL,
        callback_reader=lambda: (
            "/callback?code=manual-code&state="
            + parse_qs(urlsplit(browser_urls[0]).query)["state"][0]
        ),
    )

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert servers.ports == []


def test_token_exchange_uses_only_the_fixed_pkce_form() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("authorization-code-canary"))
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}),
        mode=AuthorizationMode.FIXED_LOOPBACK,
    )

    assert result.authorized is True
    assert len(requests) == 1
    form = dict(parse_qsl(requests[0].content.decode("ascii"), keep_blank_values=True))
    assert form == {
        "client_id": "client-id",
        "grant_type": "authorization_code",
        "code": "authorization-code-canary",
        "redirect_uri": "http://127.0.0.1:43210/callback",
        "code_verifier": _pkce_verifier(_byte_source(b"v" * 32)),
    }
    assert "client_secret" not in form


@pytest.mark.parametrize(
    ("redirect_uri", "mode"),
    (
        ("http://localhost/callback", AuthorizationMode.DYNAMIC_LOOPBACK),
        ("http://127.0.0.1:43210/callback", AuthorizationMode.DYNAMIC_LOOPBACK),
        ("http://127.0.0.1/callback", AuthorizationMode.FIXED_LOOPBACK),
        ("http://localhost:43210/callback", AuthorizationMode.FIXED_LOOPBACK),
        ("http://127.0.0.1/callback", AuthorizationMode.MANUAL),
    ),
    ids=(
        "dynamic-localhost",
        "dynamic-port",
        "fixed-missing-port",
        "fixed-localhost",
        "manual-dynamic-uri",
    ),
)
def test_invalid_mode_and_redirect_combinations_fail_without_side_effects(
    redirect_uri: str | None, mode: AuthorizationMode
) -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("code"))
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", redirect_uri),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(frozenset({Capability.HEALTH}), mode=mode)

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert browser_urls == []
    assert servers.ports == []


def test_loaded_client_id_only_settings_drive_dynamic_authorization() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("code"))
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "client-id"})
    authorizer = _authorizer(
        settings=settings,
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}), mode=AuthorizationMode.DYNAMIC_LOOPBACK
    )

    assert result == AuthorizationResult(True, frozenset({Capability.HEALTH}))
    assert servers.ports == [0]
    assert parse_qs(urlsplit(browser_urls[0]).query)["redirect_uri"] == [
        "http://127.0.0.1:49152/callback"
    ]


@pytest.mark.parametrize("mode", (AuthorizationMode.FIXED_LOOPBACK, AuthorizationMode.MANUAL))
def test_loaded_client_id_only_settings_reject_non_dynamic_modes_without_io(
    mode: AuthorizationMode,
) -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("code"))
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "client-id"})
    authorizer = _authorizer(
        settings=settings,
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(frozenset({Capability.HEALTH}), mode=mode)

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert browser_urls == []
    assert servers.ports == []


def test_browser_open_failure_closes_without_exposing_the_url() -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(_CallbackOutcome.success("code"))
    authorizer = SpotifyAuthorization(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        tokens=_tokens(SpotifySettings("client-id", "http://127.0.0.1:43210/callback"), requests),
        browser_opener=lambda url: bool(browser_urls.append(url)),
        random_bytes=_byte_source(b"v" * 32, b"s" * 32),
        _server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}), mode=AuthorizationMode.FIXED_LOOPBACK
    )

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
    assert len(browser_urls) == 1
    assert servers.servers[0].handle_count == 0
    assert servers.servers[0].close_count == 1


def test_listener_close_failure_is_redacted_and_attempt_reaches_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostic = "listener-close-diagnostic-canary"
    attempts: list[_AuthorizationAttempt] = []
    original_attempt = _AuthorizationAttempt

    def record_attempt(**values: str) -> _AuthorizationAttempt:
        attempt = original_attempt(**values)
        attempts.append(attempt)
        return attempt

    class RaisingCloseServer(_AuthorizationServer):
        def server_close(self) -> None:
            self.close_count += 1
            raise OSError(diagnostic)

    def make_server(port: int, *, expected_state: str) -> RaisingCloseServer:
        return RaisingCloseServer(port, _CallbackOutcome.success("code"))

    monkeypatch.setattr(spotify_oauth, "_AuthorizationAttempt", record_attempt)
    requests: list[httpx.Request] = []
    authorizer = SpotifyAuthorization(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        tokens=_tokens(SpotifySettings("client-id", "http://127.0.0.1:43210/callback"), requests),
        browser_opener=lambda _url: True,
        random_bytes=_byte_source(b"v" * 32, b"s" * 32),
        _server_factory=make_server,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}), mode=AuthorizationMode.FIXED_LOOPBACK
    )
    captured = capsys.readouterr()

    assert result == AuthorizationResult(False, frozenset())
    assert attempts[0]._lifecycle is _AttemptLifecycle.CLOSED
    assert diagnostic not in captured.out + captured.err + repr(result)


@pytest.mark.parametrize(
    "outcome",
    (_CallbackOutcome.denied(), _CallbackOutcome.invalid(), _CallbackOutcome.timeout()),
    ids=("denied", "invalid", "timeout"),
)
def test_callback_failure_never_exchanges_a_code(outcome: _CallbackOutcome) -> None:
    requests: list[httpx.Request] = []
    browser_urls: list[str] = []
    servers = _ServerFactory(outcome)
    authorizer = _authorizer(
        settings=SpotifySettings("client-id", "http://127.0.0.1:43210/callback"),
        requests=requests,
        browser_urls=browser_urls,
        server_factory=servers,
    )

    result = authorizer.authorize(
        frozenset({Capability.HEALTH}), mode=AuthorizationMode.FIXED_LOOPBACK
    )

    assert result == AuthorizationResult(False, frozenset())
    assert requests == []
