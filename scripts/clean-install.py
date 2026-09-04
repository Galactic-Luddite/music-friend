#!/usr/bin/env python3
"""Install one built wheel in a fresh offline environment and run local smoke checks."""

from __future__ import annotations

import argparse
import json
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_EXPECTED_MCP_TOOLS = (
    "music_status",
    "refresh_music",
    "search_catalog",
    "list_watchlist",
    "update_watchlist",
    "list_inbox",
    "update_inbox_item",
    "explain_inbox_item",
    "summarize_listening_history",
)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    return parser.parse_args(argv)


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = path.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_link_or_reparse(path: Path) -> bool:
    return path.is_symlink() or _is_reparse_point(path)


def _wheel_from_distribution(distribution: Path) -> Path:
    if not distribution.is_dir() or _is_link_or_reparse(distribution):
        raise SystemExit("distribution must name a regular directory")
    distribution = distribution.resolve()
    artifacts = sorted(distribution.iterdir(), key=lambda path: path.name)
    if (
        len(artifacts) != 1
        or not artifacts[0].is_file()
        or _is_link_or_reparse(artifacts[0])
        or artifacts[0].suffix != ".whl"
    ):
        raise SystemExit("distribution directory must contain exactly one wheel")
    return artifacts[0]


def _run(command: list[str], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=Path(environment["MUSIC_FRIEND_WORKSPACE"]),
    )


def _read_mcp_response(
    responses: queue.Queue[str | None], *, expected_id: int, timeout: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("MCP smoke check timed out")
        try:
            line = responses.get(timeout=remaining)
        except queue.Empty as error:
            raise ValueError("MCP smoke check timed out") from error
        if line is None:
            raise ValueError("MCP smoke check ended before a response")
        message = json.loads(line)
        if isinstance(message, dict) and message.get("id") == expected_id:
            return message


def _assert_mcp_smoke(messages: tuple[dict[str, object], ...]) -> None:
    if len(messages) != 3 or {message.get("id") for message in messages} != {1, 2, 3}:
        raise ValueError("MCP smoke check returned unexpected responses")
    by_id = {message["id"]: message for message in messages}
    tools = by_id[2].get("result", {}).get("tools")
    status = by_id[3].get("result", {}).get("structuredContent", {}).get("status")
    tool_names = (
        [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if isinstance(tools, list)
        else []
    )
    if tool_names != list(_EXPECTED_MCP_TOOLS) or status != "ready":
        raise ValueError("MCP smoke check returned an invalid result")


def _mcp_smoke(command: Path, *, environment: dict[str, str]) -> None:
    workspace = Path(environment["MUSIC_FRIEND_WORKSPACE"])
    process = subprocess.Popen(
        [str(command)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
        cwd=workspace,
    )
    if process.stdin is None or process.stdout is None:
        process.terminate()
        raise ValueError("MCP smoke check could not open stdio")
    responses: queue.Queue[str | None] = queue.Queue()

    def collect_stdout() -> None:
        for line in process.stdout:
            responses.put(line)
        responses.put(None)

    reader = threading.Thread(target=collect_stdout, daemon=True)
    reader.start()

    def send(message: dict[str, object]) -> None:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "clean-install", "version": "1"},
                },
            }
        )
        initialized = _read_mcp_response(responses, expected_id=1, timeout=10)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = _read_mcp_response(responses, expected_id=2, timeout=10)
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "music_status", "arguments": {}},
            }
        )
        status = _read_mcp_response(responses, expected_id=3, timeout=10)
        _assert_mcp_smoke((initialized, tools, status))
    finally:
        process.stdin.close()
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            return_code = process.wait(timeout=10)
        reader.join(timeout=10)
    stderr = process.stderr.read() if process.stderr is not None else ""
    if return_code != 0 or stderr:
        raise ValueError("MCP smoke check failed")


def _mcp_executable(venv: Path, *, platform: str) -> Path:
    if platform == "win32":
        return venv / "Scripts" / "music-friend-mcp.exe"
    return venv / "bin" / "music-friend-mcp"


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    wheel = _wheel_from_distribution(arguments.distribution)
    wheelhouse = arguments.wheelhouse.resolve()
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise SystemExit("wheelhouse must name a regular directory")

    with tempfile.TemporaryDirectory(
        prefix="music-friend-clean-install-", dir=Path(tempfile.gettempdir()).resolve()
    ) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        venv = root / "venv"
        home = root / "home"
        config = root / "config"
        data = root / "data"
        cache = root / "cache"
        app_data = root / "appdata"
        local_app_data = root / "local-appdata"
        for directory in (workspace, home, config, data, cache, app_data, local_app_data):
            directory.mkdir()
        environment = {
            "APPDATA": str(app_data),
            "HOME": str(home),
            "LOCALAPPDATA": str(local_app_data),
            "MUSIC_FRIEND_WORKSPACE": str(workspace),
            "PATH": os.defpath,
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "WIN_PD_OVERRIDE_APPDATA": str(app_data),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(local_app_data),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
        }
        for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT"):
            if name in os.environ:
                environment[name] = os.environ[name]
        _run([sys.executable, "-I", "-m", "venv", str(venv)], environment=environment)
        executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command = venv / ("Scripts/music-friend.exe" if os.name == "nt" else "bin/music-friend")
        mcp_command = _mcp_executable(venv, platform=sys.platform)
        _run(
            [
                str(executable),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                str(wheel),
            ],
            environment=environment,
        )
        _run([str(command), "version"], environment=environment)
        _run([str(command), "--help"], environment=environment)
        status = subprocess.run(
            [str(command), "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=workspace,
        )
        if status.returncode not in {0, 4}:
            raise SystemExit("status smoke check failed")
        payload = json.loads(status.stdout)
        if payload.get("status") not in {"ready", "unavailable"}:
            raise SystemExit("status smoke check produced an invalid payload")
        try:
            _mcp_smoke(mcp_command, environment=environment)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise SystemExit("MCP smoke check returned invalid output") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
