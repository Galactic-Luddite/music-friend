"""Fail-closed native credential-store adapter."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from typing import Protocol, TypeAlias, cast

from music_friend.providers.credentials import (
    CredentialKey,
    CredentialStoreError,
)

_ACCOUNT = "credential"


class _KeyringBackend(Protocol):
    def set_password(self, service: str, account: str, value: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...

    def delete_password(self, service: str, account: str) -> None: ...


class _BackendFailure(RuntimeError):
    """Private diagnostic that deliberately contains no backend values."""


_ApprovedTypes: TypeAlias = type[object] | tuple[type[object], ...]
_BackendFactory = Callable[[], tuple[object, _ApprovedTypes]]


def _default_backend_factory() -> tuple[object, _ApprovedTypes]:
    keyring = importlib.import_module("keyring")
    get_keyring = getattr(keyring, "get_keyring", None)
    approved = _approved_backend_types()
    if not callable(get_keyring) or not approved:
        raise _BackendFailure()
    return get_keyring(), approved


def _approved_backend_types() -> tuple[type[object], ...]:
    """Return only platform-native backend types that Music Friend permits."""
    candidates: tuple[tuple[str, str], ...]
    if sys.platform == "darwin":
        candidates = (("keyring.backends.macOS", "Keyring"),)
    elif sys.platform == "win32":
        candidates = (("keyring.backends.Windows", "WinVaultKeyring"),)
    elif sys.platform.startswith("linux"):
        candidates = (
            ("keyring.backends.SecretService", "Keyring"),
            ("keyring.backends.kwallet", "DBusKeyring"),
        )
    else:
        return ()
    approved: list[type[object]] = []
    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
            candidate = getattr(module, class_name, None)
        except Exception:
            continue
        if isinstance(candidate, type):
            approved.append(candidate)
    return tuple(approved)


class KeyringCredentialStore:
    """Use only exact, validated native macOS, Windows, or Linux keyring backends."""

    scheduled_eligible = True

    def __init__(self, *, _backend_factory: _BackendFactory = _default_backend_factory) -> None:
        backend: object | None = None
        failed = False
        try:
            backend, approved_type = _backend_factory()
            approved_types = (approved_type,) if isinstance(approved_type, type) else approved_type
            if type(backend) not in approved_types:
                raise _BackendFailure()
            for method in ("set_password", "get_password", "delete_password"):
                if not callable(getattr(backend, method, None)):
                    raise _BackendFailure()
        except Exception:
            failed = True
        if failed or backend is None:
            self._raise_unavailable()
        self._backend = cast(_KeyringBackend, backend)

    def save(self, key: CredentialKey, value: str) -> None:
        if type(key) is not CredentialKey or type(value) is not str or not value:
            raise CredentialStoreError()
        failed = False
        try:
            self._backend.set_password(key._service, _ACCOUNT, value)
        except Exception:
            failed = True
        if failed:
            self._raise_unavailable()

    def load(self, key: CredentialKey) -> str | None:
        if type(key) is not CredentialKey:
            raise CredentialStoreError()
        value: object = None
        failed = False
        try:
            value = self._backend.get_password(key._service, _ACCOUNT)
        except Exception:
            failed = True
        if failed:
            self._raise_unavailable()
        if value is not None and type(value) is not str:
            self._raise_unavailable()
        return cast(str | None, value)

    def delete(self, key: CredentialKey) -> None:
        if type(key) is not CredentialKey:
            raise CredentialStoreError()
        failed = False
        try:
            if self._backend.get_password(key._service, _ACCOUNT) is not None:
                self._backend.delete_password(key._service, _ACCOUNT)
        except Exception:
            failed = True
        if failed:
            self._raise_unavailable()

    @staticmethod
    def _raise_unavailable() -> None:
        failure = _BackendFailure()
        raise CredentialStoreError() from failure


__all__ = ["KeyringCredentialStore"]
