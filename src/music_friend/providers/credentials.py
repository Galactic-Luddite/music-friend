"""Provider-neutral credential storage boundary."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Protocol, runtime_checkable

_SOURCE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


class CredentialStoreError(RuntimeError):
    """Stable failure raised when protected local storage is unavailable."""

    def __init__(self) -> None:
        super().__init__("Credential storage is unavailable.")


class CredentialKey:
    """Opaque, source- and client-scoped key for one protected value."""

    __slots__ = ("_service",)

    def __init__(self, source: str, client_id: str) -> None:
        if type(source) is not str or _SOURCE.fullmatch(source) is None:
            raise ValueError("source must be a normalized identifier")
        if type(client_id) is not str:
            raise ValueError("client identifier must be text")
        normalized = unicodedata.normalize("NFKC", client_id).strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("client identifier must contain 1..256 characters")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        self._service = f"music-friend:{source}:{digest}"

    def __eq__(self, other: object) -> bool:
        return type(other) is CredentialKey and self._service == other._service

    def __hash__(self) -> int:
        return hash(self._service)

    def __repr__(self) -> str:
        return "CredentialKey()"


@runtime_checkable
class CredentialStore(Protocol):
    """Store opaque protected values under opaque local keys."""

    def save(self, key: CredentialKey, value: str) -> None: ...

    def load(self, key: CredentialKey) -> str | None: ...

    def delete(self, key: CredentialKey) -> None: ...


__all__ = ["CredentialKey", "CredentialStore", "CredentialStoreError"]
