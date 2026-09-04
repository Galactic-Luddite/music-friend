"""Tests for the interactive encrypted credential fallback."""

from __future__ import annotations

import json
import traceback
from base64 import b64decode, b64encode
from pathlib import Path

import pytest

import music_friend._local_files as local_files
from music_friend.providers import encrypted_vault
from music_friend.providers.credentials import CredentialKey, CredentialStoreError
from music_friend.providers.encrypted_vault import EncryptedVaultCredentialStore
from music_friend.providers.store_selection import (
    open_interactive_credential_store,
    open_scheduled_credential_store,
)


def test_vault_round_trip_encrypts_values_and_is_not_schedule_eligible(tmp_path: Path) -> None:
    """Replacing encryption or scheduled policy would expose protected values."""
    path = tmp_path / "credentials.vault"
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    spotify = CredentialKey("spotify", "public-client-id")
    ticketmaster = CredentialKey("ticketmaster", "default")

    store.save(spotify, "refresh-token-value")
    store.save(ticketmaster, "ticketmaster-key-value")

    reopened = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    assert reopened.load(spotify) == "refresh-token-value"
    assert reopened.load(ticketmaster) == "ticketmaster-key-value"
    assert reopened.scheduled_eligible is False
    assert b"refresh-token-value" not in path.read_bytes()
    assert b"ticketmaster-key-value" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_vault_persists_exact_kdf_header_and_rotates_nonce_on_rewrite(tmp_path: Path) -> None:
    """Changing the crypto parameters or reusing a nonce would weaken vault confidentiality."""
    path = tmp_path / "credentials.vault"
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    key = CredentialKey("spotify", "public-client-id")

    store.save(key, "first-value")
    first = json.loads(path.read_text(encoding="ascii"))
    store.save(key, "second-value")
    second = json.loads(path.read_text(encoding="ascii"))

    assert first["header"] == {
        "kdf": {
            "memory_cost_kib": 65_536,
            "parallelism": 4,
            "time_cost": 3,
            "type": "argon2id",
        },
        "version": 1,
    }
    assert len(b64decode(first["salt"], validate=True)) == 16
    assert len(b64decode(first["nonce"], validate=True)) == 12
    assert first["nonce"] != second["nonce"]


def test_tampered_authenticated_vault_header_is_rejected(tmp_path: Path) -> None:
    """Accepting a changed header would let attackers alter authenticated crypto policy."""
    path = tmp_path / "credentials.vault"
    key = CredentialKey("spotify", "public-client-id")
    EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse").save(
        key, "refresh-token-value"
    )
    document = json.loads(path.read_text(encoding="ascii"))
    document["header"]["kdf"]["time_cost"] = 1
    path.write_text(json.dumps(document), encoding="ascii")

    with pytest.raises(CredentialStoreError):
        EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse").load(
            key
        )


def test_vault_refuses_a_sixty_fifth_distinct_record_without_rewriting(tmp_path: Path) -> None:
    """Writing 65 records would create a vault the reader must reject."""
    path = tmp_path / "credentials.vault"
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    keys = [CredentialKey("spotify", f"client-{number}") for number in range(65)]

    for number, key in enumerate(keys[:64]):
        store.save(key, f"value-{number}")
    before = path.read_bytes()

    with pytest.raises(CredentialStoreError):
        store.save(keys[64], "sixty-fifth-value")

    assert path.read_bytes() == before
    reopened = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    assert reopened.load(keys[63]) == "value-63"
    assert reopened.load(keys[64]) is None


def test_malformed_vault_is_rejected_before_prompting_for_a_passphrase(tmp_path: Path) -> None:
    """Prompting before validating a corrupted vault needlessly exposes the interactive path."""
    path = tmp_path / "credentials.vault"
    path.write_bytes(b'{"version":999}')
    store = EncryptedVaultCredentialStore(
        path=path,
        passphrase_prompt=lambda: pytest.fail("malformed vault reached the KDF"),
    )

    with pytest.raises(CredentialStoreError) as raised:
        store.load(CredentialKey("spotify", "public-client-id"))

    assert str(raised.value) == "Credential storage is unavailable."


def test_wrong_vault_passphrase_has_a_sanitized_failure(tmp_path: Path) -> None:
    """Exposing authentication diagnostics could disclose protected material."""
    path = tmp_path / "credentials.vault"
    key = CredentialKey("spotify", "public-client-id")
    EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "right passphrase").save(
        key, "refresh-token-value"
    )

    with pytest.raises(CredentialStoreError) as raised:
        EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "wrong passphrase").load(
            key
        )

    assert str(raised.value) == "Credential storage is unavailable."
    assert "passphrase" not in repr(raised.value)


class _RawVaultError(RuntimeError):
    def __init__(self, operation: str) -> None:
        super().__init__(f"vault-message-canary:{operation}")
        self.private_detail = f"vault-attribute-canary:{operation}"


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


@pytest.mark.parametrize("operation", ("prompt", "parser", "crypto", "filesystem"))
def test_vault_failure_graph_never_exposes_raw_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Retaining raw exception context would disclose sensitive local failure details."""
    path = tmp_path / "credentials.vault"
    key = CredentialKey("spotify", "public-client-id")
    EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse").save(
        key, "refresh-token-value"
    )

    if operation == "prompt":
        path.unlink()

        def prompt() -> str:
            raise _RawVaultError(operation)

        store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=prompt)

        def action() -> None:
            store.save(key, "replacement-value")

    elif operation == "parser":
        monkeypatch.setattr(
            encrypted_vault,
            "_parse_document",
            lambda _encoded: (_ for _ in ()).throw(_RawVaultError(operation)),
        )
        store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")

        def action() -> None:
            store.load(key)

    elif operation == "crypto":

        class FailingCipher:
            def __init__(self, _key: bytes) -> None:
                pass

            def decrypt(self, _nonce: bytes, _ciphertext: bytes, _aad: bytes) -> bytes:
                raise _RawVaultError(operation)

        monkeypatch.setattr(encrypted_vault, "ChaCha20Poly1305", FailingCipher)
        store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")

        def action() -> None:
            store.load(key)

    else:
        monkeypatch.setattr(
            local_files.os,
            "replace",
            lambda _source, _destination: (_ for _ in ()).throw(_RawVaultError(operation)),
        )
        store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")

        def action() -> None:
            store.save(key, "replacement-value")

    with pytest.raises(CredentialStoreError) as raised:
        action()

    assert _exception_graph(raised.value) == (raised.value,)
    assert "vault-message-canary" not in _exception_graph_surface(raised.value)
    assert "vault-attribute-canary" not in _exception_graph_surface(raised.value)
    assert "refresh-token-value" not in "".join(traceback.format_exception(raised.value))


def test_vault_atomic_write_remains_usable_without_posix_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requiring a POSIX-only descriptor API would break the vault on Windows."""
    monkeypatch.delattr(local_files.os, "fchmod", raising=False)
    path = tmp_path / "credentials.vault"
    key = CredentialKey("spotify", "public-client-id")
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")

    store.save(key, "first-value")
    store.save(key, "second-value")

    assert store.load(key) == "second-value"


def test_failed_atomic_replace_preserves_prior_vault_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement must not corrupt the last readable credential vault."""
    path = tmp_path / "credentials.vault"
    key = CredentialKey("spotify", "public-client-id")
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "correct horse")
    store.save(key, "previous-value")
    before = path.read_bytes()

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("replace-failure-canary")

    monkeypatch.setattr(local_files.os, "replace", fail_replace)

    with pytest.raises(CredentialStoreError):
        store.save(key, "new-value")

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".vault-*")) == []


def test_interactive_store_selects_vault_but_scheduled_store_refuses_native_failure(
    tmp_path: Path,
) -> None:
    """Using the fallback in a schedule would require unattended passphrase entry."""

    def unavailable_native_store() -> None:
        raise CredentialStoreError()

    interactive = open_interactive_credential_store(
        vault_path=tmp_path / "credentials.vault",
        passphrase_prompt=lambda: "correct horse",
        _native_store_factory=unavailable_native_store,
    )

    assert isinstance(interactive, EncryptedVaultCredentialStore)
    with pytest.raises(CredentialStoreError):
        open_scheduled_credential_store(_native_store_factory=unavailable_native_store)


def test_store_selection_uses_default_vault_prompt_and_rejects_ineligible_schedule(
    tmp_path: Path,
) -> None:
    def unavailable_native_store() -> None:
        raise CredentialStoreError()

    interactive = open_interactive_credential_store(
        vault_path=tmp_path / "credentials.vault",
        _native_store_factory=unavailable_native_store,
    )
    assert isinstance(interactive, EncryptedVaultCredentialStore)
    with pytest.raises(CredentialStoreError):
        open_scheduled_credential_store(
            _native_store_factory=lambda: EncryptedVaultCredentialStore(
                path=tmp_path / "credentials.vault"
            )
        )


@pytest.mark.parametrize(
    "encoded",
    (
        b"not-json",
        b"[]",
        b'{"ciphertext":"","header":{},"nonce":"","salt":""}',
        b'{"ciphertext":"AA==","header":{"kdf":{"memory_cost_kib":65536,'
        b'"parallelism":4,"time_cost":3,"type":"argon2id"},"version":1},'
        b'"nonce":"AA==","salt":"AA=="}',
        b'{"ciphertext":"AAAAAAAAAAAAAAAAAAAAAA==","header":{"kdf":'
        b'{"memory_cost_kib":65536,"parallelism":4,"time_cost":3,"type":"argon2id"},'
        b'"version":1},"nonce":"AAAAAAAAAAAAAAAA","salt":"not-base64"}',
    ),
)
def test_document_parser_rejects_malformed_or_unsupported_documents(encoded: bytes) -> None:
    assert encrypted_vault._parse_document(encoded) is None


def test_document_parser_accepts_only_complete_bounded_binary_fields() -> None:
    document = {
        "ciphertext": b64encode(b"x" * 16).decode("ascii"),
        "header": encrypted_vault._HEADER,
        "nonce": b64encode(b"n" * 12).decode("ascii"),
        "salt": b64encode(b"s" * 16).decode("ascii"),
    }

    assert encrypted_vault._parse_document(json.dumps(document).encode("ascii")) == (
        b"s" * 16,
        b"n" * 12,
        b"x" * 16,
    )
    document["extra"] = True
    assert encrypted_vault._parse_document(json.dumps(document).encode("ascii")) is None


@pytest.mark.parametrize(
    "plaintext",
    (
        b"not-json",
        b"[]",
        b'{"wrong-service":"value"}',
        b'{"music-friend:service":""}',
        b'{"music-friend:service":1}',
        b'{"music-friend:service":"one","music-friend:service":"two"}',
    ),
)
def test_record_parser_rejects_malformed_or_unsafe_records(plaintext: bytes) -> None:
    assert encrypted_vault._parse_records(plaintext) is None


def test_record_parser_accepts_empty_and_valid_records() -> None:
    assert encrypted_vault._parse_records(b"{}") == {}
    assert encrypted_vault._parse_records(b'{"music-friend:spotify:id":"token"}') == {
        "music-friend:spotify:id": "token"
    }


@pytest.mark.parametrize(
    ("value", "expected_length"),
    ((None, None), (1, None), ("not base64", None), ("YQ==", 2)),
)
def test_base64_decoder_rejects_wrong_types_encoding_and_lengths(
    value: object, expected_length: int | None
) -> None:
    assert encrypted_vault._decode_b64(value, expected_length) is None


def test_vault_delete_handles_missing_vault_key_and_existing_key(tmp_path: Path) -> None:
    path = tmp_path / "credentials.vault"
    first = CredentialKey("spotify", "first")
    missing = CredentialKey("spotify", "missing")
    store = EncryptedVaultCredentialStore(path=path, passphrase_prompt=lambda: "passphrase")

    store.delete(first)
    store.save(first, "value")
    before = path.read_bytes()
    store.delete(missing)
    assert path.read_bytes() == before
    store.delete(first)
    assert store.load(first) is None


@pytest.mark.parametrize("method", ("save", "load", "delete"))
def test_vault_public_methods_reject_invalid_keys_and_values(tmp_path: Path, method: str) -> None:
    store = EncryptedVaultCredentialStore(
        path=tmp_path / "credentials.vault", passphrase_prompt=lambda: "passphrase"
    )
    with pytest.raises(CredentialStoreError):
        if method == "save":
            store.save(CredentialKey("spotify", "id"), "")
        else:
            getattr(store, method)(object())


@pytest.mark.parametrize("prompt", (lambda: "", lambda: 1))
def test_vault_rejects_invalid_passphrase_prompt_results(tmp_path: Path, prompt: object) -> None:
    store = EncryptedVaultCredentialStore(
        path=tmp_path / "credentials.vault",
        passphrase_prompt=prompt,  # type: ignore[arg-type]
    )
    with pytest.raises(CredentialStoreError):
        store.save(CredentialKey("spotify", "id"), "value")


def test_vault_rejects_unsafe_file_shapes(tmp_path: Path) -> None:
    key = CredentialKey("spotify", "id")
    directory = tmp_path / "vault-directory"
    directory.mkdir()
    with pytest.raises(CredentialStoreError):
        EncryptedVaultCredentialStore(path=directory, passphrase_prompt=lambda: "passphrase").load(
            key
        )

    empty = tmp_path / "empty-vault"
    empty.touch()
    with pytest.raises(CredentialStoreError):
        EncryptedVaultCredentialStore(path=empty, passphrase_prompt=lambda: "passphrase").load(key)


def test_vault_constructor_rejects_invalid_dependencies(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EncryptedVaultCredentialStore(path="vault")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EncryptedVaultCredentialStore(path=tmp_path / "vault", passphrase_prompt=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ("save", "load", "delete"))
def test_vault_public_operations_fail_closed_on_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    store = EncryptedVaultCredentialStore(
        path=tmp_path / "vault", passphrase_prompt=lambda: "passphrase"
    )
    monkeypatch.setattr(store, "_read_vault", lambda: encrypted_vault._VaultRead(None, True))
    with pytest.raises(CredentialStoreError):
        if method == "save":
            store.save(CredentialKey("spotify", "id"), "value")
        else:
            getattr(store, method)(CredentialKey("spotify", "id"))


def test_vault_save_fails_closed_when_randomness_decryption_or_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedVaultCredentialStore(
        path=tmp_path / "vault", passphrase_prompt=lambda: "passphrase"
    )
    key = CredentialKey("spotify", "id")
    monkeypatch.setattr(encrypted_vault, "_new_salt", lambda: None)
    with pytest.raises(CredentialStoreError):
        store.save(key, "value")

    monkeypatch.setattr(
        store,
        "_read_vault",
        lambda: encrypted_vault._VaultRead((b"s" * 16, b"n" * 12, b"x" * 16), False),
    )
    monkeypatch.setattr(store, "_decrypt", lambda _document: None)
    with pytest.raises(CredentialStoreError):
        store.save(key, "value")

    monkeypatch.setattr(store, "_read_vault", lambda: encrypted_vault._VaultRead(None, False))
    monkeypatch.setattr(encrypted_vault, "_new_salt", lambda: b"s" * 16)
    monkeypatch.setattr(store, "_write_vault", lambda _salt, _records: False)
    with pytest.raises(CredentialStoreError):
        store.save(key, "value")


def test_vault_key_derivation_and_encryption_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedVaultCredentialStore(
        path=tmp_path / "vault", passphrase_prompt=lambda: "passphrase"
    )
    monkeypatch.setattr(
        encrypted_vault,
        "hash_secret_raw",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError()),
    )
    assert store._key_for(b"s" * 16) is None
    assert store._write_vault(b"s" * 16, {}) is False
