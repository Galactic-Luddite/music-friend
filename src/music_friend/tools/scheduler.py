"""Render and manage minimal per-user one-shot refresh schedules."""

from __future__ import annotations

import os
import shlex
import subprocess
import xml.sax.saxutils
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from music_friend.providers.credentials import CredentialStore, CredentialStoreError
from music_friend.providers.store_selection import open_scheduled_credential_store


class SchedulePlatform(str, Enum):
    """The three supported per-user scheduler formats."""

    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"


@dataclass(frozen=True, slots=True)
class RenderedSchedule:
    """A primary definition and the optional companion service definition."""

    relative_path: Path
    content: str
    companion_relative_path: Path | None = None
    companion_content: str | None = None


CommandRunner = Callable[[tuple[str, ...]], None]
NativeStoreFactory = Callable[[], CredentialStore]


def render_schedule(
    platform: SchedulePlatform,
    *,
    command: tuple[str, ...],
    interval_minutes: int,
) -> RenderedSchedule:
    """Render one bounded per-user schedule that runs the supplied one-shot command."""
    _validate(platform, command, interval_minutes)
    if platform is SchedulePlatform.MACOS:
        return RenderedSchedule(
            Path("Library/LaunchAgents/com.musicfriend.refresh.plist"),
            _macos_plist(command, interval_minutes),
        )
    if platform is SchedulePlatform.WINDOWS:
        return RenderedSchedule(
            Path("MusicFriendRefresh.xml"), _windows_xml(command, interval_minutes)
        )
    return RenderedSchedule(
        Path(".config/systemd/user/music-friend-refresh.timer"),
        _linux_timer(interval_minutes),
        Path(".config/systemd/user/music-friend-refresh.service"),
        _linux_service(command),
    )


def install_schedule(
    platform: SchedulePlatform,
    *,
    user_root: Path,
    command: tuple[str, ...],
    interval_minutes: int,
    runner: CommandRunner | None = None,
    native_store_factory: NativeStoreFactory = open_scheduled_credential_store,
) -> Path:
    """Install definitions beneath an explicit user root after native-store eligibility succeeds."""
    if not isinstance(user_root, Path):
        raise ValueError("user_root must be a Path")
    store = native_store_factory()
    if getattr(store, "scheduled_eligible", False) is not True:
        raise CredentialStoreError()
    rendered = render_schedule(platform, command=command, interval_minutes=interval_minutes)
    primary = _write_definition(user_root, rendered.relative_path, rendered.content)
    if rendered.companion_relative_path is not None and rendered.companion_content is not None:
        _write_definition(user_root, rendered.companion_relative_path, rendered.companion_content)
    execute = _default_runner if runner is None else runner
    _install_command(platform, primary, execute)
    return primary


def remove_schedule(
    platform: SchedulePlatform,
    *,
    user_root: Path,
    runner: CommandRunner | None = None,
) -> None:
    """Remove only the known per-user definitions and deactivate the corresponding schedule."""
    if not isinstance(platform, SchedulePlatform) or not isinstance(user_root, Path):
        raise ValueError("platform and user_root are required")
    rendered = render_schedule(
        platform, command=("music-friend", "refresh", "all"), interval_minutes=60
    )
    execute = _default_runner if runner is None else runner
    _remove_command(platform, user_root / rendered.relative_path, execute)
    for relative_path in (rendered.relative_path, rendered.companion_relative_path):
        if relative_path is not None:
            try:
                (user_root / relative_path).unlink(missing_ok=True)
            except OSError:
                pass


def _validate(platform: SchedulePlatform, command: tuple[str, ...], interval_minutes: int) -> None:
    if not isinstance(platform, SchedulePlatform):
        raise ValueError("platform must be a SchedulePlatform")
    if (
        not isinstance(command, tuple)
        or not command
        or not all(
            type(argument) is str and argument and "\x00" not in argument for argument in command
        )
    ):
        raise ValueError("command must be a non-empty tuple of text")
    if type(interval_minutes) is not int or not 60 <= interval_minutes <= 10_080:
        raise ValueError("interval_minutes must be from 60 through 10080")


def _macos_plist(command: tuple[str, ...], interval_minutes: int) -> str:
    arguments = "".join(
        f"<string>{xml.sax.saxutils.escape(argument)}</string>" for argument in command
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict><key>Label</key><string>com.musicfriend.refresh</string>'
        f"<key>ProgramArguments</key><array>{arguments}</array>"
        f"<key>StartInterval</key><integer>{interval_minutes * 60}</integer>"
        "</dict></plist>\n"
    )


def _windows_xml(command: tuple[str, ...], interval_minutes: int) -> str:
    executable = xml.sax.saxutils.escape(command[0])
    arguments = xml.sax.saxutils.escape(" ".join(_windows_argument(value) for value in command[1:]))
    duration = f"PT{interval_minutes}M"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Triggers><TimeTrigger><Repetition><Interval>"
        f"{duration}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
        "<StartBoundary>2026-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>"
        '</TimeTrigger></Triggers><Principals><Principal id="Author">'
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><ExecutionTimeLimit>PT10M</ExecutionTimeLimit>"
        '</Settings><Actions Context="Author"><Exec>'
        f"<Command>{executable}</Command><Arguments>{arguments}</Arguments>"
        "</Exec></Actions></Task>\n"
    )


def _linux_timer(interval_minutes: int) -> str:
    return (
        "[Unit]\nDescription=Music Friend refresh timer\n\n[Timer]\n"
        f"OnUnitInactiveSec={interval_minutes}min\nPersistent=true\nUnit=music-friend-refresh.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )


def _linux_service(command: tuple[str, ...]) -> str:
    rendered = " ".join(shlex.quote(argument) for argument in command)
    return (
        "[Unit]\nDescription=Music Friend refresh\n\n[Service]\nType=oneshot\n"
        f"ExecStart={rendered}\n"
    )


def _windows_argument(value: str) -> str:
    return subprocess.list2cmdline([value])


def _write_definition(user_root: Path, relative_path: Path, content: str) -> Path:
    destination = user_root / relative_path
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _install_command(platform: SchedulePlatform, definition: Path, runner: CommandRunner) -> None:
    if platform is SchedulePlatform.MACOS:
        runner(("launchctl", "bootstrap", f"gui/{os.getuid()}", str(definition)))
    elif platform is SchedulePlatform.WINDOWS:
        runner(("schtasks", "/Create", "/TN", "MusicFriendRefresh", "/XML", str(definition), "/F"))
    else:
        runner(("systemctl", "--user", "daemon-reload"))
        runner(("systemctl", "--user", "enable", "--now", "music-friend-refresh.timer"))


def _remove_command(platform: SchedulePlatform, definition: Path, runner: CommandRunner) -> None:
    if platform is SchedulePlatform.MACOS:
        runner(("launchctl", "bootout", f"gui/{os.getuid()}", str(definition)))
    elif platform is SchedulePlatform.WINDOWS:
        runner(("schtasks", "/Delete", "/TN", "MusicFriendRefresh", "/F"))
    else:
        runner(("systemctl", "--user", "disable", "--now", "music-friend-refresh.timer"))
        runner(("systemctl", "--user", "daemon-reload"))


def _default_runner(arguments: tuple[str, ...]) -> None:
    subprocess.run(arguments, check=True)


__all__ = [
    "RenderedSchedule",
    "SchedulePlatform",
    "install_schedule",
    "remove_schedule",
    "render_schedule",
]
