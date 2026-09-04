"""Tests for the local, nonsecret configuration boundary."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import music_friend._local_files as local_files
from music_friend.configuration import LocalConfig, LocalConfigStore


def test_local_config_round_trips_only_v2_nonsecret_location_settings(tmp_path: Path) -> None:
    """Adding arbitrary fields would permit plaintext credential persistence."""
    store = LocalConfigStore(config_dir=tmp_path)
    config = LocalConfig(
        spotify_client_id="public-client-id",
        event_country_code="US",
        event_postal_code="94000",
        event_radius=20,
        event_radius_unit="miles",
    )

    store.save(config)

    assert store.load() == config
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "event_country_code": "US",
        "event_postal_code": "94000",
        "event_radius": 20,
        "event_radius_unit": "miles",
        "spotify_client_id": "public-client-id",
        "version": 2,
    }
    assert store.path.stat().st_mode & 0o777 == 0o600


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
