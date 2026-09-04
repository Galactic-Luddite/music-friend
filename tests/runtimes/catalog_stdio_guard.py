"""Deny-by-default side-effect guard for the catalog MCP test fixture child."""

from __future__ import annotations

import builtins
import io
import os
import socket
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote


class FixtureBoundaryError(RuntimeError):
    """Raised when the synthetic fixture attempts an operation outside its local catalog."""


class FixtureGuard:
    """Limit the executable fixture to its supplied catalog directory and stdio."""

    def __init__(self, catalog_path: Path) -> None:
        self._root = catalog_path.parent.resolve()
        self._catalog = catalog_path.resolve()
        self._originals: list[tuple[object, str, object]] = []
        self._directory_fds: dict[int, Path] = {}

    def install(self) -> Callable[[], None]:
        self._replace(socket, "socket", self._guarded_socket_type())
        self._replace(socket, "create_connection", self._deny)
        self._replace(socket, "getaddrinfo", self._deny)
        self._replace(socket, "gethostbyname", self._deny)
        self._replace(socket, "gethostbyname_ex", self._deny)
        self._replace(subprocess, "Popen", self._deny)
        self._replace(subprocess, "run", self._deny)
        self._replace(subprocess, "call", self._deny)
        self._replace(subprocess, "check_call", self._deny)
        self._replace(subprocess, "check_output", self._deny)
        self._replace(os, "system", self._deny)
        for name in (
            "posix_spawn",
            "posix_spawnp",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "execl",
            "execle",
            "execlp",
            "execlpe",
        ):
            self._replace_if_present(os, name, self._deny)
        self._replace(builtins, "open", self._open)
        self._replace(io, "open", self._open)
        self._replace(os, "open", self._os_open)
        self._replace(os, "close", self._close)
        self._replace(os, "mkdir", self._mkdir)
        self._replace(os, "makedirs", self._makedirs)
        self._replace(os, "remove", self._remove)
        self._replace(os, "unlink", self._unlink)
        self._replace(os, "rmdir", self._rmdir)
        self._replace(os, "removedirs", self._removedirs)
        self._replace(os, "rename", self._rename)
        self._replace(os, "replace", self._rename)
        self._replace(os, "link", self._deny)
        self._replace(os, "symlink", self._deny)
        self._replace(os, "truncate", self._deny)
        self._replace(os, "ftruncate", self._deny)
        self._replace(sqlite3, "connect", self._connect)
        return self.restore

    def restore(self) -> None:
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)
        self._originals.clear()

    def assert_restrictions(self) -> None:
        probes: list[Callable[[], object]] = [
            lambda: socket.socket(),
            lambda: socket.gethostbyname("fixture-guard-probe"),
            lambda: socket.gethostbyname_ex("fixture-guard-probe"),
            lambda: subprocess.Popen(["fixture-guard-probe"]),
            lambda: os.removedirs(self._root.parent / "fixture-guard-probe"),
            lambda: (self._root.parent / "fixture-guard-probe.txt").write_text(
                "blocked", encoding="utf-8"
            ),
        ]
        for name in (
            "posix_spawn",
            "posix_spawnp",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "truncate",
            "ftruncate",
            "link",
            "symlink",
        ):
            if hasattr(os, name):
                probes.append(lambda name=name: getattr(os, name)("fixture-guard-probe"))
        for probe in probes:
            try:
                probe()
            except FixtureBoundaryError:
                continue
            raise RuntimeError("fixture guard probe was not denied")

    def _replace(self, owner: object, name: str, replacement: object) -> None:
        self._originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def _replace_if_present(self, owner: object, name: str, replacement: object) -> None:
        if hasattr(owner, name):
            self._replace(owner, name, replacement)

    def _deny(self, *args: object, **kwargs: object) -> None:
        raise FixtureBoundaryError("fixture side effect denied")

    def _guarded_socket_type(self) -> type[socket.socket]:
        original = socket.socket

        class GuardedSocket(original):
            def __new__(
                cls,
                family: int = socket.AF_INET,
                type: int = socket.SOCK_STREAM,
                proto: int = 0,
                fileno: int | None = None,
            ) -> socket.socket:
                if fileno is None:
                    raise FixtureBoundaryError("fixture side effect denied")
                return original.__new__(cls, family, type, proto, fileno)

        return GuardedSocket

    def _open(self, file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if any(value in mode for value in "wax+") and not isinstance(file, int):
            self._require_path(file)
        return self._original(builtins, "open")(file, mode, *args, **kwargs)

    def _os_open(self, path: object, flags: int, *args: object, **kwargs: object) -> int:
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        dir_fd = kwargs.get("dir_fd")
        candidate = Path(os.fspath(path))
        if not candidate.is_absolute() and type(dir_fd) is int:
            if dir_fd not in self._directory_fds:
                raise FixtureBoundaryError("fixture filesystem access denied")
            candidate = self._directory_fds[dir_fd] / candidate
        if flags & write_flags:
            self._require_path(candidate)
        descriptor = self._original(os, "open")(path, flags, *args, **kwargs)
        resolved_candidate = candidate.resolve(strict=False)
        if self._root.is_relative_to(resolved_candidate):
            self._directory_fds[descriptor] = resolved_candidate
        return descriptor

    def _close(self, descriptor: int) -> None:
        self._directory_fds.pop(descriptor, None)
        self._original(os, "close")(descriptor)

    def _mkdir(self, path: object, *args: object, **kwargs: object) -> object:
        self._require_path(path, dir_fd=kwargs.get("dir_fd"))
        return self._original(os, "mkdir")(path, *args, **kwargs)

    def _makedirs(self, path: object, *args: object, **kwargs: object) -> object:
        self._require_path(path)
        return self._original(os, "makedirs")(path, *args, **kwargs)

    def _remove(self, path: object, *args: object, **kwargs: object) -> object:
        self._require_path(path, dir_fd=kwargs.get("dir_fd"))
        return self._original(os, "remove")(path, *args, **kwargs)

    def _unlink(self, path: object, *args: object, **kwargs: object) -> object:
        self._require_path(path, dir_fd=kwargs.get("dir_fd"))
        return self._original(os, "unlink")(path, *args, **kwargs)

    def _rmdir(self, path: object, *args: object, **kwargs: object) -> object:
        self._require_path(path, dir_fd=kwargs.get("dir_fd"))
        return self._original(os, "rmdir")(path, *args, **kwargs)

    def _removedirs(self, path: object) -> object:
        self._require_path(path)
        return self._original(os, "removedirs")(path)

    def _rename(
        self, source: object, destination: object, *args: object, **kwargs: object
    ) -> object:
        self._require_path(source, dir_fd=kwargs.get("src_dir_fd"))
        self._require_path(destination, dir_fd=kwargs.get("dst_dir_fd"))
        return self._original(os, "rename")(source, destination, *args, **kwargs)

    def _connect(self, database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if database != ":memory:":
            value = os.fspath(database)
            path = unquote(value[5:].split("?", 1)[0]) if value.startswith("file:") else value
            self._require_path(path)
        return self._original(sqlite3, "connect")(database, *args, **kwargs)

    def _require_path(self, value: object, *, dir_fd: object = None) -> None:
        path = Path(os.fspath(value))
        if not path.is_absolute() and type(dir_fd) is int:
            if dir_fd not in self._directory_fds:
                raise FixtureBoundaryError("fixture filesystem access denied")
            path = self._directory_fds[dir_fd] / path
        path = path.resolve(strict=False)
        if path != self._catalog and not path.is_relative_to(self._root):
            raise FixtureBoundaryError("fixture filesystem access denied")

    def _original(self, owner: object, name: str) -> Callable[..., Any]:
        for current_owner, current_name, original in self._originals:
            if current_owner is owner and current_name == name:
                return original  # type: ignore[return-value]
        raise RuntimeError("fixture guard was not installed")
