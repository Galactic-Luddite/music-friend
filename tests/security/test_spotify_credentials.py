from __future__ import annotations

import json
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from music_friend.domain import Artist, IdentityConfidence, SourceReference
from music_friend.errors import InvalidSourceResponseError
from music_friend.providers import Capability
from music_friend.providers.credentials import CredentialKey, CredentialStoreError
from music_friend.providers.keyring_store import KeyringCredentialStore
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import _encode_credential, _SpotifyCredential
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport
from music_friend.store import Catalog


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.saves = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.saves += 1
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


class RawBackendFailure(RuntimeError):
    def __init__(self, operation: str) -> None:
        super().__init__(f"native-backend-message-canary:{operation}")
        self.private_detail = f"native-backend-attribute-canary:{operation}"


class FailingNativeBackend:
    def __init__(self, operation: str) -> None:
        self.operation = operation

    def set_password(self, _service: str, _account: str, _value: str) -> None:
        if self.operation == "save":
            raise RawBackendFailure("save")

    def get_password(self, _service: str, _account: str) -> str | None:
        if self.operation == "load":
            raise RawBackendFailure("load")
        if self.operation == "delete":
            return "synthetic-existing-value"
        return None

    def delete_password(self, _service: str, _account: str) -> None:
        if self.operation == "delete":
            raise RawBackendFailure("delete")


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(result)


def _error_surface(error: BaseException) -> str:
    graph = _exception_graph(error)
    return "\n".join(
        [
            "".join(traceback.format_exception(error)),
            *(repr(node) + str(node) + repr(vars(node)) for node in graph),
        ]
    )


def _token_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "access_" + "token": "synthetic-access-value",
        "refresh_" + "token": "synthetic-refresh-value",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "user-read-private",
    }
    payload.update(updates)
    return payload


@pytest.mark.parametrize(
    "payload",
    (
        _token_payload(access_token=None),
        _token_payload(access_token=""),
        _token_payload(access_token="a" * 4097),
        _token_payload(token_type="Basic"),
        _token_payload(token_type=None),
        _token_payload(expires_in=True),
        _token_payload(expires_in=0),
        _token_payload(expires_in=86401),
        _token_payload(refresh_token=""),
        _token_payload(refresh_token="r" * 4097),
        _token_payload(scope="user-read-private user-read-private"),
        _token_payload(scope="user-read-private  user-top-read"),
        _token_payload(scope="scope-with-\N{ZERO WIDTH SPACE}"),
        _token_payload(scope="s" * 8193),
    ),
    ids=(
        "missing-access",
        "empty-access",
        "oversized-access",
        "wrong-token-type",
        "missing-token-type",
        "boolean-expiry",
        "zero-expiry",
        "oversized-expiry",
        "empty-refresh",
        "oversized-refresh",
        "duplicate-scope",
        "empty-scope",
        "unicode-scope",
        "oversized-scope",
    ),
)
def test_invalid_token_fields_leave_credentials_and_catalog_unchanged(
    payload: dict[str, object],
    tmp_path: Path,
) -> None:
    store = MemoryCredentialStore()
    selected_key = CredentialKey("spotify", "client-a")
    other_key = CredentialKey("spotify", "other-client")
    selected_envelope = _encode_credential(
        _SpotifyCredential("selected-refresh-value", frozenset({"user-read-private"}))
    )
    other_envelope = _encode_credential(
        _SpotifyCredential("other-refresh-value", frozenset({"user-library-read"}))
    )
    store.values[selected_key] = selected_envelope
    store.values[other_key] = other_envelope
    before_values = dict(store.values)
    observed_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    artist = Artist(
        local_id="spotify:artist:catalog-survivor",
        display_name="Catalog Survivor",
        source_refs=(
            SourceReference(
                source="spotify",
                native_id="catalog-survivor",
                canonical_url=None,
                observed_at=observed_at,
            ),
        ),
        identity_confidence=IdentityConfidence.EXTERNAL_ID,
        observed_at=observed_at,
    )
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    catalog.put_artist(artist)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    transport = SpotifyTransport(httpx.MockTransport(respond))
    manager = SpotifyTokenManager(
        settings=SpotifySettings("client-a"),
        transport=transport,
        store=store,
    )
    status_before = manager.status()
    capabilities_before = manager.capabilities()
    assert status_before.connected is True
    assert status_before.granted_scopes == ("user-read-private",)
    assert capabilities_before.granted == frozenset(
        {Capability.HEALTH, Capability.SEARCH_ARTISTS, Capability.RECENT_RELEASES}
    )

    try:
        with pytest.raises(InvalidSourceResponseError) as raised:
            manager._exchange_authorization_code(
                "synthetic-code",
                redirect_uri="http://127.0.0.1:43210/callback",
                verifier="synthetic-verifier",
                granted_scopes=frozenset({"user-read-private"}),
            )

        assert len(requests) == 1
        assert store.values == before_values
        assert store.values[selected_key] == selected_envelope
        assert store.values[other_key] == other_envelope
        assert store.saves == 0
        assert manager.status() == status_before
        assert manager.status().connected is True
        assert manager.status().granted_scopes == ("user-read-private",)
        assert manager.capabilities() == capabilities_before
        assert manager.capabilities().granted == frozenset(
            {Capability.HEALTH, Capability.SEARCH_ARTISTS, Capability.RECENT_RELEASES}
        )
        persisted = catalog.get_artist(artist.local_id)
        assert persisted is not None
        assert asdict(persisted) == asdict(artist)
        assert raised.value.__cause__ is None
        surface = _error_surface(raised.value)
        assert "scope-with-" not in surface
        assert "synthetic-access-value" not in surface
        assert "synthetic-refresh-value" not in surface
    finally:
        transport.close()
        catalog.close()


def test_malformed_stored_envelope_is_redacted_and_not_rewritten() -> None:
    canary = "malformed-envelope-diagnostic-canary"
    key = CredentialKey("spotify", "client-a")
    malformed = json.dumps(
        {
            "schema": 1,
            "refresh_" + "token": canary,
            "granted_scopes": ["user-read-private"],
            "unexpected": canary,
        }
    )
    store = MemoryCredentialStore()
    store.values[key] = malformed
    transport = SpotifyTransport(
        httpx.MockTransport(lambda _request: pytest.fail("malformed storage attempted HTTP"))
    )

    with pytest.raises(CredentialStoreError) as raised:
        SpotifyTokenManager(
            settings=SpotifySettings("client-a"),
            transport=transport,
            store=store,
        )

    assert store.values == {key: malformed}
    assert store.saves == 0
    assert canary not in _error_surface(raised.value)
    transport.close()


@pytest.mark.parametrize("operation", ("factory", "save", "load", "delete"))
def test_fake_native_backend_failures_never_expose_backend_diagnostics(
    operation: str,
) -> None:
    if operation == "factory":

        def factory():
            raise RawBackendFailure("factory")

        with pytest.raises(CredentialStoreError) as raised:
            KeyringCredentialStore(_backend_factory=factory)
    else:
        backend = FailingNativeBackend(operation)
        store = KeyringCredentialStore(_backend_factory=lambda: (backend, FailingNativeBackend))
        key = CredentialKey("spotify", "client-a")
        with pytest.raises(CredentialStoreError) as raised:
            if operation == "save":
                store.save(key, "synthetic-envelope")
            elif operation == "load":
                store.load(key)
            else:
                store.delete(key)

    surface = _error_surface(raised.value)
    assert "native-backend-message-canary" not in surface
    assert "native-backend-attribute-canary" not in surface
    assert str(raised.value) == "Credential storage is unavailable."
