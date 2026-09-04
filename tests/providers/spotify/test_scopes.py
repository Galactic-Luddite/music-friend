from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from music_friend.errors import AdditionalScopeRequiredError
from music_friend.providers import Capability, ProviderCapabilities
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import _encode_credential, _SpotifyCredential
from music_friend.providers.spotify.scopes import (
    _SUPPORTED_CAPABILITIES,
    _capabilities_for_scopes,
    _scopes_for_capabilities,
)
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport


class MemoryStore:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.loads = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.value = value

    def load(self, key: CredentialKey) -> str | None:
        self.loads += 1
        return self.value

    def delete(self, key: CredentialKey) -> None:
        self.value = None


def test_exact_closed_capability_scope_mapping_and_minimal_union() -> None:
    expected = {
        Capability.HEALTH: frozenset({"user-read-private"}),
        Capability.SEARCH_ARTISTS: frozenset({"user-read-private"}),
        Capability.FOLLOWED_ARTISTS: frozenset({"user-follow-read"}),
        Capability.SAVED_ITEMS: frozenset({"user-library-read"}),
        Capability.TOP_ITEMS: frozenset({"user-top-read"}),
        Capability.TOP_ARTISTS: frozenset({"user-top-read"}),
        Capability.RECENT_RELEASES: frozenset(),
    }

    assert _SUPPORTED_CAPABILITIES == frozenset(expected)
    for capability, scopes in expected.items():
        assert _scopes_for_capabilities(frozenset({capability})) == scopes
    assert _scopes_for_capabilities(
        frozenset({Capability.HEALTH, Capability.SEARCH_ARTISTS})
    ) == frozenset({"user-read-private"})


def test_unexpected_scope_is_preserved_without_capability_promotion() -> None:
    snapshot = _capabilities_for_scopes(
        frozenset({"user-read-private", "future-provider-scope"}), connected=True
    )

    assert snapshot == ProviderCapabilities(
        supported=_SUPPORTED_CAPABILITIES,
        granted=frozenset(
            {
                Capability.HEALTH,
                Capability.SEARCH_ARTISTS,
                Capability.RECENT_RELEASES,
            }
        ),
    )


def test_no_scope_capability_still_requires_a_connected_credential() -> None:
    disconnected = _capabilities_for_scopes(frozenset(), connected=False)
    connected = _capabilities_for_scopes(frozenset(), connected=True)

    assert Capability.RECENT_RELEASES not in disconnected.granted
    assert Capability.RECENT_RELEASES in connected.granted


def test_missing_scope_fails_before_token_or_transport_and_snapshot_has_no_io() -> None:
    envelope = _encode_credential(_SpotifyCredential("refresh-value", frozenset({"user-top-read"})))
    store = MemoryStore(envelope)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = SpotifyTransport(httpx.MockTransport(respond))
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"), transport=transport, store=store
    )
    source = SpotifySource(
        settings=SpotifySettings("client-a"),
        tokens=manager,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    loads_after_construction = store.loads

    assert source.capabilities() == manager.capabilities()
    assert store.loads == loads_after_construction
    with pytest.raises(AdditionalScopeRequiredError):
        source.health()
    assert requests == []
