"""Focused tests for small validation and filesystem boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import music_friend._local_files as local_files
from music_friend import configuration
from music_friend.configuration import LocalConfig, LocalConfigStore
from music_friend.providers import Capability
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify import credentials as spotify_credentials
from music_friend.providers.spotify import scopes
from music_friend.providers.ticketmaster.urls import sanitize_ticketmaster_url


@pytest.mark.parametrize(
    "config",
    (
        lambda: LocalConfig(spotify_client_id=" "),
        lambda: LocalConfig(event_country_code="us"),
        lambda: LocalConfig(event_postal_code="bad/postal"),
        lambda: LocalConfig(event_radius=float("inf")),
        lambda: LocalConfig(event_radius_unit="yards"),
    ),
)
def test_local_config_rejects_invalid_closed_values(config: object) -> None:
    with pytest.raises(ValueError):
        config()  # type: ignore[operator]


def test_radius_validator_rejects_non_numeric_types() -> None:
    assert configuration._is_radius("1") is False


def test_config_store_rejects_wrong_schema_version_type_and_save_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalConfigStore(config_dir=tmp_path)
    store.path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()
    store.path.write_text(
        '{"event_country_code":null,"event_postal_code":null,"event_radius":null,'
        '"event_radius_unit":null,"spotify_client_id":null,"version":true}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        store.load()
    with pytest.raises(ValueError):
        store.save(object())  # type: ignore[arg-type]
    monkeypatch.setattr("music_friend.configuration.atomic_replace", lambda *args, **kwargs: False)
    with pytest.raises(ValueError):
        store.save(LocalConfig())


@pytest.mark.parametrize(
    ("source", "client_id"),
    (("UPPER", "id"), (1, "id"), ("source", 1), ("source", " "), ("source", "x" * 257)),
)
def test_credential_keys_reject_unstable_identifiers(source: object, client_id: object) -> None:
    with pytest.raises(ValueError):
        CredentialKey(source, client_id)  # type: ignore[arg-type]


def test_spotify_scope_mapping_rejects_wrong_container_members_and_connection_type() -> None:
    with pytest.raises(ValueError):
        scopes._scopes_for_capabilities([])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        scopes._scopes_for_capabilities({object()})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        scopes._capabilities_for_scopes({"user-top-read"}, connected=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        scopes._capabilities_for_scopes(frozenset({"user-top-read"}), connected=1)  # type: ignore[arg-type]
    result = scopes._capabilities_for_scopes(
        scopes._scopes_for_capabilities({Capability.TOP_ARTISTS}), connected=True
    )
    assert Capability.TOP_ARTISTS in result.granted


@pytest.mark.parametrize(
    "value",
    (
        None,
        "https://[invalid",
        "http://example.test",
        "https://user@example.test",
        "https://example.test/#fragment",
    ),
)
def test_ticketmaster_url_rejects_unsafe_inputs(value: object) -> None:
    assert sanitize_ticketmaster_url(value) is None


def test_atomic_replace_cleans_temporary_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError

    monkeypatch.setattr(os, "replace", fail_replace)
    assert local_files.atomic_replace(tmp_path / "value", b"data", prefix=".temp-") is False
    assert list(tmp_path.glob(".temp-*")) == []


def test_permission_helpers_tolerate_unsupported_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fchmod", lambda *_args: (_ for _ in ()).throw(OSError()))
    descriptor = os.open(tmp_path / "value", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        local_files._restrict_descriptor(descriptor)
    finally:
        os.close(descriptor)
    monkeypatch.setattr(os, "chmod", lambda *_args: (_ for _ in ()).throw(OSError()))
    local_files._restrict_path(tmp_path, 0o700)


@pytest.mark.parametrize(
    "action",
    (
        lambda: spotify_credentials.CredentialStatus(1, ()),
        lambda: spotify_credentials.CredentialStatus(True, ("user-top-read", "a-scope")),
        lambda: spotify_credentials.CredentialStatus(False, ("user-top-read",)),
        lambda: spotify_credentials._SpotifyCredential("", frozenset()),
        lambda: spotify_credentials._validated_scopes("scope"),
        lambda: spotify_credentials._validated_scopes(("bad scope",)),
        lambda: spotify_credentials._validated_scopes(("scope", "scope")),
        lambda: spotify_credentials._encode_credential(object()),
    ),
)
def test_spotify_credential_values_reject_ambiguous_or_unsafe_shapes(action: object) -> None:
    with pytest.raises(ValueError):
        action()  # type: ignore[operator]


@pytest.mark.parametrize(
    "encoded",
    (
        "",
        "not-json",
        "[]",
        '{"schema":2,"refresh_token":"token","granted_scopes":[]}',
        '{"schema":1,"refresh_token":"token","granted_scopes":{}}',
        '{"schema":1,"refresh_token":"one","refresh_token":"two","granted_scopes":[]}',
    ),
)
def test_spotify_credential_decoder_rejects_malformed_envelopes(encoded: str) -> None:
    with pytest.raises(ValueError):
        spotify_credentials._decode_credential(encoded)


def test_spotify_credential_round_trip_keeps_secret_out_of_repr() -> None:
    credential = spotify_credentials._SpotifyCredential("private-token", frozenset({"scope"}))
    decoded = spotify_credentials._decode_credential(
        spotify_credentials._encode_credential(credential)
    )
    assert decoded == credential
    assert "private-token" not in repr(decoded)
