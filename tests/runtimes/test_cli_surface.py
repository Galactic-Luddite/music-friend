"""Command-line interface contracts using only local synthetic state."""

from __future__ import annotations

import io
import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.configuration import LocalConfig, LocalConfigStore
from music_friend.domain import (
    Artist,
    IdentityConfidence,
    RefreshKind,
    RefreshMetric,
    RefreshMetricKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
)
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.oauth import AuthorizationResult
from music_friend.runtimes import cli
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class _ConfigStore:
    def __init__(
        self, config: LocalConfig = LocalConfig(spotify_client_id="public-client")
    ) -> None:
        self.config = config
        self.saved: list[LocalConfig] = []

    def load(self) -> LocalConfig:
        return self.config

    def save(self, config: LocalConfig) -> None:
        self.config = config
        self.saved.append(config)


class _EmptyCredentialStore:
    def save(self, _key: CredentialKey, _value: str) -> None:
        raise AssertionError("unused")

    def load(self, _key: CredentialKey) -> None:
        return None

    def delete(self, _key: CredentialKey) -> None:
        raise AssertionError("unused")


class _FailingCredentialStore:
    def save(self, _key: CredentialKey, _value: str) -> None:
        raise AssertionError("unused")

    def load(self, _key: CredentialKey) -> None:
        raise RuntimeError("credential-store-canary")

    def delete(self, _key: CredentialKey) -> None:
        raise AssertionError("unused")


class _TrackingCredentialStore:
    def __init__(self) -> None:
        self.deleted = False

    def save(self, _key: CredentialKey, _value: str) -> None:
        pass

    def load(self, _key: CredentialKey) -> None:
        return None

    def delete(self, _key: CredentialKey) -> None:
        self.deleted = True


class _InvalidConfigStore:
    def load(self) -> LocalConfig:
        raise ValueError("config-canary")

    def save(self, _config: LocalConfig) -> None:
        raise AssertionError("unused")


class _SetupCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.saves: list[tuple[CredentialKey, str]] = []
        self.deletes: list[CredentialKey] = []

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value
        self.saves.append((key, value))

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)
        self.deletes.append(key)


class _FakeNativeCredentialStore(_SetupCredentialStore):
    pass


class _FakeVaultCredentialStore(_SetupCredentialStore):
    pass


class _RejectingSetupCredentialStore(_SetupCredentialStore):
    def save(self, _key: CredentialKey, _value: str) -> None:
        raise RuntimeError("credential-save-canary")


class _RejectingSetupConfigStore(_ConfigStore):
    def save(self, _config: LocalConfig) -> None:
        raise RuntimeError("config-save-canary")


class _CommitThenRaiseCredentialStore(_SetupCredentialStore):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self._operation = operation
        self._raise_once = True

    def save(self, key: CredentialKey, value: str) -> None:
        super().save(key, value)
        if self._operation == "save" and self._raise_once:
            self._raise_once = False
            raise RuntimeError("credential-save-after-write-canary")

    def delete(self, key: CredentialKey) -> None:
        super().delete(key)
        if self._operation == "delete" and self._raise_once:
            self._raise_once = False
            raise RuntimeError("credential-delete-after-write-canary")


class _RollbackFailingCredentialStore(_SetupCredentialStore):
    def __init__(self) -> None:
        super().__init__()
        self.save_calls = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.save_calls += 1
        if self.save_calls == 1:
            super().save(key, value)
            raise RuntimeError("credential-save-after-write-canary")
        raise RuntimeError("credential-rollback-canary")


def _application(tmp_path: Path) -> MusicFriendApplication:
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    application = MusicFriendApplication(catalog)
    application.put_artist(
        Artist(
            "artist-1",
            "Artist One",
            (SourceReference("synthetic", "provider-artist-1", None, NOW),),
            IdentityConfidence.SOURCE_ONLY,
            NOW,
        )
    )
    return application


def _run(
    argv: list[str],
    application: MusicFriendApplication,
    config: _ConfigStore,
    *,
    refresh: object = None,
    prompt: object = None,
    secret_prompt: object = None,
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(
        argv,
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        refresh_runner=refresh,  # type: ignore[arg-type]
        prompt=prompt,  # type: ignore[arg-type]
        secret_prompt=(lambda _message: "") if secret_prompt is None else secret_prompt,  # type: ignore[arg-type]
        now=lambda: NOW,
        credential_store_factory=_EmptyCredentialStore,
    )
    return result, stdout.getvalue(), stderr.getvalue()


def test_cli_help_lists_the_complete_operational_command_surface(tmp_path: Path) -> None:
    """Catches a released command being absent from the only operational interface."""
    application = _application(tmp_path)
    result, stdout, stderr = _run(["--help"], application, _ConfigStore())

    assert result == 0
    assert stderr == ""
    for command in (
        "setup",
        "connect spotify",
        "disconnect spotify",
        "status",
        "refresh catalog|releases|events|all",
        "watchlist list",
        "inbox list|show",
        "data export|import|import-spotify|backup|restore|delete",
        "diagnostics",
        "schedule install|status|remove",
        "version",
    ):
        assert command in stdout
    application.close()


def test_cli_delegates_status_refresh_watchlist_and_inbox_as_deterministic_json(
    tmp_path: Path,
) -> None:
    """Catches JSON output drift, local state bypassing, or partial refreshes reported as success."""
    application = _application(tmp_path)
    config = _ConfigStore()
    refresh_calls: list[str] = []

    def refresh(kind: str) -> dict[str, object]:
        refresh_calls.append(kind)
        return {"kind": kind, "status": "partial"}

    status_result, status_stdout, status_stderr = _run(
        ["status", "--json"], application, config, refresh=refresh
    )
    refresh_result, refresh_stdout, refresh_stderr = _run(
        ["refresh", "all", "--json"], application, config, refresh=refresh
    )
    watch_result, watch_stdout, watch_stderr = _run(
        ["watchlist", "list", "--json"], application, config, refresh=refresh
    )
    inbox_result, inbox_stdout, inbox_stderr = _run(
        ["inbox", "list", "--json"], application, config, refresh=refresh
    )

    assert status_result == 0
    assert json.loads(status_stdout) == {
        "connected": False,
        "connection": "disconnected",
        "events": {"ready": False},
        "inbox": {"has_unread": False},
        "mcp_ready": True,
        "latest_refresh": None,
        "status": "ready",
    }
    assert status_stderr == ""
    assert refresh_result == 3
    assert json.loads(refresh_stdout) == {"kind": "all", "status": "partial"}
    assert refresh_stderr == ""
    assert refresh_calls == ["all"]
    assert watch_result == inbox_result == 0
    assert json.loads(watch_stdout) == {"items": []}
    assert json.loads(inbox_stdout) == {"items": []}
    assert watch_stderr == inbox_stderr == ""
    application.close()


def test_cli_text_output_describes_inspection_diagnostics_and_schedule_results(
    monkeypatch: object, tmp_path: Path
) -> None:
    """Catches non-JSON inspection commands collapsing useful local results to a generic message."""
    application = _application(tmp_path)
    config = _ConfigStore()
    monkeypatch.setattr(cli, "_schedule_platform", lambda: cli.SchedulePlatform.LINUX)  # type: ignore[attr-defined]
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)  # type: ignore[attr-defined]

    watch_result, watch_stdout, watch_stderr = _run(["watchlist", "list"], application, config)
    inbox_result, inbox_stdout, inbox_stderr = _run(["inbox", "list"], application, config)
    diagnostics_result, diagnostics_stdout, diagnostics_stderr = _run(
        ["diagnostics"], application, config
    )
    schedule_result, schedule_stdout, schedule_stderr = _run(
        ["schedule", "status"], application, config
    )

    assert watch_result == inbox_result == diagnostics_result == schedule_result == 0
    assert watch_stdout == "Watchlist: no monitored artists.\n"
    assert inbox_stdout == "Inbox: no items.\n"
    assert diagnostics_stdout == (
        "Music Friend diagnostics: ready\n"
        "Spotify configuration: available\n"
        "Event area: not configured\n"
        "Latest refresh: none\n"
    )
    assert schedule_stdout == "Schedule: not installed.\n"
    assert watch_stderr == inbox_stderr == diagnostics_stderr == schedule_stderr == ""
    application.close()


def test_diagnostics_source_limit_is_an_explicit_safe_allowlist(tmp_path: Path) -> None:
    """Catches source diagnostics leaking raw provider data or omitting refresh request counts."""
    application = _application(tmp_path)
    application.put_source_limit(
        SourceLimitObservation(
            "spotify",
            SourceLimitState.COOLING_DOWN,
            NOW,
            NOW + timedelta(seconds=120),
            False,
            2,
        )
    )
    application.put_refresh_run(
        RefreshRun(
            "refresh-1",
            "spotify",
            RefreshKind.RELEASES,
            RefreshStatus.PARTIAL,
            NOW,
            NOW,
            RefreshSummary(
                (
                    RefreshMetric(RefreshMetricKind.SOURCE_REQUESTS, 4),
                    RefreshMetric(RefreshMetricKind.LIMIT_PAUSES, 1),
                )
            ),
        )
    )

    result, stdout, stderr = _run(["diagnostics", "--json"], application, _ConfigStore())

    assert result == 0
    assert stderr == ""
    source_limit = json.loads(stdout)["source_limits"]["spotify"]
    assert source_limit == {
        "state": "cooling_down",
        "observed_at": NOW.isoformat(),
        "retry_at": (NOW + timedelta(seconds=120)).isoformat(),
        "retry_is_exact": False,
        "consecutive_limits": 2,
        "last_refresh_requests": 4,
        "last_refresh_pauses": 1,
    }
    assert set(source_limit) == {
        "state",
        "observed_at",
        "retry_at",
        "retry_is_exact",
        "consecutive_limits",
        "last_refresh_requests",
        "last_refresh_pauses",
    }
    application.close()


def test_cli_status_reports_credential_access_failure_as_unavailable_json(
    tmp_path: Path,
) -> None:
    """Catches an unusable credential boundary being misreported as a normal disconnect."""
    application = _application(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["status", "--json"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        credential_store_factory=_FailingCredentialStore,
    )

    assert result == 4
    assert json.loads(stdout.getvalue()) == {
        "connected": False,
        "connection": "unavailable",
        "events": {"ready": False},
        "inbox": {"has_unread": False},
        "mcp_ready": True,
        "latest_refresh": None,
        "status": "unavailable",
    }
    assert stderr.getvalue() == ""
    assert "credential-store-canary" not in stdout.getvalue()
    application.close()


def test_cli_status_reports_configuration_access_failure_as_unavailable_json(
    tmp_path: Path,
) -> None:
    """Catches a failed local configuration read being emitted as an unstructured command failure."""
    application = _application(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["status", "--json"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=_InvalidConfigStore(),  # type: ignore[arg-type]
        credential_store_factory=_EmptyCredentialStore,
    )

    assert result == 4
    assert json.loads(stdout.getvalue()) == {
        "connected": False,
        "connection": "unavailable",
        "events": {"ready": False},
        "inbox": {"has_unread": False},
        "mcp_ready": True,
        "latest_refresh": None,
        "status": "unavailable",
    }
    assert stderr.getvalue() == ""
    assert "config-canary" not in stdout.getvalue()
    application.close()


def test_cli_connect_and_disconnect_delegate_through_the_published_grammar(
    tmp_path: Path,
) -> None:
    """Catches lifecycle commands bypassing the normal command grammar or leaking connection errors."""
    application = _application(tmp_path)
    store = _TrackingCredentialStore()
    authorizer_calls: list[tuple[object, object]] = []

    class _Authorizer:
        def authorize(self, capabilities: object, *, mode: object) -> AuthorizationResult:
            authorizer_calls.append((capabilities, mode))
            return AuthorizationResult(True, frozenset())

    connect_stdout, connect_stderr = io.StringIO(), io.StringIO()
    connect_result = cli.run_cli(
        ["connect", "spotify"],
        stdout=connect_stdout,
        stderr=connect_stderr,
        application=application,
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        credential_store_factory=lambda: store,
        browser_opener=lambda _url: True,
        authorizer_factory=lambda _settings, _tokens, _opener: _Authorizer(),  # type: ignore[arg-type]
    )
    disconnect_stdout, disconnect_stderr = io.StringIO(), io.StringIO()
    disconnect_result = cli.run_cli(
        ["disconnect", "spotify"],
        stdout=disconnect_stdout,
        stderr=disconnect_stderr,
        application=application,
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        credential_store_factory=lambda: store,
    )
    failed_stdout, failed_stderr = io.StringIO(), io.StringIO()
    failed_result = cli.run_cli(
        ["connect", "spotify"],
        stdout=failed_stdout,
        stderr=failed_stderr,
        application=application,
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        credential_store_factory=lambda: store,
        browser_opener=lambda _url: True,
        authorizer_factory=lambda _settings, _tokens, _opener: type(
            "FailedAuthorizer",
            (),
            {
                "authorize": lambda _self, _capabilities, *, mode: AuthorizationResult(
                    False, frozenset()
                )
            },
        )(),  # type: ignore[arg-type]
    )

    assert connect_result == disconnect_result == 0
    assert connect_stdout.getvalue() == "Music Friend connected to Spotify.\n"
    assert disconnect_stdout.getvalue() == "Music Friend disconnected from Spotify.\n"
    assert connect_stderr.getvalue() == disconnect_stderr.getvalue() == ""
    assert authorizer_calls == [(frozenset(cli.Capability), cli.AuthorizationMode.DYNAMIC_LOOPBACK)]
    assert store.deleted is True
    assert failed_result == 1
    assert failed_stdout.getvalue() == ""
    assert failed_stderr.getvalue() == "Music Friend could not connect to Spotify.\n"
    application.close()


def test_cli_setup_is_interactive_and_unknown_or_sensitive_arguments_are_not_reflected(
    tmp_path: Path,
) -> None:
    """Catches a credential-bearing argument path or CLI error that echoes caller input."""
    application = _application(tmp_path)
    config = _ConfigStore(LocalConfig())

    answers = iter(("public-client-id", "", "", "", ""))
    setup_result, setup_stdout, setup_stderr = _run(
        ["setup"], application, config, prompt=lambda _message: next(answers)
    )
    invalid_result, invalid_stdout, invalid_stderr = _run(
        ["setup", "private-secret-canary"], application, config
    )

    assert setup_result == 0
    assert setup_stdout == "Music Friend setup complete.\n"
    assert setup_stderr == ""
    assert config.saved == [LocalConfig(spotify_client_id="public-client-id")]
    assert invalid_result == 2
    assert invalid_stdout == ""
    assert "private-secret-canary" not in invalid_stderr
    application.close()


@pytest.mark.parametrize(
    "store_type",
    (_FakeNativeCredentialStore, _FakeVaultCredentialStore),
    ids=("native", "vault"),
)
def test_cli_setup_enrolls_event_area_and_ticketmaster_key_in_protected_store(
    tmp_path: Path, store_type: type[_SetupCredentialStore]
) -> None:
    """Catches setup omitting the only safe enrollment path for optional events."""
    application = _application(tmp_path)
    config = _ConfigStore(LocalConfig())
    credentials = store_type()
    answers = iter(("public-client-id", "US", "94103", "", ""))
    secret = "ticketmaster-secret-canary"
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: secret,
        credential_store_factory=lambda: credentials,
    )

    assert result == 0
    assert stdout.getvalue() == "Music Friend setup complete.\n"
    assert stderr.getvalue() == ""
    assert config.config == LocalConfig(
        spotify_client_id="public-client-id",
        event_country_code="US",
        event_postal_code="94103",
        event_radius=50,
        event_radius_unit="miles",
    )
    assert credentials.saves == [(CredentialKey("ticketmaster", "discovery"), secret)]
    assert secret not in stdout.getvalue() + stderr.getvalue() + repr(config.config)
    application.close()


def test_cli_setup_uses_the_kilometer_default_radius_when_selected(tmp_path: Path) -> None:
    """Catches a kilometer setup inheriting the miles default radius."""
    application = _application(tmp_path)
    config = _ConfigStore(LocalConfig())
    answers = iter(("public-client-id", "US", "94103", "", "kilometers"))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: "",
        credential_store_factory=_SetupCredentialStore,
    )

    assert result == 0
    assert config.config.event_radius == 80
    assert config.config.event_radius_unit == "kilometers"
    assert stderr.getvalue() == ""
    application.close()


def test_cli_setup_never_writes_ticketmaster_key_to_local_config(tmp_path: Path) -> None:
    """Catches protected event enrollment leaking into the ordinary config document."""
    application = _application(tmp_path)
    config = LocalConfigStore(config_dir=tmp_path / "config")
    credentials = _SetupCredentialStore()
    answers = iter(("public-client-id", "US", "94103", "", ""))
    secret = "ticketmaster-secret-canary"
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: secret,
        credential_store_factory=lambda: credentials,
    )

    encoded = config.path.read_text(encoding="utf-8")
    assert result == 0
    assert secret not in encoded + stdout.getvalue() + stderr.getvalue()
    assert "ticketmaster" not in encoded
    assert credentials.load(CredentialKey("ticketmaster", "discovery")) == secret
    application.close()


def test_cli_setup_secret_prompt_refuses_noninteractive_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a protected-key prompt falling back to an echoing input stream."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())

    with pytest.raises(ValueError, match="interactive terminal"):
        cli._default_secret_prompt("Ticketmaster API key: ")


def test_cli_setup_secret_prompt_refuses_getpass_echo_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a getpass warning returning a key after the terminal can echo it."""
    monkeypatch.setattr(cli.sys, "stdin", type("Tty", (), {"isatty": lambda _self: True})())
    returned = False

    def unsafe_fallback(_message: str) -> str:
        nonlocal returned
        warnings.warn("echo fallback", cli.getpass.GetPassWarning)
        returned = True
        return "ticketmaster-secret-canary"

    monkeypatch.setattr(cli.getpass, "getpass", unsafe_fallback)

    with pytest.raises(ValueError, match="protected setup"):
        cli._default_secret_prompt("Ticketmaster API key: ")

    assert returned is False


@pytest.mark.parametrize("operation", ("save", "delete"))
def test_cli_setup_compensates_when_credential_backend_mutates_before_raising(
    tmp_path: Path, operation: str
) -> None:
    """Catches a backend reporting failure after it has already changed the protected key."""
    application = _application(tmp_path)
    initial = LocalConfig(spotify_client_id="prior-client")
    config = _ConfigStore(initial)
    credentials = _CommitThenRaiseCredentialStore(operation)
    key = CredentialKey("ticketmaster", "discovery")
    credentials.values[key] = "prior-ticketmaster-key"
    answers = iter(("public-client-id", "US", "94103", "", ""))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: (
            "replacement-ticketmaster-key" if operation == "save" else "-"
        ),
        credential_store_factory=lambda: credentials,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert config.config == initial
    assert config.saved == []
    assert credentials.values == {key: "prior-ticketmaster-key"}
    assert "credential-" not in stdout.getvalue() + stderr.getvalue()
    application.close()


def test_cli_setup_redacts_a_failed_credential_rollback(tmp_path: Path) -> None:
    """Catches a failed protected-store compensation disclosing backend detail."""
    application = _application(tmp_path)
    initial = LocalConfig(spotify_client_id="prior-client")
    config = _ConfigStore(initial)
    credentials = _RollbackFailingCredentialStore()
    key = CredentialKey("ticketmaster", "discovery")
    credentials.values[key] = "prior-ticketmaster-key"
    answers = iter(("public-client-id", "US", "94103", "", ""))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: "replacement-ticketmaster-key",
        credential_store_factory=lambda: credentials,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert config.config == initial
    assert config.saved == []
    assert credentials.save_calls == 2
    assert "credential-" not in stdout.getvalue() + stderr.getvalue()
    assert "replacement-ticketmaster-key" not in stdout.getvalue() + stderr.getvalue()
    application.close()


def test_cli_setup_blank_preserves_and_dash_clears_optional_event_enrollment(
    tmp_path: Path,
) -> None:
    """Catches a setup retry that silently drops or cannot intentionally remove optional state."""
    application = _application(tmp_path)
    initial = LocalConfig(
        spotify_client_id="public-client-id",
        event_country_code="US",
        event_postal_code="94103",
        event_radius=25,
        event_radius_unit="miles",
    )
    config = _ConfigStore(initial)
    credentials = _SetupCredentialStore()
    key = CredentialKey("ticketmaster", "discovery")
    credentials.values[key] = "existing-key"

    blank_answers = iter(("", "", "", "", ""))
    blank_stdout, blank_stderr = io.StringIO(), io.StringIO()
    blank_result = cli.run_cli(
        ["setup"],
        stdout=blank_stdout,
        stderr=blank_stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(blank_answers),
        secret_prompt=lambda _message: "",
        credential_store_factory=lambda: credentials,
    )

    clear_answers = iter(("", "-", "", "", ""))
    clear_stdout, clear_stderr = io.StringIO(), io.StringIO()
    clear_result = cli.run_cli(
        ["setup"],
        stdout=clear_stdout,
        stderr=clear_stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(clear_answers),
        secret_prompt=lambda _message: "-",
        credential_store_factory=lambda: credentials,
    )

    assert blank_result == clear_result == 0
    assert blank_stdout.getvalue() == clear_stdout.getvalue() == "Music Friend setup complete.\n"
    assert blank_stderr.getvalue() == clear_stderr.getvalue() == ""
    assert credentials.saves == []
    assert credentials.deletes == [key]
    assert config.config == LocalConfig(spotify_client_id="public-client-id")
    application.close()


def test_cli_setup_rejects_invalid_event_input_without_persisting_partial_state(
    tmp_path: Path,
) -> None:
    """Catches malformed event preferences writing a partial local setup."""
    application = _application(tmp_path)
    initial = LocalConfig(spotify_client_id="prior-client")
    config = _ConfigStore(initial)
    credentials = _SetupCredentialStore()
    answers = iter(("public-client-id", "USA", "94103", "", ""))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: "ticketmaster-secret-canary",
        credential_store_factory=lambda: credentials,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert config.config == initial
    assert credentials.saves == credentials.deletes == []
    assert "ticketmaster-secret-canary" not in stdout.getvalue() + stderr.getvalue()
    application.close()


def test_cli_setup_does_not_save_config_when_protected_key_enrollment_fails(tmp_path: Path) -> None:
    """Catches a credential failure leaving a new event area partially configured."""
    application = _application(tmp_path)
    initial = LocalConfig(spotify_client_id="prior-client")
    config = _ConfigStore(initial)
    credentials = _RejectingSetupCredentialStore()
    answers = iter(("public-client-id", "US", "94103", "", ""))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: "ticketmaster-secret-canary",
        credential_store_factory=lambda: credentials,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert config.config == initial
    assert config.saved == []
    assert credentials.values == {}
    assert "ticketmaster-secret-canary" not in stdout.getvalue() + stderr.getvalue()
    application.close()


def test_cli_setup_restores_the_prior_protected_key_when_config_save_fails(tmp_path: Path) -> None:
    """Catches a failed config write leaving the protected event key changed."""
    application = _application(tmp_path)
    initial = LocalConfig(spotify_client_id="prior-client")
    config = _RejectingSetupConfigStore(initial)
    credentials = _SetupCredentialStore()
    key = CredentialKey("ticketmaster", "discovery")
    credentials.values[key] = "prior-ticketmaster-key"
    answers = iter(("public-client-id", "US", "94103", "", ""))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["setup"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        prompt=lambda _message: next(answers),
        secret_prompt=lambda _message: "replacement-ticketmaster-key",
        credential_store_factory=lambda: credentials,
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert config.config == initial
    assert credentials.values == {key: "prior-ticketmaster-key"}
    assert credentials.saves == [
        (key, "replacement-ticketmaster-key"),
        (key, "prior-ticketmaster-key"),
    ]
    assert "replacement-ticketmaster-key" not in stdout.getvalue() + stderr.getvalue()
    application.close()


def test_cli_status_reports_optional_event_readiness_after_setup(tmp_path: Path) -> None:
    """Catches a successful event enrollment that remains invisible to normal status."""
    application = _application(tmp_path)
    config = _ConfigStore(
        LocalConfig(
            spotify_client_id="public-client-id",
            event_country_code="US",
            event_postal_code="94103",
            event_radius=50,
            event_radius_unit="miles",
        )
    )
    credentials = _SetupCredentialStore()
    credentials.values[CredentialKey("ticketmaster", "discovery")] = "ticketmaster-secret-canary"
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["status", "--json"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        credential_store_factory=lambda: credentials,
    )

    assert result == 0
    assert json.loads(stdout.getvalue())["events"] == {"ready": True}
    assert stderr.getvalue() == ""
    assert "ticketmaster-secret-canary" not in stdout.getvalue()
    application.close()


def test_cli_status_text_includes_optional_event_readiness(tmp_path: Path) -> None:
    """Catches text status hiding whether configured events can run."""
    application = _application(tmp_path)
    config = _ConfigStore(
        LocalConfig(
            spotify_client_id="public-client-id",
            event_country_code="US",
            event_postal_code="94103",
            event_radius=50,
            event_radius_unit="miles",
        )
    )
    credentials = _SetupCredentialStore()
    credentials.values[CredentialKey("ticketmaster", "discovery")] = "ticketmaster-secret-canary"
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["status"],
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=config,  # type: ignore[arg-type]
        credential_store_factory=lambda: credentials,
    )

    assert result == 0
    assert (
        stdout.getvalue()
        == "Music Friend: ready (Spotify: disconnected; Events: ready; MCP: ready)\n"
    )
    assert stderr.getvalue() == ""
    application.close()


def test_cli_requires_interactive_confirmation_before_local_restore_or_delete(
    tmp_path: Path,
) -> None:
    """Catches a destructive lifecycle command that runs without explicit human confirmation."""
    application = _application(tmp_path)
    config = _ConfigStore()
    existing = application.get_artist("artist-1")

    delete_result, delete_stdout, delete_stderr = _run(
        ["data", "delete"], application, config, prompt=lambda _message: "no"
    )
    restore_result, restore_stdout, restore_stderr = _run(
        ["data", "restore", "missing.json"], application, config, prompt=lambda _message: "no"
    )

    assert delete_result == restore_result == 2
    assert delete_stdout == restore_stdout == ""
    assert delete_stderr == "Confirmation was not accepted.\n"
    assert restore_stderr == "Confirmation was not accepted.\n"
    assert application.get_artist("artist-1") == existing
    application.close()
