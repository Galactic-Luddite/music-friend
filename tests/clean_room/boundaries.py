"""Pytest plugin enforcing Phase 1 network, process, and write boundaries."""

from __future__ import annotations

import builtins
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

import pytest

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by the Windows CI collector
    fcntl = None  # type: ignore[assignment]


class BoundaryViolation(RuntimeError):
    """Raised before a forbidden clean-room boundary operation occurs."""


class BoundaryPolicy:
    def __init__(
        self,
        *,
        allowed_write_roots: Sequence[Path] | Mapping[str, Path],
        allowed_children: Sequence[Sequence[str]],
        allowed_child_ids: Sequence[str] = (),
        source_root: Path | None = None,
    ) -> None:
        roots = (
            allowed_write_roots.items()
            if isinstance(allowed_write_roots, Mapping)
            else ((f"write-root-{index}", path) for index, path in enumerate(allowed_write_roots))
        )
        self.write_roots = {name: path.resolve() for name, path in roots}
        self.allowed_write_roots = tuple(self.write_roots.values())
        self.allowed_children = tuple(
            tuple(_normalize_argument(part) for part in command) for command in allowed_children
        )
        self.allowed_child_ids = frozenset(allowed_child_ids)
        self.source_root = (
            source_root.resolve() if source_root else Path(__file__).parents[2].resolve()
        )
        self.binds = 0
        self.connects = 0
        self.sends = 0
        self.browser_handoffs: list[tuple[str, str]] = []
        self.http_attempts: list[tuple[str, bool]] = []
        self.callback_binds: list[tuple[str, int]] = []
        self.callback_closes: list[tuple[str, int]] = []
        self.write_checks: dict[str, int] = {}
        self.child_commands: list[dict[str, object]] = []

    def reject_network(self, operation: str) -> None:
        if operation in {"bind", "listen"}:
            self.binds += 1
        elif operation.startswith("send"):
            self.sends += 1
        else:
            self.connects += 1
        raise BoundaryViolation(f"network operation blocked: {operation}")

    def check_child(self, argv: Sequence[str], *, shell: bool, cwd: object | None = None) -> None:
        if shell:
            raise BoundaryViolation("shell execution blocked")
        normalized = tuple(_normalize_argument(part) for part in argv)
        working_directory = (
            Path(os.fspath(cwd)).resolve() if cwd is not None else Path.cwd().resolve()
        )
        if normalized in self.allowed_children:
            self._record_child(_child_evidence("exact", normalized, working_directory, self))
            return
        for identifier in sorted(self.allowed_child_ids):
            if _validate_named_child(identifier, normalized, working_directory, self):
                self._record_child(_child_evidence(identifier, normalized, working_directory, self))
                return
        raise BoundaryViolation("undeclared child process blocked")

    def _record_child(self, observed: dict[str, object]) -> None:
        for prior in self.child_commands:
            if all(prior[key] == observed[key] for key in ("id", "argv", "cwd")):
                prior["count"] = int(prior["count"]) + 1
                return
        self.child_commands.append({**observed, "count": 1})

    def record_browser_handoff(self, url: str) -> None:
        target = urlsplit(url)
        host = target.hostname or ""
        self.browser_handoffs.append((host, target.path))
        try:
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            pairs = []
        query = dict(pairs)
        required_query = {
            "client_id",
            "response_type",
            "redirect_uri",
            "state",
            "scope",
            "code_challenge_method",
            "code_challenge",
        }
        if (
            target.scheme != "https"
            or target.netloc != "accounts.spotify.com"
            or target.path != "/authorize"
            or target.fragment
            or len(pairs) != len(required_query)
            or set(query) != required_query
            or query.get("response_type") != "code"
            or query.get("code_challenge_method") != "S256"
            or not all(
                query.get(name) for name in ("client_id", "redirect_uri", "state", "code_challenge")
            )
        ):
            raise BoundaryViolation("browser handoff blocked")

    def record_http_attempt(self, host: str, *, scripted: bool) -> None:
        normalized = host.lower()
        self.http_attempts.append((normalized, scripted))
        if not scripted:
            raise BoundaryViolation("scripted mock transport required")
        if normalized not in {"accounts.spotify.com", "api.spotify.com"}:
            raise BoundaryViolation("HTTP host blocked")

    def record_callback_bind(self, host: str, port: int) -> None:
        self.callback_binds.append((host, port))
        if host != "127.0.0.1" or port != 0:
            raise BoundaryViolation("callback bind blocked")

    def record_callback_close(self, host: str, port: int) -> None:
        self.callback_closes.append((host, port))
        if host != "127.0.0.1" or not 1 <= port <= 65535:
            raise BoundaryViolation("callback close blocked")

    def check_write(self, path: Path, *, surface: str = "file") -> None:
        self.write_checks[surface] = self.write_checks.get(surface, 0) + 1
        if not path.is_absolute():
            raise BoundaryViolation("filesystem write blocked: relative path")
        resolved = path.resolve(strict=False)
        if not any(
            resolved == root or resolved.is_relative_to(root) for root in self.allowed_write_roots
        ):
            raise BoundaryViolation("filesystem write blocked: outside controlled roots")


_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_URLOPEN = urllib.request.urlopen
_ORIGINAL_URLRETRIEVE = urllib.request.urlretrieve
_ORIGINAL_POPEN = subprocess.Popen
_ORIGINAL_OPEN = builtins.open
_ORIGINAL_IO_OPEN = io.open
_ORIGINAL_OS_OPEN = os.open
_ORIGINAL_MKDIR = os.mkdir
_ORIGINAL_MAKEDIRS = os.makedirs
_ORIGINAL_RMDIR = os.rmdir
_ORIGINAL_REMOVEDIRS = os.removedirs
_ORIGINAL_RENAME = os.rename
_ORIGINAL_REPLACE = os.replace
_ORIGINAL_UNLINK = os.unlink
_ORIGINAL_REMOVE = os.remove
_ORIGINAL_LINK = os.link
_ORIGINAL_SYMLINK = os.symlink
_ORIGINAL_SQLITE_CONNECT = sqlite3.connect
_POLICY: BoundaryPolicy | None = None
_INSTALLED = False


def _normalize_argument(value: object) -> str:
    argument = os.fspath(value)
    if os.path.isabs(argument):
        return str(Path(argument).resolve(strict=False))
    return str(argument)


def descriptor_path(descriptor: int) -> Path:
    for link in (f"/proc/self/fd/{descriptor}", f"/dev/fd/{descriptor}"):
        try:
            return Path(os.readlink(link)).resolve(strict=False)
        except OSError:
            pass
    if fcntl is None:
        raise BoundaryViolation("filesystem descriptor path unavailable")
    try:
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
    except OSError as error:
        raise BoundaryViolation("filesystem descriptor path unavailable") from error
    encoded = bytes(raw).split(b"\0", 1)[0]
    if not encoded:
        raise BoundaryViolation("filesystem descriptor path unavailable")
    return Path(os.fsdecode(encoded)).resolve(strict=False)


def _load_json_list(name: str) -> list[Any]:
    value = os.environ.get(name)
    if value is None:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"{name} must contain a JSON list")
    return decoded


def _within(path: str, roots: Sequence[Path]) -> bool:
    candidate = Path(path).resolve(strict=False)
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def _artifact_smoke_code(cwd: Path) -> str | None:
    wheels = sorted((cwd / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        return None
    database = cwd / "installed" / "catalog.sqlite3"
    return (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(wheels[0])!r}); "
        "from music_friend.store import Catalog; "
        f"catalog = Catalog.open(Path({str(database)!r})); "
        "assert catalog._connection.execute("
        "'SELECT version FROM schema_migrations ORDER BY version').fetchall() "
        "== [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]; "
        "catalog.close()"
    )


def _checkout_commit(source_root: Path) -> str | None:
    git_metadata = source_root / ".git"
    if git_metadata.is_file():
        marker = git_metadata.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            return None
        git_metadata = (source_root / marker.removeprefix("gitdir: ")).resolve()
    head = git_metadata / "HEAD"
    if not head.is_file():
        return None
    value = head.read_text(encoding="utf-8").strip()
    if value.startswith("ref: "):
        reference = git_metadata / value.removeprefix("ref: ")
        if not reference.is_file():
            return None
        value = reference.read_text(encoding="utf-8").strip()
    return (
        value
        if len(value) == 40 and all(character in "0123456789abcdef" for character in value)
        else None
    )


def _validate_named_child(
    identifier: str, argv: tuple[str, ...], cwd: Path, policy: BoundaryPolicy
) -> bool:
    python = str(Path(sys.executable).resolve())
    bash = str(Path("/bin/bash").resolve())
    hatchling = str(Path(sys.executable).with_name("hatchling").resolve())
    test_root = policy.write_roots.get("pytest", policy.write_roots.get("test-temporary"))
    build_root = policy.write_roots.get("build", policy.write_roots.get("test-temporary"))
    if identifier == "git-ls-source":
        return (
            argv == ("git", "ls-files", "-z", "--", "src/music_friend")
            and cwd == policy.source_root
        )
    if identifier == "git-ls-all":
        return argv == ("git", "ls-files", "-z") and cwd == policy.source_root
    if identifier == "git-archive-head":
        return (
            len(argv) == 5
            and argv[:3] == ("git", "archive", "--format=tar")
            and argv[3].startswith("--output=")
            and Path(argv[3].split("=", 1)[1]).is_absolute()
            and test_root is not None
            and _within(argv[3].split("=", 1)[1], (test_root,))
            and argv[4] == "HEAD"
            and cwd == policy.source_root
        )
    if identifier == "git-test-init":
        return argv == ("git", "init") and test_root is not None and _within(str(cwd), (test_root,))
    if identifier == "git-test-add":
        return (
            argv == ("git", "add", "clean-room-phase1.sh")
            and test_root is not None
            and _within(str(cwd), (test_root,))
        )
    if identifier == "git-test-commit":
        return (
            argv == ("git", "commit", "-m", "test")
            and test_root is not None
            and _within(str(cwd), (test_root,))
        )
    if identifier == "git-test-head":
        return (
            argv == ("git", "rev-parse", "HEAD")
            and test_root is not None
            and _within(str(cwd), (test_root,))
        )
    if identifier == "git-source-head":
        return argv == ("git", "rev-parse", "HEAD") and cwd == policy.source_root
    if identifier == "python-build-export":
        return (
            len(argv) == 7
            and argv[:5] == (python, "-m", "build", "--no-isolation", "--outdir")
            and test_root is not None
            and _within(argv[5], (test_root,))
            and _within(argv[6], (test_root,))
            and Path(argv[5]).resolve().parent == Path(argv[6]).resolve().parent
            and Path(argv[5]).name == "dist"
            and Path(argv[6]).name == "source"
            and cwd == policy.source_root
        )
    if identifier == "python-build-source":
        return (
            len(argv) == 7
            and argv[:5] == (python, "-m", "build", "--no-isolation", "--outdir")
            and test_root is not None
            and _within(argv[5], (test_root,))
            and Path(argv[5]).name == "independent-dist"
            and Path(argv[6]).resolve() == policy.source_root
            and cwd == policy.source_root
        )
    if identifier == "hatchling-build":
        return (
            len(argv) == 4
            and argv[:3] == (hatchling, "build", "-d")
            and build_root is not None
            and _within(argv[3], (build_root,))
            and cwd == policy.source_root
        )
    if identifier == "python-wheel-venv":
        return (
            len(argv) == 7
            and argv[:6]
            == (
                python,
                "-I",
                "-m",
                "venv",
                "--copies",
                "--system-site-packages",
            )
            and test_root is not None
            and _within(argv[6], (test_root,))
            and _within(str(cwd), (test_root,))
        )
    if identifier == "python-wheel-install":
        wheelhouse = os.environ.get("MF_PHASE1_TEST_WHEELHOUSE")
        dependencies = (
            ("--find-links", str(Path(wheelhouse).resolve())) if wheelhouse else ("--no-deps",)
        )
        return (
            len(argv) == 8 + len(dependencies)
            and Path(argv[0]).name in {"python", "python.exe"}
            and test_root is not None
            and _within(argv[0], (test_root,))
            and argv[1:-1]
            == ("-I", "-m", "pip", "install", "--no-index", *dependencies, "--force-reinstall")
            and build_root is not None
            and _within(argv[-1], (build_root,))
            and Path(argv[-1]).suffix == ".whl"
            and _within(str(cwd), (test_root,))
        )
    if identifier == "installed-wheel-skill":
        return (
            len(argv) == 5
            and Path(argv[0]).name in {"music-friend", "music-friend.exe"}
            and test_root is not None
            and _within(argv[0], (test_root,))
            and argv[1:4] == ("skill", "install", "--target")
            and _within(argv[4], (test_root,))
            and _within(str(cwd), (test_root,))
        )
    if identifier == "scanner-source":
        return (
            len(argv) == 3
            and argv[0] == python
            and argv[1] == str(policy.source_root / "scripts" / "scan_public_tree.py")
            and Path(argv[2]).resolve() == policy.source_root
            and cwd == policy.source_root
        )
    if identifier == "scanner-test":
        return (
            len(argv) == 3
            and argv[0] == python
            and argv[1] == str(policy.source_root / "scripts" / "scan_public_tree.py")
            and test_root is not None
            and _within(argv[2], (test_root,))
            and cwd == policy.source_root
        )
    if identifier == "python-artifact-smoke":
        return (
            len(argv) == 4
            and argv[:3] == (python, "-I", "-c")
            and test_root is not None
            and _within(str(cwd), (test_root,))
            and argv[3] == _artifact_smoke_code(cwd)
        )
    if identifier == "catalog-mcp-stdio":
        return (
            len(argv) == 3
            and argv[0] == python
            and Path(argv[1]).resolve()
            == policy.source_root / "tests" / "runtimes" / "catalog_stdio_fixture.py"
            and test_root is not None
            and _within(argv[2], (test_root,))
            and Path(argv[2]).name == "catalog.sqlite3"
            and cwd == policy.source_root
        )
    if identifier == "harness-empty":
        return (
            len(argv) == 2
            and argv
            == (
                bash,
                str(policy.source_root / "scripts" / "clean-room-phase1.sh"),
            )
            and cwd == policy.source_root
        )
    if identifier == "harness-certification":
        return (
            len(argv) == 10
            and argv[:2] == (bash, str(policy.source_root / "scripts" / "clean-room-phase1.sh"))
            and argv[2::2] == ("--python", "--wheelhouse", "--result", "--commit")
            and argv[3] == _normalize_argument(os.environ.get("MF_PHASE1_TEST_PYTHON", ""))
            and argv[5] == _normalize_argument(os.environ.get("MF_PHASE1_TEST_WHEELHOUSE", ""))
            and test_root is not None
            and _within(argv[7], (test_root,))
            and argv[9] == _checkout_commit(policy.source_root)
            and cwd == policy.source_root
        )
    if identifier == "spotify-harness":
        return (
            argv
            == (
                python,
                str(policy.source_root / "scripts" / "clean-room-spotify.py"),
            )
            and cwd == policy.source_root
        )
    return False


def _evidence_value(value: str, policy: BoundaryPolicy) -> str:
    candidate = Path(value)
    if value == str(Path(sys.executable).resolve()):
        return "{python}"
    phase_one_python = os.environ.get("MF_PHASE1_TEST_PYTHON")
    if phase_one_python and value == _normalize_argument(phase_one_python):
        return "{phase1-python}"
    phase_one_wheelhouse = os.environ.get("MF_PHASE1_TEST_WHEELHOUSE")
    if phase_one_wheelhouse and value == _normalize_argument(phase_one_wheelhouse):
        return "{phase1-wheelhouse}"
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        if resolved == policy.source_root:
            return "{source}"
        if resolved.is_relative_to(policy.source_root):
            return "{source}/" + resolved.relative_to(policy.source_root).as_posix()
        for name, root in sorted(policy.write_roots.items()):
            if resolved == root:
                return "{write:" + name + "}"
            if resolved.is_relative_to(root):
                return "{write:" + name + "}/" + resolved.relative_to(root).as_posix()
    return value


def _child_evidence(
    identifier: str, argv: tuple[str, ...], cwd: Path, policy: BoundaryPolicy
) -> dict[str, object]:
    normalized_argv: list[str] = []
    for index, value in enumerate(argv):
        if identifier == "python-artifact-smoke" and index == 3:
            normalized_argv.append("{validated-artifact-smoke}")
            continue
        if identifier == "scanner-test" and index == 2:
            normalized_argv.append("{write:pytest}/scan-target")
            continue
        if value.startswith("--output="):
            normalized_argv.append("--output=" + _evidence_value(value.split("=", 1)[1], policy))
        else:
            normalized_argv.append(_evidence_value(value, policy))
    return {
        "id": identifier,
        "argv": normalized_argv,
        "cwd": _evidence_value(str(cwd), policy),
    }


def _build_policy() -> BoundaryPolicy:
    repository_root = Path(__file__).parents[2]
    raw_roots = os.environ.get("MF_CLEAN_ROOM_WRITE_ROOTS")
    decoded_roots = json.loads(raw_roots) if raw_roots else {}
    if not isinstance(decoded_roots, dict):
        raise ValueError("MF_CLEAN_ROOM_WRITE_ROOTS must contain a JSON object")
    write_roots = {"venv": Path(sys.prefix)}
    if os.environ.get("MF_CLEAN_ROOM_ACTIVE") != "1":
        write_roots["test-temporary"] = Path(tempfile.gettempdir())
    write_roots.update({str(name): Path(value) for name, value in decoded_roots.items()})
    child_ids = [str(value) for value in _load_json_list("MF_CLEAN_ROOM_CHILD_COMMANDS")]
    if not child_ids:
        child_ids = [
            "git-ls-source",
            "git-ls-all",
            "git-archive-head",
            "git-source-head",
            "git-test-init",
            "git-test-add",
            "git-test-commit",
            "git-test-head",
            "python-build-export",
            "python-build-source",
            "python-artifact-smoke",
            "catalog-mcp-stdio",
            "hatchling-build",
            "installed-wheel-skill",
            "python-wheel-install",
            "python-wheel-venv",
            "scanner-source",
            "scanner-test",
            "harness-certification",
            "harness-empty",
            "spotify-harness",
        ]
    return BoundaryPolicy(
        allowed_write_roots=write_roots,
        allowed_children=(),
        allowed_child_ids=child_ids,
        source_root=repository_root,
    )


def _policy() -> BoundaryPolicy:
    if _POLICY is None:
        raise RuntimeError("clean-room policy is not initialized")
    return _POLICY


class GuardedSocket(_ORIGINAL_SOCKET):
    def bind(self, address: Any) -> None:
        _policy().reject_network("bind")

    def listen(self, backlog: int = 0) -> None:
        _policy().reject_network("listen")

    def connect(self, address: Any) -> None:
        _policy().reject_network("connect")

    def connect_ex(self, address: Any) -> int:
        _policy().reject_network("connect")

    def send(self, data: Any, flags: int = 0) -> int:
        _policy().reject_network("send")

    def sendall(self, data: Any, flags: int = 0) -> None:
        _policy().reject_network("sendall")

    def sendto(self, data: Any, *args: Any) -> int:
        _policy().reject_network("sendto")

    def sendmsg(self, buffers: Any, *args: Any) -> int:
        _policy().reject_network("sendmsg")


def blocked_create_connection(*args: Any, **kwargs: Any) -> Any:
    _policy().reject_network("connect")


def blocked_dns_lookup(*args: Any, **kwargs: Any) -> Any:
    _policy().reject_network("dns")


def blocked_urlopen(*args: Any, **kwargs: Any) -> Any:
    _policy().reject_network("urllib")


def _guarded_popen(args: Any, *popen_args: Any, **kwargs: Any) -> Any:
    if isinstance(args, (str, bytes)):
        raise BoundaryViolation("undeclared child process blocked")
    _policy().check_child(
        tuple(os.fspath(part) for part in args),
        shell=bool(kwargs.get("shell")),
        cwd=kwargs.get("cwd"),
    )
    return _ORIGINAL_POPEN(args, *popen_args, **kwargs)


class GuardedPopen:
    """Preserve ``Popen[T]`` imports while enforcing the child-process boundary."""

    @classmethod
    def __class_getitem__(cls, item: object) -> object:
        return _ORIGINAL_POPEN[item]

    def __new__(cls, args: Any, *popen_args: Any, **kwargs: Any) -> Any:
        return _guarded_popen(args, *popen_args, **kwargs)


def _write_mode(mode: str) -> bool:
    return any(character in mode for character in "wax+")


def _guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    if _write_mode(mode) and isinstance(file, (str, bytes, os.PathLike)):
        _policy().check_write(Path(os.fsdecode(file)), surface="file")
    return _ORIGINAL_OPEN(file, mode, *args, **kwargs)


def _guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    if _write_mode(mode) and isinstance(file, (str, bytes, os.PathLike)):
        _policy().check_write(Path(os.fsdecode(file)), surface="file")
    return _ORIGINAL_IO_OPEN(file, mode, *args, **kwargs)


def _guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if flags & write_flags and isinstance(path, (str, bytes, os.PathLike)):
        checked_path = Path(os.fsdecode(path))
        directory_descriptor = kwargs.get("dir_fd")
        if not checked_path.is_absolute() and directory_descriptor is not None:
            checked_path = descriptor_path(directory_descriptor) / checked_path
        _policy().check_write(checked_path, surface="file")
    return _ORIGINAL_OS_OPEN(path, flags, *args, **kwargs)


def _checked_path(path: Any, dir_fd: int | None = None, *, surface: str = "file") -> Path:
    candidate = Path(os.fsdecode(path))
    if not candidate.is_absolute() and dir_fd is not None:
        candidate = descriptor_path(dir_fd) / candidate
    _policy().check_write(candidate, surface=surface)
    return candidate


def _guarded_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(path, kwargs.get("dir_fd"), surface="directory")
    return _ORIGINAL_MKDIR(path, *args, **kwargs)


def _guarded_makedirs(name: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(name, surface="directory")
    return _ORIGINAL_MAKEDIRS(name, *args, **kwargs)


def _guarded_rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(path, kwargs.get("dir_fd"), surface="directory")
    return _ORIGINAL_RMDIR(path, *args, **kwargs)


def _guarded_removedirs(name: Any) -> None:
    _checked_path(name, surface="directory")
    return _ORIGINAL_REMOVEDIRS(name)


def _guarded_rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(src, kwargs.get("src_dir_fd"))
    _checked_path(dst, kwargs.get("dst_dir_fd"))
    return _ORIGINAL_RENAME(src, dst, *args, **kwargs)


def _guarded_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(src, kwargs.get("src_dir_fd"))
    _checked_path(dst, kwargs.get("dst_dir_fd"))
    return _ORIGINAL_REPLACE(src, dst, *args, **kwargs)


def _guarded_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(path, kwargs.get("dir_fd"))
    return _ORIGINAL_UNLINK(path, *args, **kwargs)


def _guarded_remove(path: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(path, kwargs.get("dir_fd"))
    return _ORIGINAL_REMOVE(path, *args, **kwargs)


def _guarded_link(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(src, kwargs.get("src_dir_fd"), surface="link")
    _checked_path(dst, kwargs.get("dst_dir_fd"), surface="link")
    return _ORIGINAL_LINK(src, dst, *args, **kwargs)


def _guarded_symlink(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
    _checked_path(dst, kwargs.get("dir_fd"), surface="link")
    return _ORIGINAL_SYMLINK(src, dst, *args, **kwargs)


def _guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
    raw = os.fspath(database) if isinstance(database, (str, bytes, os.PathLike)) else database
    if raw != ":memory:":
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        if isinstance(raw, str) and raw.startswith("file:"):
            raw = raw[5:].split("?", 1)[0]
        _policy().check_write(Path(raw), surface="sqlite")
    return _ORIGINAL_SQLITE_CONNECT(database, *args, **kwargs)


def pytest_configure(config: pytest.Config) -> None:
    global _INSTALLED, _POLICY
    if _INSTALLED:
        return
    _POLICY = _build_policy()
    socket.socket = GuardedSocket
    socket.create_connection = blocked_create_connection
    socket.getaddrinfo = blocked_dns_lookup
    socket.gethostbyname = blocked_dns_lookup
    socket.gethostbyname_ex = blocked_dns_lookup
    urllib.request.urlopen = blocked_urlopen
    urllib.request.urlretrieve = blocked_urlopen
    subprocess.Popen = GuardedPopen
    if os.environ.get("MF_CLEAN_ROOM_ACTIVE") == "1":
        builtins.open = _guarded_open
        io.open = _guarded_io_open
        os.open = _guarded_os_open
        os.mkdir = _guarded_mkdir
        os.makedirs = _guarded_makedirs
        os.rmdir = _guarded_rmdir
        os.removedirs = _guarded_removedirs
        os.rename = _guarded_rename
        os.replace = _guarded_replace
        os.unlink = _guarded_unlink
        os.remove = _guarded_remove
        os.link = _guarded_link
        os.symlink = _guarded_symlink
        sqlite3.connect = _guarded_sqlite_connect
        os.supports_dir_fd = os.supports_dir_fd | {
            _guarded_os_open,
            _guarded_mkdir,
            _guarded_rename,
            _guarded_replace,
            _guarded_unlink,
            _guarded_remove,
            _guarded_link,
        }
        os.supports_follow_symlinks = os.supports_follow_symlinks | {_guarded_link}
    _INSTALLED = True


@pytest.fixture(scope="session")
def boundary_policy() -> BoundaryPolicy:
    return _policy()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    evidence_path = os.environ.get("MF_CLEAN_ROOM_BOUNDARY_RESULT")
    if evidence_path is None or _POLICY is None:
        return
    payload = {
        "binds": _POLICY.binds,
        "connects": _POLICY.connects,
        "sends": _POLICY.sends,
        "browser_handoffs": _POLICY.browser_handoffs,
        "http_attempts": _POLICY.http_attempts,
        "callback_binds": _POLICY.callback_binds,
        "callback_closes": _POLICY.callback_closes,
        "write_checks": _POLICY.write_checks,
        "declared_writes": sorted(_POLICY.write_roots),
        "child_commands": _POLICY.child_commands,
        "status": "pass"
        if exitstatus == 0 and _POLICY.binds == _POLICY.connects == _POLICY.sends == 0
        else "fail",
    }
    with _ORIGINAL_OPEN(evidence_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
