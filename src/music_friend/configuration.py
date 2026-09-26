"""Nonsecret, per-user Music Friend configuration."""

from __future__ import annotations

import json
import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from platformdirs import user_config_path

from music_friend._local_files import atomic_replace

_VERSION = 3
_MAX_FILE_BYTES = 65_536
_COUNTRY_CODE = re.compile(r"[A-Z]{2}\Z")
_POSTAL_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{0,15}\Z")
RadiusUnit = Literal["miles", "kilometers"]
ReleaseSource = Literal["spotify", "musicbrainz"]


@dataclass(frozen=True, slots=True)
class LocalConfig:
    """Closed v3 schema for public Spotify, optional event-discovery, and release-source settings."""

    spotify_client_id: str | None = None
    event_country_code: str | None = None
    event_postal_code: str | None = None
    event_radius: int | float | None = None
    event_radius_unit: RadiusUnit | None = None
    release_source: ReleaseSource | None = None

    def __post_init__(self) -> None:
        if self.spotify_client_id is not None and not _is_client_id(self.spotify_client_id):
            raise ValueError("spotify_client_id must contain 1..256 non-whitespace characters")
        if self.event_country_code is not None and (
            type(self.event_country_code) is not str
            or _COUNTRY_CODE.fullmatch(self.event_country_code) is None
        ):
            raise ValueError("event_country_code must be a two-letter uppercase code")
        if self.event_postal_code is not None and (
            type(self.event_postal_code) is not str
            or _POSTAL_CODE.fullmatch(self.event_postal_code) is None
        ):
            raise ValueError("event_postal_code must be a normalized postal code")
        if self.event_radius is not None and not _is_radius(self.event_radius):
            raise ValueError("event_radius must be a finite value from 1 through 100")
        if self.event_radius_unit is not None and self.event_radius_unit not in {
            "miles",
            "kilometers",
        }:
            raise ValueError("event_radius_unit must be miles or kilometers")
        if self.release_source is not None and self.release_source not in {
            "spotify",
            "musicbrainz",
        }:
            raise ValueError("release_source must be spotify or musicbrainz")


class LocalConfigStore:
    """Persist the closed nonsecret configuration under the platform config directory."""

    def __init__(self, *, config_dir: Path | None = None) -> None:
        directory = (
            config_dir
            if config_dir is not None
            else user_config_path("music-friend", appauthor=False)
        )
        self._directory = Path(directory)

    @property
    def path(self) -> Path:
        return self._directory / "config.json"

    def load(self) -> LocalConfig:
        """Load one strict, bounded config document, or the first-run defaults."""
        path = self.path
        try:
            if not path.exists():
                return LocalConfig()
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_size > _MAX_FILE_BYTES
            ):
                raise ValueError
            decoded = json.loads(path.read_text(encoding="utf-8"))
            if type(decoded) is not dict:
                raise ValueError
            version = decoded.get("version")
            if type(version) is not int or version not in {2, 3}:
                raise ValueError
            # Version 2 read path: 5 keys without release_source
            if version == 2:
                if set(decoded) != {
                    "event_country_code",
                    "event_postal_code",
                    "event_radius",
                    "event_radius_unit",
                    "spotify_client_id",
                    "version",
                }:
                    raise ValueError
                return LocalConfig(
                    spotify_client_id=decoded["spotify_client_id"],
                    event_country_code=decoded["event_country_code"],
                    event_postal_code=decoded["event_postal_code"],
                    event_radius=decoded["event_radius"],
                    event_radius_unit=decoded["event_radius_unit"],
                    release_source=None,
                )
            # Version 3: 6 keys including release_source
            if set(decoded) != {
                "event_country_code",
                "event_postal_code",
                "event_radius",
                "event_radius_unit",
                "release_source",
                "spotify_client_id",
                "version",
            }:
                raise ValueError
            return LocalConfig(
                spotify_client_id=decoded["spotify_client_id"],
                event_country_code=decoded["event_country_code"],
                event_postal_code=decoded["event_postal_code"],
                event_radius=decoded["event_radius"],
                event_radius_unit=decoded["event_radius_unit"],
                release_source=decoded["release_source"],
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ValueError("local configuration is invalid") from None

    def save(self, config: LocalConfig) -> None:
        """Validate and atomically replace a canonical closed-schema configuration."""
        if type(config) is not LocalConfig:
            raise ValueError("config must be LocalConfig")
        canonical = LocalConfig(
            spotify_client_id=config.spotify_client_id,
            event_country_code=config.event_country_code,
            event_postal_code=config.event_postal_code,
            event_radius=config.event_radius,
            event_radius_unit=config.event_radius_unit,
            release_source=config.release_source,
        )
        encoded = json.dumps(
            {
                "event_country_code": canonical.event_country_code,
                "event_postal_code": canonical.event_postal_code,
                "event_radius": canonical.event_radius,
                "event_radius_unit": canonical.event_radius_unit,
                "release_source": canonical.release_source,
                "spotify_client_id": canonical.spotify_client_id,
                "version": _VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValueError("local configuration is invalid")
        if not atomic_replace(self.path, encoded, prefix=".config-"):
            raise ValueError("local configuration is unavailable")


def _is_client_id(value: object) -> bool:
    return type(value) is str and bool(value) and value.strip() == value and len(value) <= 256


def _is_radius(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    radius = cast(int | float, value)
    return math.isfinite(radius) and 1 <= radius <= 100


__all__ = ["LocalConfig", "LocalConfigStore", "RadiusUnit", "ReleaseSource"]
