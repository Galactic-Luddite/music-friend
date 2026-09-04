from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from . import boundaries
from .boundaries import BoundaryPolicy, BoundaryViolation, descriptor_path


def test_policy_allows_writes_only_below_declared_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    policy = BoundaryPolicy(allowed_write_roots=(allowed,), allowed_children=())

    policy.check_write(allowed / "nested" / "result.json")
    with pytest.raises(BoundaryViolation, match="filesystem write blocked"):
        policy.check_write(tmp_path / "outside.txt")


def test_policy_rejects_relative_write_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = BoundaryPolicy(
        allowed_write_roots=(tmp_path / "allowed",),
        allowed_children=(),
    )

    with pytest.raises(BoundaryViolation, match="filesystem write blocked"):
        policy.check_write(Path("ambiguous.txt"))


def test_policy_does_not_treat_prefix_collision_as_descendant(tmp_path: Path) -> None:
    allowed = tmp_path / "root"
    policy = BoundaryPolicy(allowed_write_roots=(allowed,), allowed_children=())

    with pytest.raises(BoundaryViolation, match="filesystem write blocked"):
        policy.check_write(tmp_path / "root-other" / "file.txt")


def test_session_records_no_network_activity(boundary_policy: BoundaryPolicy) -> None:
    assert boundary_policy.binds == 0
    assert boundary_policy.connects == 0
    assert boundary_policy.sends == 0


def test_mutation_and_sqlite_surfaces_are_guarded_during_certification() -> None:
    if os.environ.get("MF_CLEAN_ROOM_ACTIVE") != "1":
        pytest.skip("certification-only enforcement")
    assert os.mkdir.__name__ == "_guarded_mkdir"
    assert os.rename.__name__ == "_guarded_rename"
    assert os.unlink.__name__ == "_guarded_unlink"
    assert os.symlink.__name__ == "_guarded_symlink"
    assert sqlite3.connect.__name__ == "_guarded_sqlite_connect"
    for function in (os.open, os.mkdir, os.rename, os.link, os.unlink):
        assert function in os.supports_dir_fd
    assert os.link in os.supports_follow_symlinks


@pytest.mark.parametrize(
    ("wrapper", "original", "arguments"),
    (
        ("_guarded_mkdir", "_ORIGINAL_MKDIR", ("blocked",)),
        ("_guarded_rename", "_ORIGINAL_RENAME", ("blocked", "blocked-two")),
        ("_guarded_replace", "_ORIGINAL_REPLACE", ("blocked", "blocked-two")),
        ("_guarded_unlink", "_ORIGINAL_UNLINK", ("blocked",)),
        ("_guarded_remove", "_ORIGINAL_REMOVE", ("blocked",)),
        ("_guarded_link", "_ORIGINAL_LINK", ("blocked", "blocked-two")),
        ("_guarded_symlink", "_ORIGINAL_SYMLINK", ("blocked", "blocked-two")),
        ("_guarded_sqlite_connect", "_ORIGINAL_SQLITE_CONNECT", ("blocked.sqlite3",)),
    ),
)
def test_mutation_guards_refuse_undeclared_paths_before_original_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    original: str,
    arguments: tuple[str, ...],
) -> None:
    allowed = tmp_path / "allowed"
    policy = BoundaryPolicy(allowed_write_roots=(allowed,), allowed_children=())
    monkeypatch.setattr(boundaries, "_POLICY", policy)
    original_called = False

    def trap(*args: object, **kwargs: object) -> None:
        nonlocal original_called
        original_called = True
        raise AssertionError("original I/O must not be reached")

    monkeypatch.setattr(boundaries, original, trap)
    blocked = tuple(str(tmp_path.parent / argument) for argument in arguments)
    with pytest.raises(BoundaryViolation, match="filesystem write blocked"):
        getattr(boundaries, wrapper)(*blocked)
    assert original_called is False


def test_descriptor_path_resolves_open_directory(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY)
    try:
        assert descriptor_path(descriptor) == tmp_path.resolve()
    finally:
        os.close(descriptor)


def test_descriptor_path_fails_closed_when_platform_lookup_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(boundaries, "fcntl", None)

    with pytest.raises(BoundaryViolation, match="descriptor path unavailable"):
        descriptor_path(-1)
