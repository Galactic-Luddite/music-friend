"""Behavior tests for CLI operation boundaries and local lifecycle commands."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    RefreshKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
)
from music_friend.runtimes import cli
from music_friend.tools.refresh import RefreshInvocation

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)


class _ConfigStore:
    def __init__(self, value: object = LocalConfig()) -> None:
        self.value = value

    def load(self) -> object:
        return self.value

    def save(self, value: LocalConfig) -> None:
        self.value = value


class _DataApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def export_data(self, path: Path) -> object:
        self.calls.append(("export", path))
        return SimpleNamespace(record_count=7)

    def import_data(self, path: Path) -> object:
        self.calls.append(("import", path))
        return SimpleNamespace(record_count=5)

    def delete_data(self) -> None:
        self.calls.append(("delete", None))


def _run(argv: list[str], application: object, **kwargs: object) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(
        argv,
        stdout=stdout,
        stderr=stderr,
        application=application,  # type: ignore[arg-type]
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        secret_prompt=lambda _message: "",
        **kwargs,  # type: ignore[arg-type]
    )
    return result, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("argv", "confirmation", "expected_output", "expected_call"),
    (
        (
            ["data", "export", "archive.json"],
            "",
            "Data export complete.\n",
            ("export", Path("archive.json")),
        ),
        (
            ["data", "backup", "backup.json"],
            "",
            "Data export complete.\n",
            ("export", Path("backup.json")),
        ),
        (
            ["data", "import", "archive.json"],
            "",
            "Data import complete.\n",
            ("import", Path("archive.json")),
        ),
        (
            ["data", "restore", "backup.json"],
            "RESTORE",
            "Data restore complete.\n",
            ("import", Path("backup.json")),
        ),
        (["data", "delete"], "DELETE", "Local data deleted.\n", ("delete", None)),
    ),
)
def test_data_commands_perform_only_the_requested_confirmed_local_operation(
    argv: list[str],
    confirmation: str,
    expected_output: str,
    expected_call: tuple[str, object],
) -> None:
    application = _DataApplication()

    result, stdout, stderr = _run(argv, application, prompt=lambda _message: confirmation)

    assert result == 0
    assert stdout == expected_output
    assert stderr == ""
    assert application.calls == [expected_call]


@pytest.mark.parametrize("operation", ("restore", "delete"))
def test_destructive_data_commands_reject_inexact_confirmation(operation: str) -> None:
    application = _DataApplication()
    argv = ["data", operation]
    if operation == "restore":
        argv.append("backup.json")

    result, stdout, stderr = _run(argv, application, prompt=lambda _message: "no")

    assert result == 2
    assert stdout == ""
    assert stderr == "Confirmation was not accepted.\n"
    assert application.calls == []


def test_data_command_rejects_unknown_shapes_without_touching_local_data() -> None:
    application = _DataApplication()

    result, stdout, stderr = _run(["data", "export"], application)

    assert result == 2
    assert stdout == ""
    assert stderr.startswith("Usage: music-friend")
    assert application.calls == []


def test_cli_redacts_application_failures_and_closes_default_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Application:
        def __init__(self) -> None:
            self.closed = False

        def list_refresh_runs(self, *, limit: int) -> object:
            raise RuntimeError("private-provider-canary")

        def close(self) -> None:
            self.closed = True

    application = Application()
    monkeypatch.setattr(cli.Catalog, "open", lambda _path: object())
    monkeypatch.setattr(cli, "MusicFriendApplication", lambda _catalog: application)
    monkeypatch.setattr(cli, "user_data_path", lambda *_args, **_kwargs: Path("data"))
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["diagnostics"],
        stdout=stdout,
        stderr=stderr,
        config_store=_ConfigStore(),  # type: ignore[arg-type]
        secret_prompt=lambda _message: "",
    )

    assert result == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Music Friend could not complete the command.\n"
    assert "private-provider-canary" not in stderr.getvalue()
    assert application.closed is True


@pytest.mark.parametrize("value", (object(), 1))
def test_cli_rejects_invalid_configuration_store_boundaries(value: object) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    with pytest.raises(ValueError, match="config_store must provide load and save"):
        cli.run_cli(
            ["version"],
            stdout=stdout,
            stderr=stderr,
            application=object(),  # type: ignore[arg-type]
            config_store=value,
        )


def test_cli_rejects_noncallable_callback_boundaries() -> None:
    with pytest.raises(ValueError, match="prompts and now must be callable"):
        cli.run_cli(
            ["version"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            application=object(),  # type: ignore[arg-type]
            config_store=_ConfigStore(),  # type: ignore[arg-type]
            prompt=1,  # type: ignore[arg-type]
        )


def test_refresh_invocations_report_running_completed_and_invalid_results() -> None:
    run = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.CATALOG,
        RefreshStatus.SUCCEEDED,
        NOW,
        NOW,
        RefreshSummary(()),
    )

    assert cli._refresh_payload(RefreshInvocation(None, already_running=True)) == {
        "status": "partial"
    }
    assert cli._refresh_payload(RefreshInvocation(run, already_running=False)) == {
        "kind": "catalog",
        "status": "succeeded",
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
        "metrics": [],
    }
    with pytest.raises(ValueError, match="refresh result is invalid"):
        cli._refresh_payload({"status": "succeeded", "unexpected": True})


def test_schedule_install_remove_status_and_failure_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, object]] = []
    schedule_path = tmp_path / "schedule.conf"
    schedule_path.write_text("installed", encoding="utf-8")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_schedule_platform", lambda: cli.SchedulePlatform.LINUX)
    monkeypatch.setattr(
        cli,
        "render_schedule",
        lambda *_args, **_kwargs: SimpleNamespace(relative_path=Path("schedule.conf")),
    )
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda platform, **kwargs: calls.append(("install", (platform, kwargs))),
    )
    monkeypatch.setattr(
        cli,
        "remove_schedule",
        lambda platform, **kwargs: calls.append(("remove", (platform, kwargs))),
    )

    assert _run(["schedule", "install"], object())[:2] == (0, "Schedule: installed.\n")
    assert _run(["schedule", "remove", "--json"], object())[:2] == (
        0,
        '{"status":"removed"}\n',
    )
    assert _run(["schedule", "status"], object())[:2] == (0, "Schedule: installed.\n")
    assert [name for name, _value in calls] == ["install", "remove"]

    monkeypatch.setattr(
        cli, "install_schedule", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    result, stdout, stderr = _run(["schedule", "install"], object())
    assert (result, stdout, stderr) == (
        1,
        "",
        "Music Friend could not complete the command.\n",
    )


@pytest.mark.parametrize(
    ("platform", "expected"),
    (
        ("darwin", cli.SchedulePlatform.MACOS),
        ("win32", cli.SchedulePlatform.WINDOWS),
        ("linux", cli.SchedulePlatform.LINUX),
    ),
)
def test_schedule_platform_matches_supported_operating_systems(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: cli.SchedulePlatform
) -> None:
    monkeypatch.setattr(cli.sys, "platform", platform)
    assert cli._schedule_platform() is expected


def test_human_readable_output_covers_status_records_and_nonempty_lists() -> None:
    assert cli._text({"version": "1.2.3"}) == "Music Friend version 1.2.3"
    assert cli._text({"kind": "events", "status": "partial"}) == "Refresh events: partial."
    assert cli._text({"records": 4}) == "Records processed: 4."
    assert cli._text({"installed": False}) == "Schedule: not installed."
    assert cli._watchlist_text([{"artist": {"display_name": "Artist One"}}]) == (
        "Watchlist: Artist One."
    )
    assert cli._inbox_text([{"state": "unread"}, {"state": "saved"}]) == (
        "Inbox: 2 item(s), 1 unread."
    )
    assert cli._inbox_detail_text({"entry": "invalid"}) == ("Inbox item details are unavailable.")


def test_checked_at_requires_an_aware_datetime_and_normalizes_to_utc() -> None:
    assert cli._checked_at(lambda: NOW) == NOW
    for value in ("not-a-date", datetime(2026, 9, 2)):
        with pytest.raises(ValueError, match="clock is invalid"):
            cli._checked_at(lambda value=value: value)  # type: ignore[arg-type]
