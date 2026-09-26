"""Behavior tests for CLI operation boundaries and local lifecycle commands."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
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

    def import_spotify_history(self, path: Path, *, dry_run: bool = False) -> object:
        self.calls.append(("import_spotify_history", (path, dry_run)))
        return SimpleNamespace(
            imported=12,
            duplicates=3,
            non_music=4,
            member_count=2,
            first_played_at="2011-01-01T00:00:00Z",
            last_played_at="2026-01-01T00:00:00Z",
        )


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


class _FailingDataApplication:
    """Raises the given exception from whichever data-lifecycle method is exercised."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[tuple[str, object]] = []

    def export_data(self, path: Path) -> object:
        raise self._error

    def import_data(self, path: Path) -> object:
        raise self._error

    def import_spotify_history(self, path: Path, *, dry_run: bool = False) -> object:
        raise self._error

    def delete_data(self) -> None:
        raise self._error


@pytest.mark.parametrize(
    "argv",
    (
        ["data", "export", "archive.json"],
        ["data", "backup", "archive.json"],
        ["data", "import", "archive.json"],
        ["data", "import-spotify", "spotify.zip"],
    ),
)
def test_data_command_reports_a_distinct_message_for_a_missing_file(argv: list[str]) -> None:
    application = _FailingDataApplication(FileNotFoundError("supersecretcanary/private/path"))

    result, stdout, stderr = _run(argv, application)

    assert result == 1
    assert stdout == ""
    assert "could not find the file" in stderr
    assert "supersecretcanary" not in stderr


@pytest.mark.parametrize("argv", (["data", "export", "archive.json"], ["data", "backup", "b.json"]))
def test_data_command_reports_a_distinct_message_for_an_existing_destination(
    argv: list[str],
) -> None:
    application = _FailingDataApplication(FileExistsError("supersecretcanary/private/path"))

    result, stdout, stderr = _run(argv, application)

    assert result == 1
    assert stdout == ""
    assert "will not overwrite" in stderr
    assert "supersecretcanary" not in stderr


@pytest.mark.parametrize(
    "argv", (["data", "import", "archive.json"], ["data", "import-spotify", "s.zip"])
)
def test_data_command_reports_a_distinct_message_for_an_invalid_archive(argv: list[str]) -> None:
    application = _FailingDataApplication(ValueError("supersecretcanary: line 4 column 2"))

    result, stdout, stderr = _run(argv, application)

    assert result == 1
    assert stdout == ""
    assert "not a valid export" in stderr
    assert "supersecretcanary" not in stderr


@pytest.mark.parametrize("operation", ("restore", "delete"))
def test_data_command_reports_a_distinct_message_when_stdin_is_not_interactive(
    operation: str,
) -> None:
    application = _DataApplication()
    argv = ["data", operation]
    if operation == "restore":
        argv.append("backup.json")

    def _no_tty(_message: str) -> str:
        raise EOFError

    result, stdout, stderr = _run(argv, application, prompt=_no_tty)

    assert result == 2
    assert stdout == ""
    assert "no terminal is attached" in stderr
    assert application.calls == []


def test_restore_and_delete_still_reject_inexact_confirmation_with_original_message() -> None:
    """Existing exit code 2 / message contract for a rejected (but readable) confirmation
    must be unchanged."""
    application = _DataApplication()

    result, stdout, stderr = _run(
        ["data", "delete"], application, prompt=lambda _message: "definitely not"
    )

    assert result == 2
    assert stderr == "Confirmation was not accepted.\n"
    assert application.calls == []


@pytest.mark.parametrize("dry_run", (False, True))
def test_spotify_history_import_reports_bounded_counts(dry_run: bool) -> None:
    application = _DataApplication()
    argv = ["data", "import-spotify", "spotify.zip"]
    if dry_run:
        argv.append("--dry-run")
    argv.append("--json")

    result, stdout, stderr = _run(argv, application)

    assert result == 0
    assert stderr == ""
    assert application.calls == [("import_spotify_history", (Path("spotify.zip"), dry_run))]
    payload = json.loads(stdout)
    assert payload["imported"] == 12
    assert payload["non_music"] == 4


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


def test_events_only_skip_reports_a_distinct_status_and_exits_zero() -> None:
    """Catches an unconfigured event refresh being reported as a misleading success."""
    payload = cli._refresh_payload(
        RefreshInvocation(None, already_running=False, skip_reason="event_area_not_configured")
    )

    assert payload == {"status": "skipped", "reason": "event_area_not_configured"}
    stdout = io.StringIO()
    invocation = RefreshInvocation(None, False, "event_area_not_configured")
    assert cli._emit_refresh(invocation, True, stdout) == 0
    assert json.loads(stdout.getvalue()) == {
        "status": "skipped",
        "reason": "event_area_not_configured",
    }


def test_all_kind_run_carries_the_events_skip_reason_without_changing_overall_status() -> None:
    """Catches `refresh all` masking an unconfigured event area as a plain success."""
    run = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.ALL,
        RefreshStatus.SUCCEEDED,
        NOW,
        NOW,
        RefreshSummary(()),
    )

    payload = cli._refresh_payload(
        RefreshInvocation(run, already_running=False, skip_reason="event_area_not_configured")
    )

    assert payload["status"] == "succeeded"
    assert payload["events_skipped_reason"] == "event_area_not_configured"


def test_partial_refresh_payload_and_text_line_carry_reason_retry_after_and_remaining() -> None:
    """Catches a partial refresh losing its reason/retry_after/remaining (AC6)."""
    run = RefreshRun(
        "run-1",
        "spotify",
        RefreshKind.RELEASES,
        RefreshStatus.PARTIAL,
        NOW,
        NOW,
        RefreshSummary(()),
    )
    invocation = RefreshInvocation(
        run,
        already_running=False,
        reason="rate_limited",
        retry_after=NOW.isoformat(),
        remaining=3,
    )

    payload = cli._refresh_payload(invocation)

    assert payload["reason"] == "rate_limited"
    assert payload["retry_after"] == NOW.isoformat()
    assert payload["remaining"] == 3
    assert cli._text(payload) == (
        f"Refresh releases: partial. reason=rate_limited retry_after={NOW.isoformat()} remaining=3"
    )


def test_schedule_install_remove_status_and_failure_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_schedule_platform", lambda: cli.SchedulePlatform.LINUX)
    monkeypatch.setattr(
        cli,
        "schedule_status",
        lambda *_args, **_kwargs: cli.ScheduleStatus(
            installed=True,
            active=True,
            platform=cli.SchedulePlatform.LINUX,
            interval_minutes=1440,
        ),
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


def test_schedule_commands_use_daily_absolute_python_without_opening_catalog_or_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    base_python = tmp_path / "base" / "python"
    base_python.parent.mkdir()
    base_python.write_text("synthetic", encoding="utf-8")
    installed_python = tmp_path / "runtime" / "bin" / "python"
    installed_python.parent.mkdir(parents=True)
    installed_python.symlink_to(base_python)

    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_schedule_platform", lambda: cli.SchedulePlatform.MACOS)
    monkeypatch.setattr(cli.sys, "executable", str(installed_python))
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda platform, **kwargs: captured.update(platform=platform, **kwargs),
    )
    monkeypatch.setattr(
        cli.Catalog,
        "open",
        lambda _path: (_ for _ in ()).throw(AssertionError("catalog must not open")),
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(
        ["schedule", "install"],
        stdout=stdout,
        stderr=stderr,
        config_store=object(),
    )

    assert result == 0
    assert stderr.getvalue() == ""
    assert captured == {
        "platform": cli.SchedulePlatform.MACOS,
        "user_root": tmp_path,
        "command": (
            str(installed_python),
            "-m",
            "music_friend.runtimes.cli",
            "refresh",
            "all",
            "--json",
        ),
        "interval_minutes": 1440,
    }


def test_schedule_status_json_reports_installation_activation_platform_and_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_schedule_platform", lambda: cli.SchedulePlatform.LINUX)
    monkeypatch.setattr(
        cli,
        "schedule_status",
        lambda *_args, **_kwargs: cli.ScheduleStatus(
            installed=True,
            active=False,
            platform=cli.SchedulePlatform.LINUX,
            interval_minutes=1440,
        ),
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(["schedule", "status", "--json"], stdout=stdout, stderr=stderr)

    assert result == 0
    assert json.loads(stdout.getvalue()) == {
        "active": False,
        "installed": True,
        "interval_minutes": 1440,
        "platform": "linux",
    }
    assert stderr.getvalue() == ""


def test_setup_never_inspects_or_installs_a_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = iter(("", "", "", "", ""))
    monkeypatch.setattr(
        cli,
        "schedule_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("schedule inspected")),
    )
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("schedule installed")),
    )

    result, stdout, stderr = _run(["setup"], object(), prompt=lambda _message: next(answers))

    assert result == 0
    assert stdout == "Music Friend setup complete.\n"
    assert stderr == ""


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


def test_refresh_releases_uses_musicbrainz_and_never_opens_a_spotify_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: `music-friend refresh releases` (release_source=musicbrainz, the
    default) drives the real cli.main()/run_cli entry point through to
    MusicBrainz and never opens a Spotify token session at all."""
    import httpx

    from music_friend.store import Catalog
    from music_friend.tools import MusicFriendApplication

    @contextmanager
    def _refuse_spotify_source(**kwargs: object) -> object:
        raise AssertionError("a musicbrainz-only releases refresh must not open Spotify")
        yield  # pragma: no cover

    musicbrainz_calls: list[httpx.Request] = []

    def _musicbrainz_response(request: httpx.Request) -> httpx.Response:
        musicbrainz_calls.append(request)
        assert request.url.host == "musicbrainz.org"
        return httpx.Response(200, json={"urls": [], "url-count": 0, "url-offset": 0})

    def connector_factory() -> httpx.BaseTransport:
        return httpx.MockTransport(_musicbrainz_response)

    class _EventStore:
        def save(self, _key: object, _value: str) -> None:
            raise AssertionError("unused")

        def load(self, _key: object) -> str | None:
            return None

        def delete(self, _key: object) -> None:
            raise AssertionError("unused")

    monkeypatch.setattr(cli, "_spotify_source", _refuse_spotify_source)
    # refresh_once() acquires a real lock file at the lock_path _refresh() builds
    # from user_data_path(); redirect that to this test's own isolated tmp_path
    # so the run never touches the real platform user-data directory.
    monkeypatch.setattr(cli, "user_data_path", lambda *_args, **_kwargs: tmp_path)

    from music_friend.domain import (
        Artist,
        IdentityConfidence,
        SourceReference,
        WatchlistAction,
        WatchlistOverride,
    )

    application = MusicFriendApplication(Catalog.open(tmp_path / "catalog.sqlite3"))
    artist = Artist(
        "artist-1",
        "Artist One",
        (SourceReference("spotify", "artist-native", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    application.put_artist(artist)
    application.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, NOW))
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        result = cli.run_cli(
            ["refresh", "releases", "--json"],
            stdout=stdout,
            stderr=stderr,
            application=application,
            config_store=_ConfigStore(LocalConfig()),
            secret_prompt=lambda _message: "",
            connector_factory=connector_factory,
            credential_store_factory=lambda: _EventStore(),  # type: ignore[arg-type]
        )
    finally:
        application.close()

    assert result == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "succeeded"
    assert stderr.getvalue() == ""
    # The watchlisted artist's identity mapping step made a real MusicBrainz
    # request (proving MusicBrainz is actually in play, not just configured),
    # and _refuse_spotify_source never fired (proving Spotify was never
    # touched for this musicbrainz-only releases refresh).
    assert musicbrainz_calls


def test_refresh_releases_calls_musicbrainz_and_deezer_but_never_spotify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC (issue #42): with release_sources=("musicbrainz", "deezer"), a real
    `music-friend refresh releases` run through cli.run_cli() actually opens and
    calls both hosts (not just musicbrainz, the prior wiring's only source), and
    never opens a Spotify token session, since "spotify" is not configured."""
    import httpx

    from music_friend.store import Catalog
    from music_friend.tools import MusicFriendApplication

    @contextmanager
    def _refuse_spotify_source(**kwargs: object) -> object:
        raise AssertionError("spotify must not be opened when it is not a configured source")
        yield  # pragma: no cover

    musicbrainz_calls: list[httpx.Request] = []
    deezer_calls: list[httpx.Request] = []
    synthetic_mbid = "11111111-1111-1111-1111-111111111111"
    synthetic_deezer_artist_id = "424242"

    def _dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "musicbrainz.org":
            musicbrainz_calls.append(request)
            if request.url.path == "/ws/2/url":
                # The Spotify-URL batch lookup: resolve artist-1's Spotify URL to a
                # synthetic MBID via exactly one artist relation.
                spotify_url = "https://open.spotify.com/artist/artist-native"
                return httpx.Response(
                    200,
                    json={
                        "url-count": 1,
                        "url-offset": 0,
                        "urls": [
                            {
                                "resource": spotify_url,
                                "relations": [{"artist": {"id": synthetic_mbid}}],
                            }
                        ],
                    },
                )
            if request.url.path == "/ws/2/release-group":
                # Release-group search for musicbrainz's own recent_releases call.
                return httpx.Response(200, json={"release-groups": []})
            # The per-artist url-rels lookup used to resolve a Deezer artist id.
            return httpx.Response(
                200,
                json={
                    "relations": [
                        {
                            "type": "free streaming",
                            "url": {
                                "resource": (
                                    f"https://www.deezer.com/artist/{synthetic_deezer_artist_id}"
                                )
                            },
                        }
                    ]
                },
            )
        if request.url.host == "api.deezer.com":
            deezer_calls.append(request)
            return httpx.Response(200, json={"data": [], "total": 0})
        raise AssertionError(f"unexpected host: {request.url.host}")

    def connector_factory() -> httpx.BaseTransport:
        return httpx.MockTransport(_dispatch)

    class _EventStore:
        def save(self, _key: object, _value: str) -> None:
            raise AssertionError("unused")

        def load(self, _key: object) -> str | None:
            return None

        def delete(self, _key: object) -> None:
            raise AssertionError("unused")

    monkeypatch.setattr(cli, "_spotify_source", _refuse_spotify_source)

    from music_friend.domain import (
        Artist,
        IdentityConfidence,
        SourceReference,
        WatchlistAction,
        WatchlistOverride,
    )

    application = MusicFriendApplication(Catalog.open(tmp_path / "catalog.sqlite3"))
    # Pre-seed a musicbrainz identity (as an earlier musicbrainz-only refresh
    # would have already resolved) so this run's deezer url-rel mapping step is
    # eligible immediately, instead of requiring a second refresh cycle.
    artist = Artist(
        "artist-1",
        "Artist One",
        (
            SourceReference("spotify", "artist-native", None, NOW),
            SourceReference("musicbrainz", synthetic_mbid, None, NOW),
        ),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    application.put_artist(artist)
    application.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, NOW))
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        result = cli.run_cli(
            ["refresh", "releases", "--json"],
            stdout=stdout,
            stderr=stderr,
            application=application,
            config_store=_ConfigStore(LocalConfig(release_sources=("musicbrainz", "deezer"))),
            secret_prompt=lambda _message: "",
            connector_factory=connector_factory,
            credential_store_factory=lambda: _EventStore(),  # type: ignore[arg-type]
        )
    finally:
        application.close()

    assert result == 0, f"stdout={stdout.getvalue()!r} stderr={stderr.getvalue()!r}"
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "succeeded"
    assert stderr.getvalue() == ""
    # Both configured sources were actually reached over the network (proving
    # runtime wiring, not just config acceptance), and _refuse_spotify_source
    # never fired (proving Spotify was never touched).
    assert musicbrainz_calls
    assert deezer_calls
