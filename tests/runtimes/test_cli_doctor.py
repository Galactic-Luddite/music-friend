"""Onboarding preflight contracts: one local report of every blocker, without secret values."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from music_friend.configuration import LocalConfig
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.ticketmaster import TICKETMASTER_CREDENTIAL_KEY
from music_friend.runtimes import cli
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication

_COMPLETE = LocalConfig(
    spotify_client_id="public-client",
    event_country_code="US",
    event_postal_code="00000",
    event_radius=50,
    event_radius_unit="miles",
)


class _ConfigStore:
    def __init__(self, config: LocalConfig) -> None:
        self.config = config

    def load(self) -> LocalConfig:
        return self.config

    def save(self, _config: LocalConfig) -> None:
        raise AssertionError("doctor must not change configuration")


class _BrokenConfigStore(_ConfigStore):
    def load(self) -> LocalConfig:
        raise ValueError("config-canary")


class _KeyStore:
    def __init__(self, values: dict[CredentialKey, str] | None = None) -> None:
        self.values = values or {}

    def save(self, _key: CredentialKey, _value: str) -> None:
        raise AssertionError("doctor must not write credentials")

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, _key: CredentialKey) -> None:
        raise AssertionError("doctor must not delete credentials")


def _no_network(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("doctor must not contact a provider")


def _run(
    tmp_path: Path,
    argv: list[str],
    *,
    config_store: object,
    native: Callable[[], bool],
    credential_store_factory: Callable[[], object] = _KeyStore,
) -> tuple[int, str, str]:
    application = MusicFriendApplication(Catalog.open(tmp_path / "catalog.sqlite3"))
    stdout, stderr = io.StringIO(), io.StringIO()

    def _secret_prompt(_message: str) -> str:
        raise AssertionError("doctor must not prompt for a passphrase")

    try:
        result = cli.run_cli(
            argv,
            stdout=stdout,
            stderr=stderr,
            application=application,
            config_store=config_store,
            secret_prompt=_secret_prompt,
            connector_factory=lambda: httpx.MockTransport(_no_network),
            credential_store_factory=credential_store_factory,  # type: ignore[arg-type]
            native_store_probe=native,
        )
    finally:
        application.close()
    return result, stdout.getvalue(), stderr.getvalue()


def _fake_tokens(*, connected: bool) -> Callable[..., object]:
    class _Status:
        def __init__(self) -> None:
            self.connected = connected

    class _Tokens:
        def status(self) -> _Status:
            return _Status()

    @contextmanager
    def _manager(*_args: object, **_kwargs: object) -> Iterator[tuple[object, _Tokens]]:
        yield object(), _Tokens()

    return _manager


def test_doctor_names_connect_remedy_when_spotify_is_not_connected(tmp_path: Path) -> None:
    """Catches a configured but unconnected Spotify account passing preflight."""
    store = _KeyStore({TICKETMASTER_CREDENTIAL_KEY: "tm-secret-canary"})
    result, stdout, stderr = _run(
        tmp_path,
        ["doctor", "--json"],
        config_store=_ConfigStore(_COMPLETE),
        native=lambda: True,
        credential_store_factory=lambda: store,
    )

    checks = json.loads(stdout)["checks"]
    assert result == 5
    assert checks["spotify_connection"] == {
        "state": "failed",
        "remedy": "music-friend connect spotify",
    }
    for name in (
        "python",
        "credential_store",
        "spotify_client_id",
        "event_area",
        "ticketmaster_key",
    ):
        assert checks[name] == {"state": "ok", "remedy": None}
    assert "tm-secret-canary" not in stdout
    assert stderr == ""


def test_doctor_reports_ready_and_exits_zero_when_everything_is_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the ready verdict or exit code drifting from the individual checks."""
    monkeypatch.setattr(cli, "_spotify_tokens", _fake_tokens(connected=True))
    store = _KeyStore({TICKETMASTER_CREDENTIAL_KEY: "tm-secret-canary"})
    result, stdout, _stderr = _run(
        tmp_path,
        ["doctor"],
        config_store=_ConfigStore(_COMPLETE),
        native=lambda: True,
        credential_store_factory=lambda: store,
    )

    assert result == 0
    assert stdout.splitlines()[0] == "Music Friend doctor: ready"
    assert "tm-secret-canary" not in stdout


def test_doctor_flags_missing_native_store_without_opening_the_vault(tmp_path: Path) -> None:
    """Catches a headless host passing preflight even though MCP cannot read credentials."""

    def _vault() -> object:
        raise AssertionError("doctor must not open the passphrase vault")

    result, stdout, stderr = _run(
        tmp_path,
        ["doctor", "--json"],
        config_store=_ConfigStore(_COMPLETE),
        native=lambda: False,
        credential_store_factory=_vault,
    )

    checks = json.loads(stdout)["checks"]
    assert result == 5
    assert checks["credential_store"]["state"] == "failed"
    assert "MCP server" in checks["credential_store"]["remedy"]
    assert checks["spotify_connection"]["state"] == "unchecked"
    assert checks["ticketmaster_key"]["state"] == "unchecked"
    assert stderr == ""


def test_doctor_lists_every_missing_setting_in_one_text_report(tmp_path: Path) -> None:
    """Catches onboarding blockers being discovered one command at a time."""
    result, stdout, _stderr = _run(
        tmp_path, ["doctor"], config_store=_ConfigStore(LocalConfig()), native=lambda: True
    )

    assert result == 5
    assert stdout.splitlines()[0] == "Music Friend doctor: not ready"
    for line in (
        "[failed] spotify_client_id",
        "[unchecked] spotify_connection",
        "[failed] event_area",
        "[failed] ticketmaster_key",
    ):
        assert line in stdout


def test_doctor_survives_unreadable_configuration_and_failing_probe(tmp_path: Path) -> None:
    """Catches a broken config or probe crashing preflight or leaking its error text."""

    def _probe() -> bool:
        raise RuntimeError("probe-canary")

    result, stdout, stderr = _run(
        tmp_path,
        ["doctor", "--json"],
        config_store=_BrokenConfigStore(LocalConfig()),
        native=_probe,
    )

    checks = json.loads(stdout)["checks"]
    assert result == 5
    assert checks["credential_store"]["state"] == "failed"
    assert checks["spotify_client_id"]["state"] == "failed"
    assert "canary" not in stdout + stderr


def test_status_reports_mcp_not_ready_without_a_native_store(tmp_path: Path) -> None:
    """Catches status saying ready while the MCP server cannot open credentials."""
    result, stdout, _stderr = _run(
        tmp_path, ["status"], config_store=_ConfigStore(LocalConfig()), native=lambda: False
    )

    assert result == 0
    assert "MCP: needs a native credential store, run music-friend doctor" in stdout


def test_native_store_probe_reports_a_refused_backend_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the default probe treating a failing keyring backend as ready."""
    monkeypatch.undo()  # Drop the hermetic probe stub so the real probe runs.

    class _Refused:
        def __init__(self) -> None:
            raise RuntimeError("backend-canary")

    monkeypatch.setattr(cli, "KeyringCredentialStore", _Refused)
    assert cli._native_store_available() is False


def test_doctor_reports_unreadable_credentials_as_failed_without_leaking_errors(
    tmp_path: Path,
) -> None:
    """Catches a credential-store read error crashing preflight or passing as unchecked."""

    def _broken() -> object:
        raise RuntimeError("store-canary")

    result, stdout, stderr = _run(
        tmp_path,
        ["doctor", "--json"],
        config_store=_ConfigStore(_COMPLETE),
        native=lambda: True,
        credential_store_factory=_broken,
    )

    checks = json.loads(stdout)["checks"]
    assert result == 5
    assert checks["spotify_connection"]["state"] == "failed"
    assert checks["ticketmaster_key"]["state"] == "failed"
    assert "canary" not in stdout + stderr


def test_native_store_probe_reports_an_approved_backend_as_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the default probe rejecting a store that opened successfully."""
    monkeypatch.undo()  # Drop the hermetic probe stub so the real probe runs.
    monkeypatch.setattr(cli, "KeyringCredentialStore", object)
    assert cli._native_store_available() is True


def test_doctor_reports_unmapped_artists_for_a_non_empty_watchlist_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: doctor with a non-empty watchlist reports the unmapped count instead of
    raising AttributeError (Artist.source_refs, not Artist.refs) when
    release_source=musicbrainz (the default)."""
    from datetime import datetime, timezone

    from music_friend.domain import (
        Artist,
        IdentityConfidence,
        SourceReference,
        WatchlistAction,
        WatchlistOverride,
    )

    monkeypatch.setattr(cli, "_spotify_tokens", _fake_tokens(connected=True))
    now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    application = MusicFriendApplication(Catalog.open(tmp_path / "catalog.sqlite3"))
    artist = Artist(
        "artist-1",
        "Artist One",
        (SourceReference("spotify", "artist-native", None, now),),
        IdentityConfidence.SOURCE_ONLY,
        now,
    )
    application.put_artist(artist)
    application.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, now))
    application.close()

    store = _KeyStore({TICKETMASTER_CREDENTIAL_KEY: "tm-secret-canary"})
    result, stdout, stderr = _run(
        tmp_path,
        ["doctor", "--json"],
        config_store=_ConfigStore(_COMPLETE),
        native=lambda: True,
        credential_store_factory=lambda: store,
    )

    payload = json.loads(stdout)
    assert payload["release_source"] == {
        "source": "musicbrainz",
        "unmapped_artists": 1,
        "remedy": "Run 'music-friend refresh releases' to map artists",
    }
    assert stderr == ""
    assert result == 0
