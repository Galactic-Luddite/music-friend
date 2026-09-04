from __future__ import annotations

import httpx
import pytest

from music_friend.errors import AuthenticationRequiredError
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import _encode_credential, _SpotifyCredential
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


def test_disconnect_is_local_idempotent_and_client_scoped(tmp_path) -> None:
    store = MemoryStore()
    first = CredentialKey("spotify", "client-a")
    second = CredentialKey("spotify", "client-b")
    envelope = _encode_credential(
        _SpotifyCredential("refresh-value", frozenset({"user-read-private"}))
    )
    store.values[first] = envelope
    store.values[second] = envelope
    requests: list[httpx.Request] = []
    transport = SpotifyTransport(
        httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500))
    )
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog-canary")
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"), transport=transport, store=store
    )

    assert manager.disconnect() is None
    assert manager.disconnect() is None

    assert first not in store.values
    assert store.values[second] == envelope
    assert requests == []
    assert catalog.read_bytes() == b"catalog-canary"
    assert manager.status().connected is False
    with pytest.raises(AuthenticationRequiredError):
        manager._access_token()
