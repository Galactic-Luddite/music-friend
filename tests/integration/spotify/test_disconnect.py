from __future__ import annotations

import traceback
from datetime import datetime, timezone

import httpx
import pytest

from music_friend.domain import Artist, IdentityConfidence, SourceReference
from music_friend.errors import AuthenticationRequiredError
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import _encode_credential, _SpotifyCredential
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport
from music_friend.store import Catalog


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.deletes: list[CredentialKey] = []

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.deletes.append(key)
        self.values.pop(key, None)


def test_revoked_refresh_fails_safely_then_disconnect_preserves_other_local_state(
    tmp_path,
) -> None:
    canary = "revoked-provider-diagnostic-canary"
    first_key = CredentialKey("spotify", "client-a")
    second_key = CredentialKey("spotify", "client-b")
    envelope = _encode_credential(
        _SpotifyCredential("synthetic-refresh-value", frozenset({"user-read-private"}))
    )
    store = MemoryCredentialStore()
    store.values[first_key] = envelope
    store.values[second_key] = envelope
    requests: list[httpx.Request] = []

    def revoked(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": canary,
            },
        )

    transport = SpotifyTransport(httpx.MockTransport(revoked))
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=transport,
        store=store,
    )
    observed = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    artist = Artist(
        local_id="local-artist",
        display_name="Preserved Artist",
        source_refs=(
            SourceReference(
                source="spotify",
                native_id="artist001",
                canonical_url="https://open.spotify.com/artist/artist001",
                observed_at=observed,
            ),
        ),
        identity_confidence=IdentityConfidence.SOURCE_ONLY,
        observed_at=observed,
    )
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    catalog.put_artist(artist)

    try:
        with pytest.raises(AuthenticationRequiredError) as raised:
            manager._access_token()

        rendered = "".join(traceback.format_exception(raised.value))
        assert canary not in rendered
        assert len(requests) == 1
        assert requests[0].url == "https://accounts.spotify.com/api/token"
        assert manager.status().connected is True

        requests_before_disconnect = tuple(requests)
        manager.disconnect()
        manager.disconnect()

        assert tuple(requests) == requests_before_disconnect
        assert manager.status().connected is False
        assert first_key not in store.values
        assert store.values[second_key] == envelope
        assert store.deletes == [first_key, first_key]
        assert catalog.get_artist(artist.local_id) == artist
        with pytest.raises(AuthenticationRequiredError):
            manager._access_token()
    finally:
        catalog.close()
        transport.close()
