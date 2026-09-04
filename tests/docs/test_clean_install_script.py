"""Checks for the offline built-wheel clean-install command."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "clean-install.py"


def _load_clean_install_module() -> object:
    specification = importlib.util.spec_from_file_location("clean_install", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_clean_install_script_requires_a_wheel_and_wheelhouse() -> None:
    module = _load_clean_install_module()

    try:
        module._arguments([])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("missing required arguments must exit")


@pytest.mark.parametrize(
    ("entries", "expected_message"),
    [
        ([], "distribution directory must contain exactly one wheel"),
        (
            ["music_friend-0.1.0-py3-none-any.whl", "music_friend-0.1.1-py3-none-any.whl"],
            "distribution directory must contain exactly one wheel",
        ),
        (["music_friend-0.1.0.tar.gz"], "distribution directory must contain exactly one wheel"),
        (
            ["music_friend-0.1.0-py3-none-any.whl", "release-notes.txt"],
            "distribution directory must contain exactly one wheel",
        ),
    ],
)
def test_clean_install_distribution_directory_requires_exactly_one_wheel(
    tmp_path: Path, entries: list[str], expected_message: str
) -> None:
    """Artifact selection is deterministic and rejects ambiguous distributions."""
    module = _load_clean_install_module()
    distribution = tmp_path / "dist"
    distribution.mkdir()
    for name in entries:
        (distribution / name).write_bytes(b"artifact")

    with pytest.raises(SystemExit, match=expected_message):
        module._wheel_from_distribution(distribution)

    wheel = distribution / "music_friend-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"artifact")
    if not entries:
        assert module._wheel_from_distribution(distribution) == wheel


@pytest.mark.parametrize("kind", ("file", "symlink"))
def test_clean_install_rejects_a_non_regular_distribution_directory(
    tmp_path: Path, kind: str
) -> None:
    module = _load_clean_install_module()
    target = tmp_path / "distribution"
    if kind == "file":
        target.write_bytes(b"not a directory")
    else:
        destination = tmp_path / "actual-distribution"
        destination.mkdir()
        target.symlink_to(destination, target_is_directory=True)

    with pytest.raises(SystemExit, match="distribution must name a regular directory"):
        module._wheel_from_distribution(target)


def test_clean_install_rejects_a_symlinked_wheel(tmp_path: Path) -> None:
    module = _load_clean_install_module()
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    original = tmp_path / "music_friend-0.1.0-py3-none-any.whl"
    original.write_bytes(b"wheel")
    (distribution / original.name).symlink_to(original)

    with pytest.raises(SystemExit, match="distribution directory must contain exactly one wheel"):
        module._wheel_from_distribution(distribution)


def test_clean_install_rejects_a_windows_reparse_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_clean_install_module()
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    (distribution / "music_friend-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path == distribution)

    with pytest.raises(SystemExit, match="distribution must name a regular directory"):
        module._wheel_from_distribution(distribution)


def test_mcp_executable_path_is_selected_from_an_explicit_platform_value(tmp_path: Path) -> None:
    """Windows path selection is covered locally without executing a Windows process."""
    module = _load_clean_install_module()
    venv = tmp_path / "venv"

    assert module._mcp_executable(venv, platform="linux") == venv / "bin" / "music-friend-mcp"
    assert module._mcp_executable(venv, platform="win32") == (
        venv / "Scripts" / "music-friend-mcp.exe"
    )


def test_clean_install_run_executes_only_in_its_temporary_workspace(
    monkeypatch: object, tmp_path: Path
) -> None:
    """The subprocess helper keeps process and profile writes outside the checkout."""
    module = _load_clean_install_module()
    workspace = tmp_path / "workspace"
    profile = tmp_path / "profile"
    workspace.mkdir()
    profile.mkdir()
    environment = {
        "HOME": str(profile),
        "MUSIC_FRIEND_WORKSPACE": str(workspace),
        "PATH": os.defpath,
    }
    child_marker = "clean-install-child-cwd.txt"
    profile_marker = "clean-install-profile-write.txt"
    code = (
        "import os\n"
        "from pathlib import Path\n"
        f'Path("{child_marker}").write_text(str(Path.cwd()), encoding="utf-8")\n'
        f'Path(os.environ["HOME"]).joinpath("{profile_marker}").write_text("ok", encoding="utf-8")\n'
    )
    command = [str(Path(sys.executable).resolve()), "-c", code]
    boundaries = sys.modules.get("clean_room.boundaries")
    if boundaries is not None:
        policy = boundaries._policy()
        monkeypatch.setattr(
            policy,
            "allowed_children",
            policy.allowed_children + (tuple(command),),
        )

    module._run(command, environment=environment)

    assert (workspace / child_marker).read_text(encoding="utf-8") == str(workspace)
    assert (profile / profile_marker).read_text(encoding="utf-8") == "ok"
    assert not (ROOT / child_marker).exists()
    assert not (ROOT / profile_marker).exists()


def test_clean_install_subprocesses_run_in_the_temporary_workspace(
    monkeypatch: object, tmp_path: Path
) -> None:
    """A built-wheel smoke check must never run with the checkout as its working directory."""
    module = _load_clean_install_module()
    distribution = tmp_path / "dist"
    distribution.mkdir()
    (distribution / "music_friend-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (tmp_path / "isolated").mkdir()
    calls: list[tuple[Path | None, dict[str, str]]] = []
    temporary_calls: list[dict[str, object]] = []
    mcp_processes: list[object] = []

    @contextmanager
    def temporary_directory(**kwargs: object):
        temporary_calls.append(kwargs)
        yield str(tmp_path / "isolated")

    def run_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        cwd = kwargs.get("cwd")
        assert isinstance(environment, dict)
        assert cwd is None or isinstance(cwd, Path)
        calls.append((cwd, environment))
        if command[-2:] == ["status", "--json"]:
            return subprocess.CompletedProcess([], 0, json.dumps({"status": "ready"}), "")
        return subprocess.CompletedProcess([], 0, "", "")

    class ResponsiveStdout:
        def __init__(self) -> None:
            self.messages: queue.Queue[str | None] = queue.Queue()

        def __iter__(self) -> object:
            return self

        def __next__(self) -> str:
            item = self.messages.get(timeout=1)
            if item is None:
                raise StopIteration
            return item

    class ResponsiveStdin:
        def __init__(self, stdout: ResponsiveStdout) -> None:
            self.stdout = stdout
            self.writes: list[dict[str, object]] = []
            self.closed = False
            self.initialized = False
            self.tool_listed = False

        def write(self, value: str) -> int:
            request = json.loads(value)
            self.writes.append(request)
            if request.get("id") == 1:
                self.stdout.messages.put(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
                )
            elif request.get("method") == "notifications/initialized":
                self.initialized = True
            elif request.get("id") == 2 and self.initialized:
                self.tool_listed = True
                tool_names = [
                    "music_status",
                    "refresh_music",
                    "search_catalog",
                    "list_watchlist",
                    "update_watchlist",
                    "list_inbox",
                    "update_inbox_item",
                    "explain_inbox_item",
                    "summarize_listening_history",
                ]
                self.stdout.messages.put(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {"tools": [{"name": name} for name in tool_names]},
                        }
                    )
                    + "\n"
                )
            elif request.get("id") == 3 and self.tool_listed:
                self.stdout.messages.put(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {"structuredContent": {"status": "ready"}},
                        }
                    )
                    + "\n"
                )
            return len(value)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True
            self.stdout.messages.put(None)

    class ResponsiveProcess:
        def __init__(self) -> None:
            self.stdout = ResponsiveStdout()
            self.stdin = ResponsiveStdin(self.stdout)
            self.stderr = None
            self.returncode: int | None = None

        def wait(self, timeout: float | None = None) -> int:
            assert self.stdin.closed
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15
            self.stdin.close()

    def open_mcp(command: list[str], **kwargs: object) -> ResponsiveProcess:
        assert command[-1].endswith(("music-friend-mcp", "music-friend-mcp.exe"))
        assert kwargs["cwd"] == tmp_path / "isolated" / "workspace"
        process = ResponsiveProcess()
        mcp_processes.append(process)
        return process

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(module.subprocess, "run", run_process)
    monkeypatch.setattr(module.subprocess, "Popen", open_mcp)

    assert module.main(["--distribution", str(distribution), "--wheelhouse", str(wheelhouse)]) == 0
    assert calls
    assert temporary_calls == [
        {
            "prefix": "music-friend-clean-install-",
            "dir": Path(module.tempfile.gettempdir()).resolve(),
        }
    ]
    workspace = tmp_path / "isolated" / "workspace"
    assert all(path == workspace for path, _environment in calls)
    assert all(ROOT != path for path, _environment in calls)
    assert not (workspace / "keyring").exists()
    for _path, environment in calls:
        assert environment["HOME"].startswith(str(tmp_path / "isolated"))
        assert environment["APPDATA"].startswith(str(tmp_path / "isolated"))
        assert environment["LOCALAPPDATA"].startswith(str(tmp_path / "isolated"))
        assert environment["WIN_PD_OVERRIDE_APPDATA"] == environment["APPDATA"]
        assert environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] == environment["LOCALAPPDATA"]
        assert "PYTHONPATH" not in environment
        for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT"):
            if name in os.environ:
                assert environment[name] == os.environ[name]
    assert len(mcp_processes) == 1
    writes = mcp_processes[0].stdin.writes
    assert [request.get("id") for request in writes] == [1, None, 2, 3]
    assert writes[1]["method"] == "notifications/initialized"


def test_clean_install_script_uses_an_offline_fresh_venv_and_local_smoke_flow() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for required in (
        '"-m", "venv"',
        '"--no-index"',
        '"--find-links"',
        '"music_status"',
        '"summarize_listening_history"',
        "music-friend-mcp",
        '"initialize"',
        '"tools/list"',
        '"XDG_CONFIG_HOME"',
        'cwd=Path(environment["MUSIC_FRIEND_WORKSPACE"])',
        '"WIN_PD_OVERRIDE_APPDATA"',
        '"WIN_PD_OVERRIDE_LOCAL_APPDATA"',
    ):
        assert required in source
    assert '"PYTHONPATH"' not in source
