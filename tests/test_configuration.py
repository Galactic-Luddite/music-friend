"""Tests for the local, nonsecret configuration boundary."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import music_friend._local_files as local_files
from music_friend.configuration import LocalConfig, LocalConfigStore


def test_local_config_round_trips_only_v4_nonsecret_location_settings(tmp_path: Path) -> None:
    """Adding arbitrary fields would permit plaintext credential persistence."""
    store = LocalConfigStore(config_dir=tmp_path)
    config = LocalConfig(
        spotify_client_id="public-client-id",
        event_country_code="US",
        event_postal_code="94000",
        event_radius=20,
        event_radius_unit="miles",
        release_sources=("musicbrainz", "deezer"),
    )

    store.save(config)

    assert store.load() == config
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "event_country_code": "US",
        "event_postal_code": "94000",
        "event_radius": 20,
        "event_radius_unit": "miles",
        "release_sources": ["musicbrainz", "deezer"],
        "spotify_client_id": "public-client-id",
        "version": 4,
    }
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_v2_config_loads_and_is_rewritten_as_v4(tmp_path: Path) -> None:
    """A version-2 file should load with the default release_sources and be upgraded to v4."""
    store = LocalConfigStore(config_dir=tmp_path)
    # Write a v2 config directly
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "event_country_code": "US",
                "event_postal_code": "94000",
                "event_radius": 20,
                "event_radius_unit": "miles",
                "spotify_client_id": "public-client-id",
                "version": 2,
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load()
    assert loaded.release_sources == ("musicbrainz",)
    assert loaded.spotify_client_id == "public-client-id"

    # Save should upgrade to v4
    store.save(loaded)
    saved_json = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved_json["version"] == 4
    assert saved_json["release_sources"] == ["musicbrainz"]


def test_v3_config_with_scalar_release_source_loads_and_is_rewritten_as_v4(
    tmp_path: Path,
) -> None:
    """A version-3 file's scalar release_source becomes a one-element v4 tuple."""
    store = LocalConfigStore(config_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "event_country_code": None,
                "event_postal_code": None,
                "event_radius": None,
                "event_radius_unit": None,
                "release_source": "spotify",
                "spotify_client_id": None,
                "version": 3,
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load()
    assert loaded.release_sources == ("spotify",)

    store.save(loaded)
    saved_json = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved_json["version"] == 4
    assert saved_json["release_sources"] == ["spotify"]


def test_v3_config_with_null_release_source_migrates_to_the_default(tmp_path: Path) -> None:
    """A version-3 file with release_source=null becomes the v4 default tuple."""
    store = LocalConfigStore(config_dir=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "event_country_code": None,
                "event_postal_code": None,
                "event_radius": None,
                "event_radius_unit": None,
                "release_source": None,
                "spotify_client_id": None,
                "version": 3,
            }
        ),
        encoding="utf-8",
    )

    assert store.load().release_sources == ("musicbrainz",)


def test_local_config_accepts_deezer_in_release_sources() -> None:
    """Deezer is now an allowed release source (issue #42)."""
    config = LocalConfig(release_sources=("musicbrainz", "deezer"))
    assert config.release_sources == ("musicbrainz", "deezer")


def test_local_config_rejects_unknown_release_source() -> None:
    """Unknown release_sources values must be rejected."""
    with pytest.raises(ValueError) as raised:
        LocalConfig(release_sources=("spotify", "not-a-source"))
    assert "release_sources must contain only spotify, musicbrainz, or deezer" in str(raised.value)


def test_local_config_rejects_empty_release_sources() -> None:
    """An empty release_sources tuple would silently disable all release discovery."""
    with pytest.raises(ValueError) as raised:
        LocalConfig(release_sources=())
    assert "release_sources must contain at least one source" in str(raised.value)


def test_local_config_rejects_duplicate_release_sources() -> None:
    """Duplicate entries would make refresh iterate the same source twice."""
    with pytest.raises(ValueError) as raised:
        LocalConfig(release_sources=("musicbrainz", "musicbrainz"))
    assert "release_sources must not contain duplicates" in str(raised.value)


def test_removing_deezer_from_release_sources_disables_it_with_no_migration(
    tmp_path: Path,
) -> None:
    """Dropping deezer from the tuple is a plain config edit, not a schema change."""
    store = LocalConfigStore(config_dir=tmp_path)
    store.save(LocalConfig(release_sources=("musicbrainz", "deezer")))
    store.save(LocalConfig(release_sources=("musicbrainz",)))

    assert store.load().release_sources == ("musicbrainz",)


def test_legacy_v1_config_is_rejected_without_echoing_its_contents(tmp_path: Path) -> None:
    """Accepting the former open document would reintroduce plaintext secret storage."""
    store = LocalConfigStore(config_dir=tmp_path)
    legacy_canary = "legacy-preference-canary"
    store.path.write_text(
        json.dumps(
            {
                "preferences": {"ticketmaster_key": legacy_canary},
                "spotify_client_id": "public-client-id",
                "version": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        store.load()

    assert str(raised.value) == "local configuration is invalid"
    assert legacy_canary not in repr(raised.value)


def test_local_config_has_no_mutable_preference_bag() -> None:
    """An open mutable mapping would let callers bypass protected credential storage."""
    with pytest.raises(TypeError):
        LocalConfig(preferences={"ticketmaster_key": "not-allowed"})  # type: ignore[call-arg]


@pytest.mark.parametrize("radius", (math.nan, math.inf, -math.inf))
def test_local_config_rejects_nonfinite_radius(radius: float) -> None:
    """Non-finite numbers would produce noncanonical JSON configuration."""
    with pytest.raises(ValueError):
        LocalConfig(event_radius=radius)


def test_save_revalidates_a_bypassed_frozen_config(tmp_path: Path) -> None:
    """Trusting mutated object fields would permit invalid persistence after construction."""
    config = LocalConfig(event_radius=20, event_radius_unit="miles")
    object.__setattr__(config, "event_radius", math.nan)
    store = LocalConfigStore(config_dir=tmp_path)

    with pytest.raises(ValueError):
        store.save(config)

    assert not store.path.exists()


def test_config_atomic_write_remains_usable_without_posix_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requiring a POSIX-only descriptor API would break configuration on Windows."""
    monkeypatch.delattr(local_files.os, "fchmod", raising=False)
    store = LocalConfigStore(config_dir=tmp_path)

    store.save(LocalConfig(event_country_code="US"))
    store.save(LocalConfig(event_country_code="CA"))

    assert store.load() == LocalConfig(event_country_code="CA")


def test_missing_local_config_is_an_empty_nonsecret_setup(tmp_path: Path) -> None:
    """Changing the default would make first-run configuration surprising."""
    assert LocalConfigStore(config_dir=tmp_path).load() == LocalConfig()
