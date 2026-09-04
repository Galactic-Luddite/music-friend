"""Private Spotify credential envelope and safe connection status."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_SCHEMA = 1
_MAX_ENVELOPE_LENGTH = 16_384
_MAX_REFRESH_TOKEN_LENGTH = 4096
_MAX_SCOPE_COUNT = 64
_SCOPE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_REFRESH_FIELD = "refresh_" + "token"


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Credential-free local connection status."""

    connected: bool
    granted_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.connected) is not bool:
            raise ValueError("connected must be a boolean")
        scopes = _validated_scopes(self.granted_scopes)
        if tuple(sorted(scopes)) != self.granted_scopes:
            raise ValueError("granted scopes must be sorted")
        if not self.connected and scopes:
            raise ValueError("disconnected status cannot contain scopes")


@dataclass(frozen=True, slots=True, repr=False)
class _SpotifyCredential:
    refresh_token: str
    granted_scopes: frozenset[str]

    def __post_init__(self) -> None:
        if (
            type(self.refresh_token) is not str
            or not self.refresh_token
            or len(self.refresh_token) > _MAX_REFRESH_TOKEN_LENGTH
        ):
            raise ValueError("credential envelope is invalid")
        object.__setattr__(self, "granted_scopes", _validated_scopes(self.granted_scopes))

    def __repr__(self) -> str:
        return "_SpotifyCredential()"


def _validated_scopes(value: object) -> frozenset[str]:
    if not isinstance(value, (tuple, list, set, frozenset)) or len(value) > _MAX_SCOPE_COUNT:
        raise ValueError("credential envelope is invalid")
    values = tuple(value)
    if not all(type(scope) is str and _SCOPE.fullmatch(scope) is not None for scope in values):
        raise ValueError("credential envelope is invalid")
    if len(set(values)) != len(values):
        raise ValueError("credential envelope is invalid")
    return frozenset(values)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("credential envelope is invalid")
        result[key] = value
    return result


def _encode_credential(credential: _SpotifyCredential) -> str:
    if type(credential) is not _SpotifyCredential:
        raise ValueError("credential envelope is invalid")
    encoded = json.dumps(
        {
            "schema": _SCHEMA,
            _REFRESH_FIELD: credential.refresh_token,
            "granted_scopes": sorted(credential.granted_scopes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > _MAX_ENVELOPE_LENGTH:
        raise ValueError("credential envelope is invalid")
    return encoded


def _decode_credential(value: str) -> _SpotifyCredential:
    credential: _SpotifyCredential | None = None
    try:
        if type(value) is not str or not value or len(value) > _MAX_ENVELOPE_LENGTH:
            raise ValueError("credential envelope is invalid")
        payload = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
        if type(payload) is not dict or set(payload) != {
            "schema",
            _REFRESH_FIELD,
            "granted_scopes",
        }:
            raise ValueError("credential envelope is invalid")
        if type(payload["schema"]) is not int or payload["schema"] != _SCHEMA:
            raise ValueError("credential envelope is invalid")
        scopes = payload["granted_scopes"]
        if type(scopes) is not list:
            raise ValueError("credential envelope is invalid")
        credential = _SpotifyCredential(payload[_REFRESH_FIELD], _validated_scopes(scopes))
    except (TypeError, json.JSONDecodeError, UnicodeError, ValueError):
        pass
    if credential is None:
        raise ValueError("credential envelope is invalid") from None
    return credential


__all__ = ["CredentialStatus"]
