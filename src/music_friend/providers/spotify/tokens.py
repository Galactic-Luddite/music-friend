"""Private Spotify access-token lifecycle over neutral credential storage."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from music_friend.errors import AuthenticationRequiredError, InvalidSourceResponseError
from music_friend.providers import ProviderCapabilities
from music_friend.providers.credentials import (
    CredentialKey,
    CredentialStore,
    CredentialStoreError,
)
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import (
    CredentialStatus,
    _decode_credential,
    _encode_credential,
    _SpotifyCredential,
)
from music_friend.providers.spotify.scopes import _capabilities_for_scopes
from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport

_MAX_TOKEN_LENGTH = 4096
_MAX_SCOPE_TEXT_LENGTH = 8192
_MAX_ACCESS_LIFETIME_SECONDS = 86_400


class _Response(Protocol):
    data: Mapping[str, object]


class SpotifyTokenManager:
    """Own persisted refresh state and memory-only Spotify access state."""

    def __init__(
        self,
        *,
        settings: SpotifySettings,
        transport: SpotifyTransport,
        store: CredentialStore,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(settings) is not SpotifySettings:
            raise ValueError("settings must be SpotifySettings")
        if type(transport) is not SpotifyTransport:
            raise ValueError("transport must be SpotifyTransport")
        if not isinstance(store, CredentialStore):
            raise ValueError("store must implement CredentialStore")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._transport = transport
        self._store = store
        self._clock = clock
        self._key = CredentialKey("spotify", settings.client_id)
        self._client_id = settings.client_id
        self._credential: _SpotifyCredential | None = None
        self._access: str | None = None
        self._expires_at: float | None = None
        self._access_was_refreshed = False
        self._refresh_blocked = False
        credential_failure = False
        try:
            stored = self._store.load(self._key)
            if stored is not None:
                self._credential = _decode_credential(stored)
        except CredentialStoreError:
            raise
        except Exception:
            credential_failure = True
        if credential_failure:
            raise CredentialStoreError() from None
        self._refresh_snapshot()

    @property
    def granted_scopes(self) -> tuple[str, ...]:
        """Return only the sorted, nonsecret granted scope names."""
        if self._credential is None:
            return ()
        return tuple(sorted(self._credential.granted_scopes))

    def status(self) -> CredentialStatus:
        """Return safe local connection status without performing I/O."""
        return CredentialStatus(self._credential is not None, self.granted_scopes)

    def capabilities(self) -> ProviderCapabilities:
        """Return the precomputed capability snapshot without performing I/O."""
        return self._capabilities

    def _exchange_authorization_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        verifier: str,
        granted_scopes: frozenset[str],
    ) -> None:
        """Exchange one PKCE code and retain only bounded private token state."""
        _bounded_secret(code)
        _bounded_secret(redirect_uri)
        _bounded_secret(verifier)
        requested_scopes = _scope_set(granted_scopes)
        self._clear_access()
        try:
            response = self._transport.execute(
                SpotifyOperation.TOKEN,
                form=(
                    ("client_id", self._client_id),
                    ("grant_type", "authorization_code"),
                    ("code", code),
                    ("redirect_uri", redirect_uri),
                    ("code_verifier", verifier),
                ),
            )
            access, expires_in, replacement, response_scopes = _parse_token_response(response.data)
            if replacement is None:
                raise InvalidSourceResponseError()
            exact_scopes = requested_scopes if response_scopes is None else response_scopes
            credential = _SpotifyCredential(replacement, exact_scopes)
            self._store.save(self._key, _encode_credential(credential))
            self._credential = credential
            self._refresh_blocked = False
            self._install_access(access, expires_in, refreshed=False)
            self._refresh_snapshot()
        except (AuthenticationRequiredError, InvalidSourceResponseError, CredentialStoreError):
            self._clear_access()
            raise
        except Exception:
            self._clear_access()
            raise InvalidSourceResponseError() from None

    def _access_token(self, *, deadline: float | None = None) -> str:
        now = self._now()
        if self._access is not None and self._expires_at is not None and now < self._expires_at:
            return self._access
        self._clear_access()
        if self._credential is None or self._refresh_blocked:
            raise AuthenticationRequiredError()
        prior = self._credential
        try:
            try:
                response = self._transport.execute(
                    SpotifyOperation.TOKEN,
                    form=(
                        ("client_id", self._client_id),
                        ("grant_type", "refresh_token"),
                        ("refresh_token", prior.refresh_token),
                    ),
                    deadline=deadline,
                )
            except InvalidSourceResponseError:
                raise AuthenticationRequiredError() from None
            access, expires_in, replacement, response_scopes = _parse_token_response(response.data)
            retained_secret = prior.refresh_token if replacement is None else replacement
            scopes = prior.granted_scopes if response_scopes is None else response_scopes
            rotated = _SpotifyCredential(retained_secret, scopes)
            if rotated != prior:
                self._store.save(self._key, _encode_credential(rotated))
            self._credential = rotated
            self._install_access(access, expires_in, refreshed=True)
            self._refresh_snapshot()
            return access
        except AuthenticationRequiredError:
            self._clear_access()
            self._refresh_blocked = True
            raise AuthenticationRequiredError() from None
        except CredentialStoreError:
            self._clear_access()
            self._refresh_blocked = True
            raise
        except InvalidSourceResponseError:
            self._clear_access()
            self._refresh_blocked = True
            raise
        except Exception:
            self._clear_access()
            self._refresh_blocked = True
            raise AuthenticationRequiredError() from None

    def disconnect(self) -> None:
        """Erase only this local client credential; no remote revocation is attempted."""
        try:
            self._store.delete(self._key)
        finally:
            self._credential = None
            self._refresh_blocked = False
            self._clear_access()
            self._refresh_snapshot()

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> Mapping[str, object]:
        if operation is SpotifyOperation.TOKEN:
            raise InvalidSourceResponseError()
        access = self._access_token(deadline=deadline)
        try:
            return self._execute_with_access(operation, query, access, deadline)
        except AuthenticationRequiredError:
            replacement = self._after_unauthorized(access, deadline=deadline)
            try:
                return self._execute_with_access(operation, query, replacement, deadline)
            except AuthenticationRequiredError:
                self._after_unauthorized(replacement, deadline=deadline)
                raise AuthenticationRequiredError() from None

    def _call_deadline(self) -> float:
        return self._transport._call_deadline()

    def _execute_with_access(
        self,
        operation: SpotifyOperation,
        query: tuple[tuple[str, str], ...],
        access: str,
        deadline: float,
    ) -> Mapping[str, object]:
        execute = cast(Callable[..., _Response], self._transport.execute)
        return execute(
            operation,
            query=query,
            deadline=deadline,
            **{"access_" + "token": access},
        ).data

    def _after_unauthorized(self, rejected_access: str, *, deadline: float | None = None) -> str:
        if rejected_access != self._access:
            self._clear_access()
            self._refresh_blocked = True
            raise AuthenticationRequiredError()
        was_refreshed = self._access_was_refreshed
        self._clear_access()
        if was_refreshed:
            self._refresh_blocked = True
            raise AuthenticationRequiredError()
        return self._access_token(deadline=deadline)

    def _owns_client(self, client_id: str) -> bool:
        return type(client_id) is str and client_id == self._client_id

    def _install_access(self, access: str, expires_in: int, *, refreshed: bool) -> None:
        self._access = access
        self._expires_at = self._now() + expires_in
        self._access_was_refreshed = refreshed

    def _clear_access(self) -> None:
        self._access = None
        self._expires_at = None
        self._access_was_refreshed = False

    def _refresh_snapshot(self) -> None:
        scopes = frozenset() if self._credential is None else self._credential.granted_scopes
        self._capabilities = _capabilities_for_scopes(
            scopes, connected=self._credential is not None
        )

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AuthenticationRequiredError()
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AuthenticationRequiredError()
        return parsed


def _bounded_secret(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TOKEN_LENGTH:
        raise InvalidSourceResponseError()
    return value


def _scope_set(value: object) -> frozenset[str]:
    try:
        if type(value) is not frozenset:
            raise ValueError("scopes must be a frozen set")
        return _SpotifyCredential("placeholder", cast(frozenset[str], value)).granted_scopes
    except (TypeError, ValueError):
        raise InvalidSourceResponseError() from None


def _response_scope(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > _MAX_SCOPE_TEXT_LENGTH:
        raise InvalidSourceResponseError()
    parts = value.split(" ")
    if not parts or any(not part for part in parts) or len(set(parts)) != len(parts):
        raise InvalidSourceResponseError()
    return _scope_set(frozenset(parts))


def _parse_token_response(
    data: Mapping[str, object],
) -> tuple[str, int, str | None, frozenset[str] | None]:
    access = data.get("access_token")
    token_type = data.get("token_type")
    expires_in = data.get("expires_in")
    replacement = data.get("refresh_token")
    if (
        type(access) is not str
        or not access
        or len(access) > _MAX_TOKEN_LENGTH
        or token_type != "Bearer"
        or type(expires_in) is not int
        or not 1 <= expires_in <= _MAX_ACCESS_LIFETIME_SECONDS
        or (
            replacement is not None
            and (
                type(replacement) is not str
                or not replacement
                or len(replacement) > _MAX_TOKEN_LENGTH
            )
        )
    ):
        raise InvalidSourceResponseError()
    scopes = _response_scope(data.get("scope"))
    return access, expires_in, replacement, scopes


__all__ = ["SpotifyTokenManager"]
