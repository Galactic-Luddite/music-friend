"""Tests for the subprocess-local guard used by the catalog MCP fixture."""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from catalog_stdio_guard import FixtureBoundaryError, FixtureGuard  # noqa: E402


def test_fixture_guard_blocks_network_children_and_writes_outside_its_catalog_root(
    tmp_path: Path,
) -> None:
    """Catches a synthetic child fixture gaining a side effect outside its supplied local catalog."""
    catalog_path = tmp_path / "catalog.sqlite3"
    guard = FixtureGuard(catalog_path)
    restore = guard.install()
    try:
        guard.assert_restrictions()
        with pytest.raises(FixtureBoundaryError):
            socket.socket()
        with pytest.raises(FixtureBoundaryError):
            subprocess.Popen(["unavailable-command"])
        with pytest.raises(FixtureBoundaryError):
            (tmp_path.parent / "outside.txt").write_text("blocked", encoding="utf-8")
        connection = sqlite3.connect(catalog_path)
        connection.close()
        assert os.path.exists(catalog_path)
    finally:
        restore()


@pytest.mark.parametrize("name", ("gethostbyname", "gethostbyname_ex"))
def test_fixture_guard_blocks_hostname_lookup_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Catches a fixture edit bypassing the guarded socket constructor through DNS helpers."""
    monkeypatch.setattr(socket, name, lambda *_args, **_kwargs: None)
    guard = FixtureGuard(tmp_path / "catalog.sqlite3")
    restore = guard.install()
    try:
        with pytest.raises(FixtureBoundaryError):
            getattr(socket, name)("example.test")
    finally:
        restore()


@pytest.mark.parametrize(
    "name",
    (
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
    ),
)
def test_fixture_guard_blocks_available_process_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Catches a fixture edit escaping the subprocess guard through an OS process API."""
    if not hasattr(os, name):
        pytest.skip(f"{name} is unavailable")
    monkeypatch.setattr(os, name, lambda *_args, **_kwargs: None)
    guard = FixtureGuard(tmp_path / "catalog.sqlite3")
    restore = guard.install()
    try:
        with pytest.raises(FixtureBoundaryError):
            getattr(os, name)("fixture-guard-probe")
    finally:
        restore()


@pytest.mark.parametrize("name", ("truncate", "ftruncate", "link", "symlink", "removedirs"))
def test_fixture_guard_blocks_destructive_filesystem_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Catches an omitted destructive filesystem primitive in the fixture-local guard."""
    monkeypatch.setattr(os, name, lambda *_args, **_kwargs: None)
    guard = FixtureGuard(tmp_path / "catalog.sqlite3")
    restore = guard.install()
    try:
        with pytest.raises(FixtureBoundaryError):
            if name == "removedirs":
                getattr(os, name)(str(tmp_path.parent / "outside"))
            else:
                getattr(os, name)(str(tmp_path.parent / "outside"), 0)
    finally:
        restore()
