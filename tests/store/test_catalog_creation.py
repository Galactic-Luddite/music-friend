from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest

from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.store.catalog import (
    _open_parent_posix,
    _prepare_database_fallback,
    _prepare_database_posix,
    _UnsafeCatalogPath,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes are unavailable")
def test_open_creates_private_parent_and_database(catalog_path: Path) -> None:
    catalog = Catalog.open(catalog_path)
    catalog.close()

    assert _mode(catalog_path.parent) == 0o700
    assert _mode(catalog_path) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes are unavailable")
def test_open_tightens_broad_existing_modes(tmp_path: Path) -> None:
    parent = tmp_path / "catalog"
    parent.mkdir(mode=0o777)
    path = parent / "music.sqlite3"
    path.touch(mode=0o666)
    parent.chmod(0o777)
    path.chmod(0o666)

    Catalog.open(path).close()

    assert _mode(parent) == 0o700
    assert _mode(path) == 0o600


def test_open_refuses_symlink_database_path(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "catalog.sqlite3"
    link.symlink_to(target)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(link)


def test_open_refuses_symlink_parent_component(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(linked_parent / "catalog.sqlite3")

    assert not (real_parent / "catalog.sqlite3").exists()


def test_open_refuses_parent_traversal_component(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    requested = staging / ".." / "catalog.sqlite3"

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(requested)

    assert not staging.exists()
    assert not (tmp_path / "catalog.sqlite3").exists()


def test_open_refuses_nonregular_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "catalog-directory"
    path.mkdir()

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes are unavailable")
def test_open_sets_every_new_parent_component_to_0700_despite_umask(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = first / "second"
    path = second / "catalog.sqlite3"
    previous_umask = os.umask(0o777)
    try:
        Catalog.open(path).close()
    finally:
        os.umask(previous_umask)

    assert _mode(first) == 0o700
    assert _mode(second) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative traversal requires POSIX")
def test_parent_creation_recovers_when_another_opener_wins_the_create_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a concurrent parent creator turning a safe open into a false failure."""
    import music_friend.store.catalog as catalog_module

    parent_name = "concurrently-created-parent"
    path = tmp_path / parent_name / "catalog.sqlite3"
    real_mkdir = os.mkdir
    raced = False

    def create_then_report_existing(
        component: str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        nonlocal raced
        if component == parent_name and not raced:
            raced = True
            real_mkdir(component, mode, dir_fd=dir_fd)
            raise FileExistsError
        real_mkdir(component, mode, dir_fd=dir_fd)

    monkeypatch.setattr(catalog_module.os, "mkdir", create_then_report_existing)

    parent_fd = _open_parent_posix(path)
    try:
        assert raced is True
        assert os.path.samefile(parent_fd, path.parent)
    finally:
        os.close(parent_fd)


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative traversal requires POSIX")
def test_database_creation_recovers_when_another_opener_wins_the_create_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a concurrent database creator turning a safe open into a false failure."""
    import music_friend.store.catalog as catalog_module

    name = "concurrently-created.sqlite3"
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(tmp_path, parent_flags)
    real_open = os.open
    raced = False

    def create_then_report_existing(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if path == name and flags & os.O_EXCL and not raced:
            raced = True
            created_fd = real_open(path, flags, mode, dir_fd=dir_fd)
            os.close(created_fd)
            raise FileExistsError
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(catalog_module.os, "open", create_then_report_existing)

    database_fd = -1
    try:
        database_fd, metadata, created = _prepare_database_posix(parent_fd, name)
        assert raced is True
        assert created is False
        assert stat.S_ISREG(metadata.st_mode)
    finally:
        if database_fd >= 0:
            os.close(database_fd)
        os.close(parent_fd)


def test_open_configures_sqlite_safety_pragmas(catalog_path: Path) -> None:
    catalog = Catalog.open(catalog_path)
    connection = cast(Any, catalog)._connection

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert 1 <= busy_timeout <= 10_000
    catalog.close()


def test_close_is_idempotent_and_context_manager_closes(catalog_path: Path) -> None:
    with Catalog.open(catalog_path) as catalog:
        assert catalog.get_check_time("source") is None

    catalog.close()
    with pytest.raises(CatalogUnavailableError):
        catalog.get_check_time("source")


def test_open_requires_an_explicit_path() -> None:
    with pytest.raises(TypeError):
        Catalog.open("catalog.sqlite3")  # type: ignore[arg-type]


def test_operational_failure_is_path_free(tmp_path: Path) -> None:
    blocker = tmp_path / "private-location-marker"
    blocker.write_text("not a directory", encoding="utf-8")
    requested = blocker / "catalog.sqlite3"

    with pytest.raises(CatalogUnavailableError) as caught:
        Catalog.open(requested)

    assert str(requested) not in str(caught.value)
    assert str(requested) not in repr(caught.value)
    assert caught.value.to_public_dict() == {
        "category": "catalog_unavailable",
        "message": "The local catalog is unavailable.",
    }


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative traversal requires POSIX")
def test_intermediate_path_swap_cannot_redirect_open(
    catalog_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    held_parent = tmp_path / "held-parent"
    replacement_parent = tmp_path / "replacement-parent"
    replacement_parent.mkdir(mode=0o700)
    replacement_database = replacement_parent / catalog_path.name
    replacement_database.touch(mode=0o600)

    def swap_then_connect(database: str, **kwargs: object) -> sqlite3.Connection:
        catalog_path.parent.rename(held_parent)
        catalog_path.parent.symlink_to(replacement_parent, target_is_directory=True)
        return real_connect(database, **kwargs)

    monkeypatch.setattr("music_friend.store.catalog.sqlite3.connect", swap_then_connect)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert not (held_parent / catalog_path.name).exists()
    with closing(real_connect(replacement_database)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            == []
        )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative traversal requires POSIX")
def test_final_path_swap_is_detected_and_created_inode_is_cleaned(
    catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    relocated = catalog_path.with_name("relocated-created-file")

    def swap_then_connect(database: str, **kwargs: object) -> sqlite3.Connection:
        catalog_path.rename(relocated)
        catalog_path.touch(mode=0o600)
        return real_connect(database, **kwargs)

    monkeypatch.setattr("music_friend.store.catalog.sqlite3.connect", swap_then_connect)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert not relocated.exists()
    assert catalog_path.exists()
    with closing(real_connect(catalog_path)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            == []
        )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative cleanup requires POSIX")
def test_new_database_is_cleaned_when_descriptor_chmod_fails(
    catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fchmod = os.fchmod

    def fail_for_regular_file(descriptor: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("synthetic chmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr("music_friend.store.catalog.os.fchmod", fail_for_regular_file)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert not catalog_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative cleanup requires POSIX")
def test_new_database_is_cleaned_when_descriptor_stat_fails(
    catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = os.fstat

    def fail_for_regular_file(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            raise OSError("synthetic fstat failure")
        return metadata

    monkeypatch.setattr("music_friend.store.catalog.os.fstat", fail_for_regular_file)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert not catalog_path.exists()


def test_direct_construction_cannot_bypass_secure_open() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(TypeError):
            Catalog(connection)  # type: ignore[arg-type]
    finally:
        connection.close()


def test_fallback_cleans_created_file_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fallback-fstat.sqlite3"

    def fail(descriptor: int) -> os.stat_result:
        raise OSError("synthetic fallback fstat failure")

    monkeypatch.setattr("music_friend.store.catalog.os.fstat", fail)

    with pytest.raises(OSError, match="fstat failure"):
        _prepare_database_fallback(path)

    assert not path.exists()


def test_fallback_cleans_created_file_when_chmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fallback-chmod.sqlite3"

    def fail(path: Path, mode: int) -> None:
        raise OSError("synthetic fallback chmod failure")

    monkeypatch.setattr("music_friend.store.catalog.os.chmod", fail)

    with pytest.raises(OSError, match="chmod failure"):
        _prepare_database_fallback(path)

    assert not path.exists()


def test_fallback_cleans_created_file_when_descriptor_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fallback-validation.sqlite3"
    decoy = tmp_path / "fallback-validation-decoy"
    decoy.mkdir()

    def return_nonregular_metadata(descriptor: int) -> os.stat_result:
        return decoy.stat()

    monkeypatch.setattr("music_friend.store.catalog.os.fstat", return_nonregular_metadata)

    with pytest.raises(_UnsafeCatalogPath):
        _prepare_database_fallback(path)

    assert not path.exists()
