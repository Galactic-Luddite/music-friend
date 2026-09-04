"""Interactive, encrypted fallback for protected local credentials."""

from __future__ import annotations

import getpass
import json
import secrets
import stat
from base64 import b64decode, b64encode
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeAlias

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from music_friend._local_files import atomic_replace
from music_friend.providers.credentials import CredentialKey, CredentialStoreError

_VERSION = 1
_MAX_FILE_BYTES = 1_048_576
_MAX_VALUE_LENGTH = 16_384
_MAX_RECORDS = 64
_SALT_LENGTH = 16
_NONCE_LENGTH = 12
_KEY_LENGTH = 32
_TAG_LENGTH = 16
_HEADER: dict[str, object] = {
    "kdf": {
        "memory_cost_kib": 65_536,
        "parallelism": 4,
        "time_cost": 3,
        "type": "argon2id",
    },
    "version": _VERSION,
}
_AAD = json.dumps(_HEADER, sort_keys=True, separators=(",", ":")).encode("ascii")
_VaultDocument: TypeAlias = tuple[bytes, bytes, bytes]


@dataclass(frozen=True, slots=True)
class _VaultRead:
    document: _VaultDocument | None
    failed: bool


def _prompt_for_passphrase() -> str:
    return getpass.getpass("Music Friend vault passphrase: ")


class EncryptedVaultCredentialStore:
    """Store credentials in a passphrase-protected local vault for interactive use only."""

    scheduled_eligible = False

    def __init__(
        self,
        *,
        path: Path,
        passphrase_prompt: Callable[[], str] = _prompt_for_passphrase,
    ) -> None:
        if not isinstance(path, Path) or not callable(passphrase_prompt):
            raise ValueError("vault path and passphrase prompt are required")
        self._path = path
        self._passphrase_prompt = passphrase_prompt
        self._derived_key: bytes | None = None
        self._salt: bytes | None = None

    def save(self, key: CredentialKey, value: str) -> None:
        if (
            type(key) is not CredentialKey
            or type(value) is not str
            or not value
            or len(value) > _MAX_VALUE_LENGTH
        ):
            self._raise_unavailable()
        vault = self._read_vault()
        if vault.failed:
            self._raise_unavailable()
        if vault.document is None:
            salt = _new_salt()
            if salt is None:
                self._raise_unavailable()
            records: dict[str, str] = {}
        else:
            decrypted = self._decrypt(vault.document)
            if decrypted is None:
                self._raise_unavailable()
            salt, records = decrypted
        if key._service not in records and len(records) >= _MAX_RECORDS:
            self._raise_unavailable()
        records[key._service] = value
        if not self._write_vault(salt, records):
            self._raise_unavailable()

    def load(self, key: CredentialKey) -> str | None:
        if type(key) is not CredentialKey:
            self._raise_unavailable()
        vault = self._read_vault()
        if vault.failed:
            self._raise_unavailable()
        if vault.document is None:
            return None
        decrypted = self._decrypt(vault.document)
        if decrypted is None:
            self._raise_unavailable()
        _salt, records = decrypted
        return records.get(key._service)

    def delete(self, key: CredentialKey) -> None:
        if type(key) is not CredentialKey:
            self._raise_unavailable()
        vault = self._read_vault()
        if vault.failed:
            self._raise_unavailable()
        if vault.document is None:
            return
        decrypted = self._decrypt(vault.document)
        if decrypted is None:
            self._raise_unavailable()
        salt, records = decrypted
        if key._service not in records:
            return
        del records[key._service]
        if not self._write_vault(salt, records):
            self._raise_unavailable()

    def _read_vault(self) -> _VaultRead:
        try:
            if not self._path.exists():
                return _VaultRead(None, False)
            metadata = self._path.stat()
            if (
                self._path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_FILE_BYTES
            ):
                return _VaultRead(None, True)
            document = _parse_document(self._path.read_bytes())
            return _VaultRead(document, document is None)
        except Exception:
            return _VaultRead(None, True)

    def _decrypt(self, encrypted: _VaultDocument) -> tuple[bytes, dict[str, str]] | None:
        salt, nonce, ciphertext = encrypted
        key = self._key_for(salt)
        if key is None:
            return None
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, _AAD)
        except Exception:
            return None
        records = _parse_records(plaintext)
        if records is None:
            return None
        return salt, records

    def _write_vault(self, salt: bytes, records: dict[str, str]) -> bool:
        key = self._key_for(salt)
        if key is None:
            return False
        try:
            plaintext = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(plaintext) > _MAX_FILE_BYTES:
                return False
            nonce = secrets.token_bytes(_NONCE_LENGTH)
            ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, _AAD)
            encoded = json.dumps(
                {
                    "ciphertext": b64encode(ciphertext).decode("ascii"),
                    "header": _HEADER,
                    "nonce": b64encode(nonce).decode("ascii"),
                    "salt": b64encode(salt).decode("ascii"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            return len(encoded) <= _MAX_FILE_BYTES and atomic_replace(
                self._path, encoded, prefix=".vault-"
            )
        except Exception:
            return False

    def _key_for(self, salt: bytes) -> bytes | None:
        if self._derived_key is not None and self._salt == salt:
            return self._derived_key
        try:
            passphrase = self._passphrase_prompt()
        except Exception:
            return None
        if type(passphrase) is not str or not passphrase or len(passphrase) > _MAX_VALUE_LENGTH:
            return None
        try:
            key = hash_secret_raw(
                secret=passphrase.encode("utf-8"),
                salt=salt,
                time_cost=3,
                memory_cost=65_536,
                parallelism=4,
                hash_len=_KEY_LENGTH,
                type=Type.ID,
            )
        except Exception:
            return None
        self._derived_key = key
        self._salt = salt
        return key

    @staticmethod
    def _raise_unavailable() -> NoReturn:
        raise CredentialStoreError()


def _new_salt() -> bytes | None:
    try:
        return secrets.token_bytes(_SALT_LENGTH)
    except Exception:
        return None


def _parse_document(encoded: bytes) -> _VaultDocument | None:
    try:
        decoded: Any = json.loads(encoded.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
        if type(decoded) is not dict or set(decoded) != {"ciphertext", "header", "nonce", "salt"}:
            return None
        if decoded["header"] != _HEADER:
            return None
        salt = _decode_b64(decoded["salt"], _SALT_LENGTH)
        nonce = _decode_b64(decoded["nonce"], _NONCE_LENGTH)
        ciphertext = _decode_b64(decoded["ciphertext"], None)
        if salt is None or nonce is None or ciphertext is None:
            return None
        if len(ciphertext) < _TAG_LENGTH or len(ciphertext) > _MAX_FILE_BYTES:
            return None
        return salt, nonce, ciphertext
    except Exception:
        return None


def _parse_records(plaintext: bytes) -> dict[str, str] | None:
    try:
        decoded: Any = json.loads(
            plaintext.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if type(decoded) is not dict or len(decoded) > _MAX_RECORDS:
            return None
        records: dict[str, str] = {}
        for service, value in decoded.items():
            if type(service) is not str or not service.startswith("music-friend:"):
                return None
            if type(value) is not str or not value or len(value) > _MAX_VALUE_LENGTH:
                return None
            records[service] = value
        return records
    except Exception:
        return None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_b64(value: object, expected_length: int | None) -> bytes | None:
    if type(value) is not str or len(value) > _MAX_FILE_BYTES * 2:
        return None
    try:
        decoded = b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        return None
    if expected_length is not None and len(decoded) != expected_length:
        return None
    return decoded


__all__ = ["EncryptedVaultCredentialStore"]
