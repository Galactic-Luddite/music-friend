"""Install Music Friend's packaged Agent Skill into a local skills directory."""

from __future__ import annotations

import os
import secrets
import stat
from importlib import resources
from pathlib import Path

_CLIENT_ROOTS = {
    "codex": Path(".agents") / "skills",
    "claude": Path(".claude") / "skills",
}
_SKILL_RELATIVE_PATH = Path("music-friend") / "SKILL.md"


class SkillInstallError(ValueError):
    """Raised when a skill destination is invalid or contains conflicting content."""


def install_skill(
    *, client: str | None = None, target: Path | None = None, replace: bool = False
) -> bool:
    """Install the packaged skill and return whether the destination changed."""
    if (client is None) == (target is None):
        raise SkillInstallError("select exactly one client or target")

    if client is not None:
        relative_root = _CLIENT_ROOTS.get(client)
        if relative_root is None:
            raise SkillInstallError("unsupported skill client")
        root = _absolute(Path.home() / relative_root)
        create_root = True
    else:
        if not isinstance(target, Path):
            raise SkillInstallError("skill target must be a directory")
        root = _absolute(target)
        create_root = False

    destination = _absolute(root / _SKILL_RELATIVE_PATH)
    if not destination.is_relative_to(root):
        raise SkillInstallError("skill destination is outside the selected root")

    content = _read_packaged_skill()
    root_descriptor = _open_directory_tree(root, create_missing=create_root)
    try:
        parent_descriptor = _open_relative_directory_tree(
            root_descriptor,
            _SKILL_RELATIVE_PATH.parent.parts,
        )
        try:
            destination_name = _SKILL_RELATIVE_PATH.name
            existing = _regular_file_or_none(parent_descriptor, destination_name)
            if existing is not None:
                if existing == content:
                    return False
                if not replace:
                    raise SkillInstallError("skill already exists with different content")

            _atomic_write(
                parent_descriptor,
                destination_name,
                content,
                replace=replace,
            )
            return True
        finally:
            os.close(parent_descriptor)
    finally:
        os.close(root_descriptor)


def _read_packaged_skill() -> bytes:
    return resources.files(__package__).joinpath("SKILL.md").read_bytes()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_secure_directory_operations() -> None:
    required = (os.open, os.mkdir, os.stat, os.rename, os.link, os.unlink)
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in required)
        or os.stat not in os.supports_follow_symlinks
        or os.link not in os.supports_follow_symlinks
    ):
        raise SkillInstallError(
            "secure directory-relative skill installation is unavailable on this platform"
        )


def _open_directory_tree(path: Path, *, create_missing: bool) -> int:
    _require_secure_directory_operations()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError as error:
        raise SkillInstallError("skill root cannot be opened securely") from error

    for part in path.parts[1:]:
        try:
            try:
                child_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_missing:
                    raise SkillInstallError("skill root must be an existing directory") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child_descriptor = os.open(part, flags, dir_fd=descriptor)
        except SkillInstallError:
            os.close(descriptor)
            raise
        except OSError as error:
            os.close(descriptor)
            raise SkillInstallError(
                "skill paths must contain only directories and no symlinks"
            ) from error
        os.close(descriptor)
        descriptor = child_descriptor
    return descriptor


def _open_relative_directory_tree(root_descriptor: int, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.dup(root_descriptor)
    for part in parts:
        try:
            try:
                child_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child_descriptor = os.open(part, flags, dir_fd=descriptor)
        except OSError as error:
            os.close(descriptor)
            raise SkillInstallError(
                "skill paths must contain only directories and no symlinks"
            ) from error
        os.close(descriptor)
        descriptor = child_descriptor
    return descriptor


def _regular_file_or_none(parent_descriptor: int, name: str) -> bytes | None:
    try:
        mode = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        ).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        raise SkillInstallError("symlinked skill paths are not allowed")
    if not stat.S_ISREG(mode):
        raise SkillInstallError("skill destination must be a regular file")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise SkillInstallError("skill destination cannot be opened securely") from error
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SkillInstallError("skill destination must be a regular file")
        return stream.read()


def _atomic_write(
    parent_descriptor: int,
    destination_name: str,
    content: bytes,
    *,
    replace: bool,
) -> None:
    temporary_name, descriptor = _create_temporary_file(parent_descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if replace:
                os.replace(
                    temporary_name,
                    destination_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            else:
                os.link(
                    temporary_name,
                    destination_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileExistsError:
            raise SkillInstallError("skill already exists with different content") from None
        except (NotImplementedError, TypeError) as error:
            raise SkillInstallError(
                "secure directory-relative skill installation is unavailable on this platform"
            ) from error
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise


def _create_temporary_file(parent_descriptor: int) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _attempt in range(100):
        name = f".SKILL.md.{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
    raise SkillInstallError("could not create a unique temporary skill file")


__all__ = ["SkillInstallError", "install_skill"]
