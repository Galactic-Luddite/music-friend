"""Portable atomic replacement for local-only configuration and credential files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_replace(path: Path, value: bytes, *, prefix: str) -> bool:
    """Atomically replace a local file, restricting POSIX permissions when available."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _restrict_path(path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
        temporary = Path(temporary_name)
        _restrict_descriptor(descriptor)
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        _restrict_path(path, 0o600)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _restrict_descriptor(descriptor: int) -> None:
    operation = getattr(os, "fchmod", None)
    if callable(operation):
        try:
            operation(descriptor, 0o600)
        except OSError:
            pass


def _restrict_path(path: Path, mode: int) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


__all__ = ["atomic_replace"]
