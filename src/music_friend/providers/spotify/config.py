"""Explicit, secret-free Spotify configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_FIXED_REDIRECT = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/callback\Z")


@dataclass(frozen=True, slots=True)
class SpotifySettings:
    """Public Spotify application settings supplied by the caller."""

    client_id: str
    redirect_uri: str | None = None


def load_spotify_settings(values: Mapping[str, object]) -> SpotifySettings:
    """Load only the two supported nonsecret settings from an explicit mapping."""
    client_id = values.get("SPOTIFY_CLIENT_ID")
    if (
        not isinstance(client_id, str)
        or not client_id
        or client_id.strip() != client_id
        or len(client_id) > 256
    ):
        raise ValueError("SPOTIFY_CLIENT_ID must contain 1..256 non-whitespace characters")

    redirect_value = values.get("SPOTIFY_REDIRECT_URI")
    if redirect_value is None or redirect_value == "":
        redirect_uri = None
    elif isinstance(redirect_value, str):
        match = _FIXED_REDIRECT.fullmatch(redirect_value)
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("SPOTIFY_REDIRECT_URI must be an exact fixed loopback callback")
        redirect_uri = redirect_value
    else:
        raise ValueError("SPOTIFY_REDIRECT_URI must be a string")

    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)


__all__ = ["SpotifySettings", "load_spotify_settings"]
