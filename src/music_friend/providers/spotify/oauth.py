"""Bounded Spotify authorization using PKCE."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlencode

from music_friend.providers import Capability
from music_friend.providers.spotify.callback import (
    _CallbackServer,
    _CallbackStatus,
    _close_callback_server,
    _new_callback_server,
    _read_manual_callback,
    _serve_one,
)
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.scopes import _scopes_for_capabilities
from music_friend.providers.spotify.tokens import SpotifyTokenManager

_AUTHORIZE_ORIGIN = "https://accounts.spotify.com"
_AUTHORIZE_PATH = "/authorize"
_SECRET_BYTES = 32
_DYNAMIC_REDIRECT_URI = "http://127.0.0.1/callback"
_FIXED_REDIRECT_URI = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/callback\Z")


class AuthorizationMode(str, Enum):
    """Supported ways to receive one Spotify authorization callback."""

    DYNAMIC_LOOPBACK = "dynamic_loopback"
    FIXED_LOOPBACK = "fixed_loopback"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    """A credential-free summary of an authorization attempt."""

    authorized: bool
    granted_capabilities: frozenset[Capability]


class SpotifyAuthorization:
    """Perform one bounded authorization attempt through injected local boundaries."""

    def __init__(
        self,
        *,
        settings: SpotifySettings,
        tokens: SpotifyTokenManager,
        browser_opener: Callable[[str], bool],
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        _server_factory: Callable[..., _CallbackServer] = _new_callback_server,
    ) -> None:
        if type(tokens) is not SpotifyTokenManager:
            raise ValueError("tokens must be SpotifyTokenManager")
        if type(settings) is not SpotifySettings or not tokens._owns_client(settings.client_id):
            raise ValueError("authorization settings do not match token manager")
        self._settings = settings
        self._tokens = tokens
        self._browser_opener = browser_opener
        self._random_bytes = random_bytes
        self._server_factory = _server_factory

    def authorize(
        self,
        capabilities: set[Capability] | frozenset[Capability],
        *,
        mode: AuthorizationMode,
        callback_reader: Callable[[], str] | None = None,
    ) -> AuthorizationResult:
        """Authorize selected capabilities without returning credentials or diagnostics."""
        failure = AuthorizationResult(False, frozenset())
        if (
            not isinstance(capabilities, (set, frozenset))
            or not all(isinstance(capability, Capability) for capability in capabilities)
            or not capabilities
            or not isinstance(mode, AuthorizationMode)
        ):
            return failure
        selected = frozenset(capabilities)
        listener: _CallbackServer | None = None
        attempt: _AuthorizationAttempt | None = None
        try:
            verifier, state = _authorization_secrets(self._random_bytes)
            redirect_uri, listener = self._prepare_callback(mode, state, callback_reader)
            if redirect_uri is None:
                return failure
            authorization_url = _build_authorization_url(
                client_id=self._settings.client_id,
                redirect_uri=redirect_uri,
                state=state,
                verifier=verifier,
                scope=_scope_for(selected),
            )
            attempt = _AuthorizationAttempt(
                verifier=verifier,
                state=state,
                redirect_uri=redirect_uri,
                authorization_url=authorization_url,
            )
            attempt.wait()
            if self._browser_opener(authorization_url) is not True:
                attempt.fail()
                return failure
            if mode is AuthorizationMode.MANUAL:
                assert callback_reader is not None
                outcome = _read_manual_callback(
                    callback_reader,
                    redirect_uri=redirect_uri,
                    expected_state=state,
                )
            else:
                assert listener is not None
                outcome = _serve_one(listener)
                listener = None
            if outcome._status is not _CallbackStatus.SUCCESS or outcome._code is None:
                attempt.fail()
                return failure
            self._tokens._exchange_authorization_code(
                outcome._code,
                redirect_uri=redirect_uri,
                verifier=verifier,
                granted_scopes=_scopes_for_capabilities(selected),
            )
            attempt.complete()
            granted = self._tokens.capabilities().granted.intersection(selected)
            return AuthorizationResult(True, granted)
        except Exception:
            if attempt is not None and attempt._lifecycle is _AttemptLifecycle.WAITING:
                attempt.fail()
            return failure
        finally:
            if listener is not None:
                _close_callback_server(listener)
            if attempt is not None and attempt._lifecycle in {
                _AttemptLifecycle.COMPLETED,
                _AttemptLifecycle.FAILED,
            }:
                attempt.close()

    def _prepare_callback(
        self,
        mode: AuthorizationMode,
        state: str,
        callback_reader: Callable[[], str] | None,
    ) -> tuple[str | None, _CallbackServer | None]:
        configured = self._settings.redirect_uri
        if mode is AuthorizationMode.DYNAMIC_LOOPBACK:
            if configured not in {None, _DYNAMIC_REDIRECT_URI} or callback_reader is not None:
                return None, None
            server = self._server_factory(0, expected_state=state)
            port = _server_port(server)
            if port is None:
                _close_callback_server(server)
                return None, None
            return f"http://127.0.0.1:{port}/callback", server
        match = _FIXED_REDIRECT_URI.fullmatch(configured or "")
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            return None, None
        if mode is AuthorizationMode.MANUAL:
            if callback_reader is None:
                return None, None
            return configured, None
        if callback_reader is not None:
            return None, None
        port = int(match.group(1))
        server = self._server_factory(port, expected_state=state)
        if _server_port(server) != port:
            _close_callback_server(server)
            return None, None
        return configured, server


class _AttemptLifecycle(str, Enum):
    NEW = "new"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CLOSED = "closed"


class _AuthorizationAttempt:
    __slots__ = ("_authorization_url", "_lifecycle", "_redirect_uri", "_state", "_verifier")

    def __init__(
        self,
        *,
        verifier: str,
        state: str,
        redirect_uri: str,
        authorization_url: str,
    ) -> None:
        self._verifier: str | None = verifier
        self._state: str | None = state
        self._redirect_uri: str | None = redirect_uri
        self._authorization_url: str | None = authorization_url
        self._lifecycle = _AttemptLifecycle.NEW

    def __repr__(self) -> str:
        return f"<_AuthorizationAttempt state={self._lifecycle.name}>"

    def wait(self) -> None:
        self._transition(_AttemptLifecycle.NEW, _AttemptLifecycle.WAITING)

    def complete(self) -> None:
        self._transition(_AttemptLifecycle.WAITING, _AttemptLifecycle.COMPLETED)

    def fail(self) -> None:
        self._transition(_AttemptLifecycle.WAITING, _AttemptLifecycle.FAILED)

    def close(self) -> None:
        if self._lifecycle not in {_AttemptLifecycle.COMPLETED, _AttemptLifecycle.FAILED}:
            raise RuntimeError("authorization attempt is not terminal")
        self._verifier = None
        self._state = None
        self._redirect_uri = None
        self._authorization_url = None
        self._lifecycle = _AttemptLifecycle.CLOSED

    def _transition(self, expected: _AttemptLifecycle, destination: _AttemptLifecycle) -> None:
        if self._lifecycle is not expected:
            raise RuntimeError("authorization attempt is not waiting")
        self._lifecycle = destination


def _urlsafe_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _entropy_bytes(random_bytes: Callable[[int], bytes]) -> bytes:
    value = random_bytes(_SECRET_BYTES)
    if type(value) is not bytes or len(value) != _SECRET_BYTES:
        raise ValueError("entropy source must return exactly 32 bytes")
    return value


def _pkce_verifier(random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return _urlsafe_no_padding(_entropy_bytes(random_bytes))


def _state_token(random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return _urlsafe_no_padding(_entropy_bytes(random_bytes))


def _authorization_secrets(random_bytes: Callable[[int], bytes]) -> tuple[str, str]:
    verifier_bytes = _entropy_bytes(random_bytes)
    state_bytes = _entropy_bytes(random_bytes)
    if verifier_bytes == state_bytes:
        raise ValueError("PKCE verifier and state entropy must be distinct")
    return _urlsafe_no_padding(verifier_bytes), _urlsafe_no_padding(state_bytes)


def _pkce_challenge(verifier: str) -> str:
    return _urlsafe_no_padding(hashlib.sha256(verifier.encode("ascii")).digest())


def _scope_for(capabilities: frozenset[Capability]) -> str:
    return " ".join(sorted(_scopes_for_capabilities(capabilities)))


def _build_authorization_url(
    *, client_id: str, redirect_uri: str, state: str, verifier: str, scope: str
) -> str:
    query = urlencode(
        (
            ("client_id", client_id),
            ("response_type", "code"),
            ("redirect_uri", redirect_uri),
            ("state", state),
            ("scope", scope),
            ("code_challenge_method", "S256"),
            ("code_challenge", _pkce_challenge(verifier)),
        )
    )
    return f"{_AUTHORIZE_ORIGIN}{_AUTHORIZE_PATH}?{query}"


def _server_port(server: _CallbackServer) -> int | None:
    address = getattr(server, "server_address", None)
    if (
        not isinstance(address, tuple)
        or len(address) < 2
        or address[0] != "127.0.0.1"
        or isinstance(address[1], bool)
        or not isinstance(address[1], int)
        or not 1 <= address[1] <= 65535
    ):
        return None
    return address[1]


__all__ = ["AuthorizationMode", "AuthorizationResult", "SpotifyAuthorization"]
