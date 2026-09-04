#!/usr/bin/env python3
"""Scan a proposed public tree or Python artifact without revealing matches."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

MAX_SCANNED_BYTES = 16 * 1024 * 1024
CANARY = "MF_TEST_SECRET_" + "DO_NOT_PUBLISH_7d2a9c"
CONTENT_RULES = (
    (
        "credential-assignment",
        re.compile(
            r"(?ix)(?:"
            r"(?<![a-z0-9_'\"-])(?:[a-z][a-z0-9]*[_-]+)*(?:access[_-]?key[_-]?id|"
            r"access[_-]?token|api[_-]?key|client[_-]?secret|password|refresh[_-]?token)"
            r"(?![a-z0-9_-])\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{6,}"
            r"|['\"](?:access[_-]?key[_-]?id|access[_-]?token|api[_-]?key|client[_-]?secret|"
            r"password|refresh[_-]?token)['\"]\s*:\s*(?:['\"][A-Za-z0-9_./+\-=]{6,}['\"]"
            r"|[A-Za-z0-9_./+\-=]{6,})"
            r")"
        ),
    ),
    (
        "bearer-authorization",
        re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=\-]{6,}"),
    ),
    ("private-key-header", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("credential-canary", re.compile(re.escape(CANARY))),
    (
        "unsafe-path",
        re.compile(
            r"(?:/(?:Users|home)/[^/\s]+/|[A-Za-z]:["
            + chr(92)
            + r"/]Users["
            + chr(92)
            + r"/][^"
            + chr(92)
            + r"/\s]+["
            + chr(92)
            + r"/])"
        ),
    ),
    (
        "unsafe-path",
        re.compile(r"(?:^|[\s'\"])/(?:[^\s'\"]+/)*(?:Development|Projects|checkouts|worktrees)/"),
    ),
    ("unicode-variation-selector", re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef]")),
    (
        "emoji",
        re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0000231a-\U0000231b]"),
    ),
)
RAW_CONTENT_RULES = tuple(
    (
        rule,
        re.compile(pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE),
    )
    for rule, pattern in CONTENT_RULES[:4]
)
_SQLITE_RECORD_CREDENTIAL_RULE = re.compile(
    rb"\x02Saccess[_-]?token\s*[:=]\s*[A-Za-z0-9_./+\-=]{6,}", re.IGNORECASE
)
RAW_CONTENT_RULES += (("credential-assignment", _SQLITE_RECORD_CREDENTIAL_RULE),)
TEXT_CONTENT_RULES = CONTENT_RULES[4:]
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
GENERATED_PARTS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar", ".tar.gz", ".tgz")


def _path_rules(relative: PurePosixPath) -> tuple[str, ...]:
    name = relative.name
    rules: list[str] = []
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        rules.append("prohibited-file")
    if any(part in GENERATED_PARTS for part in relative.parts) or name.endswith((".pyc", ".pyo")):
        rules.append("generated-content")
    if relative.suffix.lower() in DATABASE_SUFFIXES or name.endswith((".db-wal", ".db-shm")):
        rules.append("local-database")
    return tuple(rules)


def _content_rules(data: bytes) -> tuple[str, ...]:
    if len(data) > MAX_SCANNED_BYTES:
        return ()
    rules = [rule for rule, pattern in RAW_CONTENT_RULES if pattern.search(data)]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return tuple(rules)
    rules.extend(rule for rule, pattern in TEXT_CONTENT_RULES if pattern.search(text))
    return tuple(rules)


def _is_safe_member(name: str) -> bool:
    if "\\" in name or re.match(r"^[A-Za-z]:", name):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and name not in {"", "."}


def _scan_zip(path: Path, label: str) -> tuple[list[dict[str, str]], int]:
    findings: list[dict[str, str]] = []
    scanned = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for index, member in enumerate(archive.infolist()):
                display = f"{label}!{member.filename}"
                mode = member.external_attr >> 16
                if not _is_safe_member(member.filename):
                    findings.append(
                        {"path": f"{label}!unsafe-member-{index}", "rule": "unsafe-archive-member"}
                    )
                    continue
                if member.is_dir():
                    continue
                file_type = stat.S_IFMT(mode) if member.create_system == 3 else 0
                if file_type and not stat.S_ISREG(mode):
                    findings.append(
                        {"path": f"{label}!unsafe-member-{index}", "rule": "unsafe-archive-member"}
                    )
                    continue
                if member.file_size > MAX_SCANNED_BYTES:
                    findings.append({"path": display, "rule": "oversized-authored-file"})
                    continue
                scanned += 1
                relative = PurePosixPath(member.filename)
                for rule in _path_rules(relative):
                    findings.append({"path": display, "rule": rule})
                for rule in _content_rules(archive.read(member)):
                    findings.append({"path": display, "rule": rule})
    except (OSError, zipfile.BadZipFile):
        findings.append({"path": label, "rule": "invalid-archive"})
    return findings, scanned


def _scan_tar(path: Path, label: str) -> tuple[list[dict[str, str]], int]:
    findings: list[dict[str, str]] = []
    scanned = 0
    try:
        with tarfile.open(path, "r:*") as archive:
            for index, member in enumerate(archive.getmembers()):
                display = f"{label}!{member.name}"
                if not _is_safe_member(member.name) or not (member.isfile() or member.isdir()):
                    findings.append(
                        {"path": f"{label}!unsafe-member-{index}", "rule": "unsafe-archive-member"}
                    )
                    continue
                if member.isdir():
                    continue
                if member.size > MAX_SCANNED_BYTES:
                    findings.append({"path": display, "rule": "oversized-authored-file"})
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    findings.append({"path": display, "rule": "invalid-archive-member"})
                    continue
                scanned += 1
                relative = PurePosixPath(member.name)
                for rule in _path_rules(relative):
                    findings.append({"path": display, "rule": rule})
                for rule in _content_rules(stream.read(MAX_SCANNED_BYTES + 1)):
                    findings.append({"path": display, "rule": rule})
    except (OSError, tarfile.TarError):
        findings.append({"path": label, "rule": "invalid-archive"})
    return findings, scanned


def _is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def _scan_regular(path: Path, label: str) -> tuple[list[dict[str, str]], int]:
    if _is_archive(path):
        if path.name.lower().endswith((".whl", ".zip")):
            return _scan_zip(path, label)
        return _scan_tar(path, label)
    findings = [{"path": label, "rule": rule} for rule in _path_rules(PurePosixPath(label))]
    try:
        size = path.stat(follow_symlinks=False).st_size
        if size > MAX_SCANNED_BYTES:
            findings.append({"path": label, "rule": "oversized-authored-file"})
            return findings, 0
        data = path.read_bytes()
    except OSError:
        findings.append({"path": label, "rule": "unreadable-file"})
        return findings, 0
    findings.extend({"path": label, "rule": rule} for rule in _content_rules(data))
    return findings, 1


def _walk(root: Path) -> Iterable[tuple[Path, str, bool]]:
    pending = [root]
    while pending:
        current = pending.pop()
        relative = current.relative_to(root).as_posix()
        label = relative if relative != "." else current.name
        try:
            metadata = current.lstat()
        except OSError:
            yield current, label, True
            continue
        if stat.S_ISLNK(metadata.st_mode):
            yield current, label, True
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
            except OSError:
                yield current, label, True
                continue
            pending.extend(children)
        elif stat.S_ISREG(metadata.st_mode):
            yield current, label, False
        else:
            yield current, label, True


def scan(root: Path) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    scanned = 0
    if not root.is_absolute():
        root = Path.cwd() / root
    if not root.exists() and not root.is_symlink():
        return {
            "findings": [{"path": root.name, "rule": "missing-root"}],
            "scanned_files": 0,
            "status": "fail",
        }
    if root.is_file() and not root.is_symlink():
        findings, scanned = _scan_regular(root, root.name)
    else:
        for path, label, unsafe in _walk(root):
            if unsafe:
                findings.append({"path": label, "rule": "unsafe-symlink"})
                continue
            current, count = _scan_regular(path, label)
            findings.extend(current)
            scanned += count
    unique = sorted({(item["path"], item["rule"]) for item in findings})
    normalized = [{"path": path, "rule": rule} for path, rule in unique]
    return {
        "findings": normalized,
        "scanned_files": scanned,
        "status": "fail" if normalized else "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    arguments = parser.parse_args(argv)
    result = scan(arguments.root)
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
