"""Tests for agent-driven setup and non-interactive operations (issue #33)."""

from __future__ import annotations

import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from music_friend.configuration import LocalConfig
from music_friend.providers.ticketmaster import TICKETMASTER_CREDENTIAL_KEY
from music_friend.runtimes import cli

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)


class _ConfigStore:
    def __init__(self, value: LocalConfig | None = None) -> None:
        self.value = value or LocalConfig()

    def load(self) -> LocalConfig:
        return self.value

    def save(self, value: LocalConfig) -> None:
        self.value = value


class _CredentialStore:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def load(self, key: str) -> str | None:
        return self.store.get(key)

    def save(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class _Application:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def delete_data(self) -> None:
        self.calls.append(("delete", None))

    def import_data(self, path: Path) -> object:
        self.calls.append(("import", path))
        return SimpleNamespace(record_count=5)

    def export_data(self, path: Path) -> object:
        self.calls.append(("export", path))
        return SimpleNamespace(record_count=7)


def _run(
    argv: list[str],
    *,
    application: object | None = None,
    config_store: object | None = None,
    secret_prompt: object | None = None,
    credential_store_factory: object | None = None,
    **kwargs: object,
) -> tuple[int, str, str]:
    """Run CLI and capture output."""
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(
        argv,
        stdout=stdout,
        stderr=stderr,
        application=application or _Application(),
        config_store=config_store or _ConfigStore(),
        secret_prompt=secret_prompt or (lambda _message: ""),
        credential_store_factory=credential_store_factory or (lambda: _CredentialStore()),
        now=lambda: NOW,
        **kwargs,
    )
    return result, stdout.getvalue(), stderr.getvalue()


def test_setup_with_flags_non_tty() -> None:
    """Setup with flags should work without TTY."""
    config_store = _ConfigStore()
    credential_store = _CredentialStore()

    exit_code, stdout, stderr = _run(
        ["setup", "--spotify-client-id", "example-id"],
        config_store=config_store,
        credential_store_factory=lambda: credential_store,
    )

    assert exit_code == 0
    assert config_store.value.spotify_client_id == "example-id"


def test_setup_with_all_flags() -> None:
    """Setup with all event area flags should configure fully."""
    config_store = _ConfigStore()

    exit_code, stdout, stderr = _run(
        [
            "setup",
            "--spotify-client-id", "example-id",
            "--event-country", "US",
            "--event-postal", "94110",
            "--event-radius", "50",
            "--event-unit", "miles",
        ],
        config_store=config_store,
    )

    assert exit_code == 0
    assert config_store.value.spotify_client_id == "example-id"
    assert config_store.value.event_country_code == "US"
    assert config_store.value.event_postal_code == "94110"
    assert config_store.value.event_radius == 50
    assert config_store.value.event_radius_unit == "miles"


def test_setup_clear_flag() -> None:
    """--clear-<field> should remove a field."""
    prior = LocalConfig(
        spotify_client_id="example-id",
        event_country_code="US",
        event_postal_code="94110",
        event_radius=50,
        event_radius_unit="miles",
    )
    config_store = _ConfigStore(prior)

    exit_code, stdout, stderr = _run(
        ["setup", "--clear-spotify-client-id"],
        config_store=config_store,
    )

    assert exit_code == 0
    assert config_store.value.spotify_client_id is None
    assert config_store.value.event_country_code == "US"


def test_setup_with_json_output() -> None:
    """Setup with --json should emit structured output."""
    config_store = _ConfigStore()

    exit_code, stdout, stderr = _run(
        ["setup", "--spotify-client-id", "example-id", "--json"],
        config_store=config_store,
    )

    assert exit_code == 0
    payload = json.loads(stdout)
    assert payload["status"] == "setup_complete"
    assert payload["spotify_client_id"] == "example-id"


def test_data_delete_with_yes_flag() -> None:
    """data delete --yes should succeed without prompt."""
    application = _Application()

    exit_code, stdout, stderr = _run(
        ["data", "delete", "--yes"],
        application=application,
    )

    assert exit_code == 0
    assert ("delete", None) in application.calls
    assert "deleted" in stdout


def test_data_delete_without_flag_no_tty() -> None:
    """data delete without flag should fail when no TTY."""
    application = _Application()

    exit_code, stdout, stderr = _run(
        ["data", "delete"],
        application=application,
        prompt=lambda _: (_ for _ in ()).throw(EOFError()),
    )

    assert exit_code == 2
    assert "Confirmation was not accepted" in stderr
    assert len(application.calls) == 0


def test_data_restore_with_confirm_flag() -> None:
    """data restore --confirm should accept confirmation token."""
    application = _Application()
    with tempfile.NamedTemporaryFile() as f:
        exit_code, stdout, stderr = _run(
            ["data", "restore", f.name, "--confirm", "RESTORE"],
            application=application,
        )

    assert exit_code == 0
    assert ("import", Path(f.name)) in application.calls
    assert "restore complete" in stdout


def test_data_restore_with_wrong_confirm_token() -> None:
    """data restore --confirm with wrong token should fail."""
    application = _Application()
    with tempfile.NamedTemporaryFile() as f:
        exit_code, stdout, stderr = _run(
            ["data", "restore", f.name, "--confirm", "wrong"],
            application=application,
        )

    assert exit_code == 2
    assert "Confirmation was not accepted" in stderr
    assert len(application.calls) == 0


def test_setup_key_from_env() -> None:
    """Setup with --ticketmaster-key-env should read from environment."""
    import os

    config_store = _ConfigStore()
    credential_store = _CredentialStore()

    # Set environment variable
    os.environ["TEST_KEY"] = "secret-key-value"
    try:
        exit_code, stdout, stderr = _run(
            ["setup", "--ticketmaster-key-env", "TEST_KEY"],
            config_store=config_store,
            credential_store_factory=lambda: credential_store,
        )

        assert exit_code == 0
        assert credential_store.load(TICKETMASTER_CREDENTIAL_KEY) == "secret-key-value"
        # Key should not appear in any output
        assert "secret-key-value" not in stdout
        assert "secret-key-value" not in stderr
    finally:
        del os.environ["TEST_KEY"]


def test_setup_key_from_file_requires_chmod_600() -> None:
    """Setup with --ticketmaster-key-file should reject non-600 files."""
    import os

    config_store = _ConfigStore()
    credential_store = _CredentialStore()

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("secret-key")
        key_file = f.name

    try:
        # Set wrong permissions
        os.chmod(key_file, 0o644)

        exit_code, stdout, stderr = _run(
            ["setup", "--ticketmaster-key-file", key_file],
            config_store=config_store,
            credential_store_factory=lambda: credential_store,
        )

        assert exit_code == 1
        assert "0o600" in stderr or "chmod 600" in stderr
    finally:
        os.unlink(key_file)


def test_setup_key_from_file_with_correct_perms() -> None:
    """Setup with --ticketmaster-key-file should work with chmod 600."""
    import os

    config_store = _ConfigStore()
    credential_store = _CredentialStore()

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("secret-key-from-file")
        key_file = f.name

    try:
        # Set correct permissions
        os.chmod(key_file, 0o600)

        exit_code, stdout, stderr = _run(
            ["setup", "--ticketmaster-key-file", key_file],
            config_store=config_store,
            credential_store_factory=lambda: credential_store,
        )

        assert exit_code == 0
        assert credential_store.load(TICKETMASTER_CREDENTIAL_KEY) == "secret-key-from-file"
        # Key should not appear in output
        assert "secret-key-from-file" not in stdout
        assert "secret-key-from-file" not in stderr
    finally:
        os.unlink(key_file)


def test_backward_compatibility_interactive_setup() -> None:
    """Setup with no flags should still work interactively."""
    config_store = _ConfigStore()

    def prompt_responses(message: str) -> str:
        if "Spotify" in message:
            return "spotify-id"
        elif "country" in message:
            return "US"
        elif "postal" in message:
            return "94110"
        elif "unit" in message:
            return "miles"
        elif "radius" in message:
            return "50"
        return ""

    exit_code, stdout, stderr = _run(
        ["setup"],
        config_store=config_store,
        prompt=prompt_responses,
    )

    assert exit_code == 0
    assert config_store.value.spotify_client_id == "spotify-id"
