from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from music_friend.providers.credentials import CredentialStoreError
from music_friend.tools.scheduler import (
    SchedulePlatform,
    ScheduleStatus,
    _windows_argument,
    install_schedule,
    remove_schedule,
    render_schedule,
    schedule_status,
)


def test_scheduler_definitions_are_per_user_and_invoke_one_shot_refresh() -> None:
    """Catches a platform definition that creates a resident or system-wide service."""
    command = ("/" + "opt/music-friend/bin/music-friend", "refresh", "all")

    macos = render_schedule(SchedulePlatform.MACOS, command=command, interval_minutes=360)
    windows = render_schedule(SchedulePlatform.WINDOWS, command=command, interval_minutes=360)
    linux = render_schedule(SchedulePlatform.LINUX, command=command, interval_minutes=360)

    assert macos.relative_path == Path("Library/LaunchAgents/com.musicfriend.refresh.plist")
    assert "music-friend" in macos.content
    assert "refresh" in macos.content
    assert "KeepAlive" not in macos.content
    assert windows.relative_path == Path("MusicFriendRefresh.xml")
    assert "schemas.microsoft.com/windows" in windows.content
    assert "refresh" in windows.content
    assert "SYSTEM" not in windows.content
    assert linux.relative_path == Path(".config/systemd/user/music-friend-refresh.timer")
    assert "music-friend-refresh.service" in linux.content
    assert "--user" not in linux.content


def test_scheduler_install_and_remove_use_a_temporary_user_root_and_fake_runner(
    tmp_path: Path,
) -> None:
    """Catches scheduler installation that writes outside the chosen user root or touches the host."""
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...]) -> None:
        calls.append(arguments)

    installed = install_schedule(
        SchedulePlatform.LINUX,
        user_root=tmp_path,
        command=("music-friend", "refresh", "all"),
        interval_minutes=120,
        runner=runner,
        native_store_factory=lambda: _EligibleStore(),
    )

    assert installed.exists()
    assert installed.parent == tmp_path / ".config/systemd/user"
    assert calls == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "music-friend-refresh.timer"),
    ]

    remove_schedule(SchedulePlatform.LINUX, user_root=tmp_path, runner=runner)

    assert not installed.exists()
    assert calls[-2:] == [
        ("systemctl", "--user", "disable", "--now", "music-friend-refresh.timer"),
        ("systemctl", "--user", "daemon-reload"),
    ]


def test_scheduler_status_requires_all_definitions_and_an_active_native_job(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    missing = schedule_status(
        SchedulePlatform.LINUX,
        user_root=tmp_path,
        interval_minutes=1440,
        runner=lambda arguments: calls.append(arguments) or True,
    )

    assert missing == ScheduleStatus(
        installed=False,
        active=False,
        platform=SchedulePlatform.LINUX,
        interval_minutes=1440,
    )
    assert calls == []

    rendered = render_schedule(
        SchedulePlatform.LINUX,
        command=("python", "-m", "music_friend.runtimes.cli", "refresh", "all", "--json"),
        interval_minutes=1440,
    )
    primary = tmp_path / rendered.relative_path
    primary.parent.mkdir(parents=True)
    primary.write_text(rendered.content, encoding="utf-8")

    incomplete = schedule_status(
        SchedulePlatform.LINUX,
        user_root=tmp_path,
        interval_minutes=1440,
        runner=lambda arguments: calls.append(arguments) or True,
    )
    assert incomplete.installed is False
    assert calls == []

    assert rendered.companion_relative_path is not None
    companion = tmp_path / rendered.companion_relative_path
    companion.write_text(rendered.companion_content or "", encoding="utf-8")

    active = schedule_status(
        SchedulePlatform.LINUX,
        user_root=tmp_path,
        interval_minutes=1440,
        runner=lambda arguments: calls.append(arguments) or True,
    )
    assert active.installed is True
    assert active.active is True
    assert calls == [("systemctl", "--user", "is-active", "music-friend-refresh.timer")]


def test_reinstalling_macos_schedule_reloads_the_existing_job(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    arguments = {
        "user_root": tmp_path,
        "command": ("python", "-m", "music_friend.runtimes.cli", "refresh", "all", "--json"),
        "interval_minutes": 1440,
        "runner": calls.append,
        "native_store_factory": lambda: _EligibleStore(),
    }

    install_schedule(SchedulePlatform.MACOS, **arguments)  # type: ignore[arg-type]
    install_schedule(SchedulePlatform.MACOS, **arguments)  # type: ignore[arg-type]

    assert [call[1] for call in calls] == ["bootstrap", "bootout", "bootstrap"]


@pytest.mark.parametrize("platform", (SchedulePlatform.MACOS, SchedulePlatform.WINDOWS))
def test_scheduler_install_uses_only_its_explicit_temporary_user_root(
    tmp_path: Path, platform: SchedulePlatform
) -> None:
    """Catches macOS or Windows installation escaping the supplied per-user root."""
    calls: list[tuple[str, ...]] = []

    installed = install_schedule(
        platform,
        user_root=tmp_path,
        command=("music-friend", "refresh", "all"),
        interval_minutes=60,
        runner=calls.append,
        native_store_factory=lambda: _EligibleStore(),
    )

    assert installed.is_file()
    assert installed.is_relative_to(tmp_path)
    assert len(calls) == 1


def test_scheduler_requires_an_approved_native_store_before_installing(tmp_path: Path) -> None:
    """Catches an unattended schedule being installed while credentials need an interactive passphrase."""

    def unavailable_store() -> object:
        raise CredentialStoreError()

    with pytest.raises(CredentialStoreError):
        install_schedule(
            SchedulePlatform.MACOS,
            user_root=tmp_path,
            command=("music-friend", "refresh", "all"),
            interval_minutes=60,
            native_store_factory=unavailable_store,
        )


def test_scheduler_rejects_an_injected_non_native_schedule_ineligible_store(tmp_path: Path) -> None:
    """Catches an injected encrypted-vault-like store bypassing unattended schedule eligibility."""

    class VaultLikeStore:
        scheduled_eligible = False

    with pytest.raises(CredentialStoreError):
        install_schedule(
            SchedulePlatform.LINUX,
            user_root=tmp_path,
            command=("music-friend", "refresh", "all"),
            interval_minutes=60,
            runner=lambda _arguments: None,
            native_store_factory=VaultLikeStore,
        )


def test_windows_schedule_declaration_matches_written_utf8_bytes(tmp_path: Path) -> None:
    """Catches XML declaring UTF-16 while installation writes it as UTF-8 text."""
    installed = install_schedule(
        SchedulePlatform.WINDOWS,
        user_root=tmp_path,
        command=("music-friend", "refresh", "all"),
        interval_minutes=60,
        runner=lambda _arguments: None,
        native_store_factory=lambda: _EligibleStore(),
    )

    assert 'encoding="UTF-8"' in installed.read_text(encoding="utf-8")
    assert ElementTree.parse(installed).getroot().tag.endswith("Task")


def test_windows_argument_escapes_a_quoted_path_ending_in_a_backslash() -> None:
    """Catches a trailing backslash consuming the closing quote of a scheduled command argument."""
    argument = "C:" + chr(92) + "Music Friend" + chr(92)

    assert _windows_argument(argument) == subprocess.list2cmdline([argument])


class _EligibleStore:
    scheduled_eligible = True


@pytest.mark.parametrize(
    ("platform", "command", "interval", "message"),
    (
        ("linux", ("music-friend",), 60, "SchedulePlatform"),
        (SchedulePlatform.LINUX, (), 60, "non-empty tuple"),
        (SchedulePlatform.LINUX, ("music-friend",), 59, "60 through 10080"),
    ),
)
def test_render_schedule_rejects_invalid_public_inputs(
    platform: object,
    command: tuple[str, ...],
    interval: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        render_schedule(platform, command=command, interval_minutes=interval)  # type: ignore[arg-type]


def test_schedule_mutations_require_path_user_roots() -> None:
    with pytest.raises(ValueError, match="user_root must be a Path"):
        install_schedule(  # type: ignore[arg-type]
            SchedulePlatform.LINUX,
            user_root="not-a-path",
            command=("music-friend",),
            interval_minutes=60,
            native_store_factory=lambda: _EligibleStore(),
        )

    with pytest.raises(ValueError, match="platform and user_root are required"):
        remove_schedule(SchedulePlatform.LINUX, user_root="not-a-path")  # type: ignore[arg-type]
