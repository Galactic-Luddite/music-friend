"""Focused selection of interactive and scheduled credential stores."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from platformdirs import user_data_path

from music_friend.providers.credentials import CredentialStore, CredentialStoreError
from music_friend.providers.encrypted_vault import EncryptedVaultCredentialStore
from music_friend.providers.keyring_store import KeyringCredentialStore

_NativeStoreFactory = Callable[[], CredentialStore]


def open_interactive_credential_store(
    *,
    vault_path: Path | None = None,
    passphrase_prompt: Callable[[], str] | None = None,
    _native_store_factory: _NativeStoreFactory = KeyringCredentialStore,
) -> CredentialStore:
    """Prefer an approved native store; otherwise use the interactive encrypted vault."""
    try:
        return _native_store_factory()
    except CredentialStoreError:
        path = vault_path or (
            Path(user_data_path("music-friend", appauthor=False)) / "credentials.vault"
        )
        if passphrase_prompt is None:
            return EncryptedVaultCredentialStore(path=path)
        return EncryptedVaultCredentialStore(path=path, passphrase_prompt=passphrase_prompt)


def open_scheduled_credential_store(
    *,
    _native_store_factory: _NativeStoreFactory = KeyringCredentialStore,
) -> CredentialStore:
    """Return a native store only; encrypted vaults are never schedule eligible."""
    store = _native_store_factory()
    if getattr(store, "scheduled_eligible", False) is not True:
        raise CredentialStoreError()
    return store


__all__ = ["open_interactive_credential_store", "open_scheduled_credential_store"]
