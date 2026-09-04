from __future__ import annotations

import hashlib
import inspect

from music_friend.providers.credentials import CredentialKey, CredentialStore


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


def test_credential_key_is_source_and_normalized_client_scoped() -> None:
    key = CredentialKey("spotify", " client-id ")
    digest = hashlib.sha256(b"client-id").hexdigest()

    assert key == CredentialKey("spotify", "client-id")
    assert key._service == f"music-friend:spotify:{digest}"
    assert "client-id" not in repr(key)
    assert digest not in repr(key)


def test_credential_store_protocol_is_provider_neutral() -> None:
    source = inspect.getsource(inspect.getmodule(CredentialStore))

    assert "spotify" not in source.lower()
    assert "keyring" not in source.lower()
    assert "darwin" not in source.lower()
    assert isinstance(MemoryCredentialStore(), CredentialStore)


def test_memory_store_round_trip_uses_opaque_keys() -> None:
    store = MemoryCredentialStore()
    key = CredentialKey("spotify", "client-a")

    store.save(key, "opaque-envelope")
    assert store.load(key) == "opaque-envelope"
    store.delete(key)
    assert store.load(key) is None
