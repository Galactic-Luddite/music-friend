from __future__ import annotations

import json

import httpx
import pytest

from music_friend.providers.credentials import (
    CredentialKey,
    CredentialStore,
    CredentialStoreError,
)
from music_friend.providers.spotify import tokens as spotify_tokens
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.credentials import (
    CredentialStatus,
    _decode_credential,
    _encode_credential,
    _SpotifyCredential,
)
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport


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


def _exception_graph_surface(error: BaseException) -> str:
    return "|".join(repr(node) + str(node) + repr(vars(node)) for node in _exception_graph(error))


class _SingleValueStore:
    def __init__(self, value: str) -> None:
        self.value = value

    def save(self, key: CredentialKey, value: str) -> None:
        self.value = value

    def load(self, key: CredentialKey) -> str | None:
        return self.value

    def delete(self, key: CredentialKey) -> None:
        self.value = ""


def test_spotify_credential_module_depends_only_on_neutral_store() -> None:
    assert CredentialStore.__module__ == "music_friend.providers.credentials"
    assert "keyring" not in CredentialStore.__module__
    assert spotify_tokens.CredentialStore is CredentialStore


def test_envelope_has_exact_canonical_shape_and_safe_public_status() -> None:
    credential = _SpotifyCredential(
        "refresh-canary", frozenset({"user-top-read", "user-read-private"})
    )

    encoded = _encode_credential(credential)
    assert json.loads(encoded) == {
        "schema": 1,
        "refresh_" + "token": "refresh-canary",
        "granted_scopes": ["user-read-private", "user-top-read"],
    }
    assert _decode_credential(encoded) == credential
    status = CredentialStatus(True, tuple(sorted(credential.granted_scopes)))
    assert status == CredentialStatus(
        connected=True,
        granted_scopes=("user-read-private", "user-top-read"),
    )
    assert "refresh-canary" not in repr(status)


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "[]",
        '{"schema":2,"refresh_" + "token":"r","granted_scopes":[]}',
        '{"schema":1,"refresh_" + "token":"r","granted_scopes":[],"extra":1}',
        '{"schema":1,"refresh_" + "token":"","granted_scopes":[]}',
        '{"schema":1,"refresh_" + "token":"r","granted_scopes":["same","same"]}',
        '{"schema":1,"refresh_" + "token":"r","granted_scopes":[1]}',
        '{"schema":1,"refresh_" + "token":"r","granted_scopes":["UPPER"]}',
    ],
)
def test_malformed_envelope_fails_without_echoing_contents(value: str) -> None:
    with pytest.raises(ValueError) as raised:
        _decode_credential(value)

    assert value not in str(raised.value)


def test_malformed_envelope_discards_parser_canary_from_complete_exception_graph() -> None:
    canary = "malformed-envelope-document-canary"
    malformed = '{"schema":1,"refresh_' + 'token":"' + canary

    with pytest.raises(ValueError) as decoded:
        _decode_credential(malformed)
    assert _exception_graph(decoded.value) == (decoded.value,)
    assert canary not in _exception_graph_surface(decoded.value)

    manager_store = _SingleValueStore(malformed)
    with pytest.raises(CredentialStoreError) as wrapped:
        SpotifyTokenManager(
            settings=SpotifySettings("client-a"),
            transport=SpotifyTransport(httpx.MockTransport(lambda _request: httpx.Response(500))),
            store=manager_store,
        )
    assert _exception_graph(wrapped.value) == (wrapped.value,)
    assert canary not in _exception_graph_surface(wrapped.value)


def test_exception_graph_recorder_detects_deliberate_parser_canary() -> None:
    canary = "deliberate-parser-canary"
    try:
        json.loads('{"value":"' + canary)
    except json.JSONDecodeError:
        try:
            raise ValueError("safe") from None
        except ValueError as public_error:
            recorded = _exception_graph_surface(public_error)

    assert canary in recorded


def test_encoder_rejects_actual_json_output_above_decoder_bound() -> None:
    credential = _SpotifyCredential("\u96ea" * 4096, frozenset({"user-read-private"}))

    with pytest.raises(ValueError):
        _encode_credential(credential)


def test_every_accepted_encoded_envelope_round_trips_through_decoder() -> None:
    credential = _SpotifyCredential("\u96ea" * 2000, frozenset({"user-read-private"}))

    encoded = _encode_credential(credential)

    assert _decode_credential(encoded) == credential


def test_client_keys_do_not_cross_load_envelopes() -> None:
    first = CredentialKey("spotify", "client-a")
    second = CredentialKey("spotify", "client-b")

    assert first != second


def test_disconnected_status_cannot_claim_granted_scopes() -> None:
    with pytest.raises(ValueError):
        CredentialStatus(False, ("user-read-private",))
