#!/usr/bin/env python3
"""Certify one exact local Spotify adapter artifact without external I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 32 * 1024
COMMAND_TIMEOUT_SECONDS = 900
COMMAND_IDS = (
    "archive-source",
    "scan-archive",
    "create-environment",
    "offline-install",
    "scan-export",
    "prepare-test-checkout",
    "positive-controls",
    "pytest-with-coverage",
    "typecheck",
    "ruff-check",
    "ruff-format",
    "build-artifacts",
    "scan-wheel",
    "scan-sdist",
    "artifact-install",
    "artifact-import-smoke",
    "mcp-protocol-gate",
    "scan-isolated-state",
)
SCAN_NAMES = (
    "archive",
    "export",
    "wheel",
    "sdist",
    "home",
    "config",
    "cache",
    "temporary",
    "working",
    "catalog",
    "logs",
    "credential_fake",
)
POSITIVE_CONTROL_NODES = {
    "artifact-digest-mismatch": (
        "tests/clean_room/test_spotify_boundaries.py::"
        "test_artifact_digest_mismatch_positive_control"
    ),
    "authorization-url-diagnostics": (
        "tests/clean_room/test_spotify_boundaries.py::"
        "test_authorization_diagnostic_scan_positive_control"
    ),
    "insecure-credential-backend": (
        "tests/providers/test_keyring_store.py::test_every_non_exact_backend_fails_closed"
    ),
    "listener-left-open": (
        "tests/clean_room/test_spotify_boundaries.py::"
        "test_boundary_evidence_rejects_a_listener_left_open"
    ),
    "missing-attribution": (
        "tests/providers/spotify/test_attribution.py::"
        "test_attribution_gate_rejects_an_incomplete_notice"
    ),
    "mutation-surface": (
        "tests/providers/spotify/test_surface.py::"
        "test_private_async_mutation_callable_is_prohibited"
    ),
    "raw-response-escape": (
        "tests/providers/spotify/test_surface.py::"
        "test_exported_class_rejects_module_level_public_attribute_injection"
    ),
    "redirect-following": (
        "tests/security/test_spotify_egress.py::"
        "test_redirect_location_never_reaches_a_second_connection"
    ),
    "token-written-to-file": (
        "tests/clean_room/test_spotify_boundaries.py::test_sensitive_scan_positive_controls"
    ),
    "unapproved-egress": (
        "tests/clean_room/test_spotify_boundaries.py::"
        "test_unapproved_scripted_host_is_recorded_before_rejection"
    ),
    "mcp-protocol-stdout": (
        "tests/mcp/test_protocol_compatibility.py::"
        "test_mcp_protocol_positive_control_rejects_non_protocol_stdout"
    ),
}
REQUIRED_WHEELS = frozenset(
    {
        "build",
        "hatchling",
        "httpx",
        "keyring",
        "mcp",
        "mcp-types",
        "mypy",
        "pytest",
        "pytest-cov",
        "ruff",
    }
)
_MCP_PROTOCOL_GATE = """
import json
from pathlib import Path
import selectors
import subprocess
import sys

entry_point = Path(sys.argv[1])
server_program = '''
import runpy
import sys

from music_friend.mcp import create_read_server
from music_friend.providers import Capability, ProviderCapabilities
import music_friend.runtimes.mcp_stdio as runtime


class Source:
    def capabilities(self):
        return ProviderCapabilities(
            supported=frozenset(Capability), granted=frozenset(Capability)
        )


def synthetic_session(**_kwargs):
    create_read_server(Source()).run("stdio")


runtime.run_stdio_session = synthetic_session
runpy.run_path(sys.argv[1], run_name="__main__")
'''
process = subprocess.Popen(
    [sys.executable, "-I", "-c", server_program, str(entry_point)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)


def send(request):
    assert process.stdin is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")) + chr(10))
    process.stdin.flush()


def receive():
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=5):
            raise RuntimeError("MCP protocol response timed out")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("MCP protocol server closed before responding")
    return line


send(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "clean-room", "version": "1"},
        },
    }
)
initialize = receive()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
tools = receive()
send(
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "music_capabilities", "arguments": {}},
    }
)
called = receive()
assert process.stdin is not None
process.stdin.close()
process.wait(timeout=5)
assert process.stdout is not None and process.stderr is not None
stdout = initialize + tools + called + process.stdout.read()
stderr = process.stderr.read()
messages = []
for line in stdout.splitlines():
    message = json.loads(line)
    assert isinstance(message, dict) and message.get("jsonrpc") == "2.0"
    messages.append(message)
assert process.returncode == 0
assert stderr == ""
assert [message.get("id") for message in messages if "id" in message] == [1, 2, 3]
assert messages[1]["result"]["tools"][0]["name"] == "music_capabilities"
assert messages[2]["result"]["structuredContent"]["effective"] == [
    "followed_artists",
    "health",
    "recent_releases",
    "saved_items",
    "search_artists",
    "top_items",
]

failure_program = '''
import runpy
import sys

import music_friend.runtimes.mcp_stdio as runtime


def failing_session(**_kwargs):
    raise RuntimeError(sys.argv[2])


runtime.run_stdio_session = failing_session
runpy.run_path(sys.argv[1], run_name="__main__")
'''
for canary in (
    "synthetic-credential-canary",
    "synthetic-provider-response-canary",
    "/synthetic/absolute-path-canary",
):
    failed = subprocess.run(
        [sys.executable, "-I", "-c", failure_program, str(entry_point), canary],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert failed.stderr == "Music Friend MCP could not start." + chr(10)
    assert canary not in failed.stdout + failed.stderr
"""
_CREDENTIAL_PATTERN = re.compile(
    r"(?ix)(?:"
    r"(?<![a-z0-9_'\"-])(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"(?![a-z0-9_-])\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{6,}"
    r"|['\"](?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password)['\"]"
    r"\s*:\s*(?:['\"][A-Za-z0-9._~+/=-]{6,}['\"]|[A-Za-z0-9._~+/=-]{6,})"
    r")"
)
_AUTHORIZATION_URL_PATTERN = re.compile(
    r"https://accounts\.spotify\.com/authorize\?[^\s'\"]*(?:client_id|state)="
)
_CREDENTIAL_BYTES_PATTERN = re.compile(
    _CREDENTIAL_PATTERN.pattern.encode("ascii"), _CREDENTIAL_PATTERN.flags & ~re.UNICODE
)
_AUTHORIZATION_URL_BYTES_PATTERN = re.compile(
    _AUTHORIZATION_URL_PATTERN.pattern.encode("ascii"),
    _AUTHORIZATION_URL_PATTERN.flags & ~re.UNICODE,
)
_REQUIRED_NONEMPTY_SCANS = frozenset({"catalog", "credential_fake"})


class CertificationFailure(RuntimeError):
    """A bounded certification-stage failure."""


@dataclass
class ReservedResult:
    path: Path
    parent_fd: int
    result_fd: int
    parent_identity: tuple[int, int]
    result_identity: tuple[int, int]
    ancestor_identities: tuple[tuple[int, int], ...]
    checkout: Path | None


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _open_absolute_directory(path: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    if not path.is_absolute():
        raise ValueError("result parent must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", flags)
    identities = [_identity(os.fstat(descriptor))]
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            identities.append(_identity(os.fstat(descriptor)))
        return descriptor, tuple(identities)
    except BaseException:
        os.close(descriptor)
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_digest(path: Path, expected: str) -> None:
    if sha256_file(path) != expected:
        raise ValueError("artifact digest mismatch")


def reserve_result(path: Path, *, checkout: Path | None = None) -> ReservedResult:
    if not path.is_absolute():
        raise ValueError("result target must be absolute")
    parent_fd, ancestor_identities = _open_absolute_directory(path.parent)
    parent_identity = _identity(os.fstat(parent_fd))
    resolved_checkout = checkout.resolve(strict=True) if checkout is not None else None
    try:
        named_parent = path.parent.resolve(strict=True)
        if _identity(os.stat(path.parent, follow_symlinks=False)) != parent_identity:
            raise ValueError("result parent changed during validation")
        if resolved_checkout is not None and (
            named_parent == resolved_checkout or named_parent.is_relative_to(resolved_checkout)
        ):
            raise ValueError("result must be outside checkout")
        verifying_fd, verifying_identities = _open_absolute_directory(path.parent)
        try:
            if (
                verifying_identities != ancestor_identities
                or _identity(os.fstat(verifying_fd)) != parent_identity
            ):
                raise ValueError("result parent changed during validation")
        finally:
            os.close(verifying_fd)
    except BaseException:
        os.close(parent_fd)
        raise
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError as error:
        os.close(parent_fd)
        raise ValueError("result target must be absent") from error
    except OSError as error:
        os.close(parent_fd)
        raise ValueError("result target must be absent and creatable") from error
    os.fchmod(descriptor, 0o600)
    return ReservedResult(
        path=path,
        parent_fd=parent_fd,
        result_fd=descriptor,
        parent_identity=parent_identity,
        result_identity=_identity(os.fstat(descriptor)),
        ancestor_identities=ancestor_identities,
        checkout=resolved_checkout,
    )


def _verify_result_location(reservation: ReservedResult) -> None:
    named_parent_fd = -1
    try:
        named_parent_fd, named_ancestors = _open_absolute_directory(reservation.path.parent)
        named_parent = reservation.path.parent.resolve(strict=True)
        parent = os.fstat(named_parent_fd)
        named_result = os.stat(
            reservation.path.name,
            dir_fd=named_parent_fd,
            follow_symlinks=False,
        )
        held_result = os.stat(
            reservation.path.name,
            dir_fd=reservation.parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError("result location changed") from error
    finally:
        if named_parent_fd >= 0:
            os.close(named_parent_fd)
    if (
        named_ancestors != reservation.ancestor_identities
        or not stat.S_ISDIR(parent.st_mode)
        or _identity(parent) != reservation.parent_identity
        or (
            reservation.checkout is not None
            and (
                named_parent == reservation.checkout
                or named_parent.is_relative_to(reservation.checkout)
            )
        )
        or _identity(named_result) != reservation.result_identity
        or _identity(held_result) != reservation.result_identity
        or not stat.S_ISREG(named_result.st_mode)
        or not stat.S_ISREG(held_result.st_mode)
    ):
        raise ValueError("result location changed")


def write_reserved_result(reservation: ReservedResult, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise ValueError("evidence exceeded size limit")
    offset = 0
    while offset < len(encoded):
        written = os.write(reservation.result_fd, encoded[offset:])
        if written <= 0:
            raise OSError("short evidence write")
        offset += written
    os.fsync(reservation.result_fd)
    _verify_result_location(reservation)
    os.close(reservation.result_fd)
    os.close(reservation.parent_fd)
    reservation.result_fd = -1
    reservation.parent_fd = -1


def _safe_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def scan_sensitive_root(root: Path) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    scanned = 0
    if not root.exists():
        return {"findings": [], "scanned_files": 0, "status": "pass"}
    for path in sorted(root.rglob("*")):
        relative = _safe_relative(path, root)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
        ):
            findings.append({"path": relative, "rule": "unsafe-filesystem-entry"})
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        scanned += 1
        if metadata.st_size > 16 * 1024 * 1024:
            findings.append({"path": relative, "rule": "oversized-file"})
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _CREDENTIAL_BYTES_PATTERN.search(data):
            findings.append({"path": relative, "rule": "credential-material"})
        if _AUTHORIZATION_URL_BYTES_PATTERN.search(data):
            findings.append({"path": relative, "rule": "complete-authorization-url"})
    findings.sort(key=lambda item: (item["path"], item["rule"]))
    return {
        "findings": findings,
        "scanned_files": scanned,
        "status": "fail" if findings else "pass",
    }


def _valid_observed_child(command: Mapping[str, Any]) -> bool:
    identifier = command["id"]
    argv = command["argv"]
    cwd = command["cwd"]
    fixed: dict[str, list[str]] = {
        "git-ls-source": ["git", "ls-files", "-z", "--", "src/music_friend"],
        "git-ls-all": ["git", "ls-files", "-z"],
        "git-source-head": ["git", "rev-parse", "HEAD"],
        "git-test-init": ["git", "init"],
        "git-test-add": ["git", "add", "clean-room-phase1.sh"],
        "git-test-commit": ["git", "commit", "-m", "test"],
        "git-test-head": ["git", "rev-parse", "HEAD"],
        "harness-empty": ["/bin/bash", "{source}/scripts/clean-room-phase1.sh"],
        "python-artifact-smoke": [
            "{python}",
            "-I",
            "-c",
            "{validated-artifact-smoke}",
        ],
        "spotify-harness": ["{python}", "{source}/scripts/clean-room-spotify.py"],
    }
    if identifier in fixed:
        source_cwd = identifier not in {
            "git-test-init",
            "git-test-add",
            "git-test-commit",
            "git-test-head",
            "python-artifact-smoke",
        }
        return argv == fixed[identifier] and (
            cwd == "{source}" if source_cwd else cwd.startswith("{write:pytest}/")
        )
    if identifier == "git-archive-head":
        return (
            len(argv) == 5
            and argv[:3] == ["git", "archive", "--format=tar"]
            and argv[3].startswith("--output={write:pytest}/")
            and argv[4] == "HEAD"
            and cwd == "{source}"
        )
    if identifier == "python-build-export":
        return (
            len(argv) == 7
            and argv[:5] == ["{python}", "-m", "build", "--no-isolation", "--outdir"]
            and argv[5].startswith("{write:pytest}/")
            and argv[5].endswith("/dist")
            and argv[6] == argv[5].removesuffix("/dist") + "/source"
            and cwd == "{source}"
        )
    if identifier == "python-build-source":
        return (
            len(argv) == 7
            and argv[:5] == ["{python}", "-m", "build", "--no-isolation", "--outdir"]
            and argv[5].startswith("{write:pytest}/")
            and argv[5].endswith("/independent-dist")
            and argv[6] == "{source}"
            and cwd == "{source}"
        )
    if identifier == "hatchling-build":
        return (
            len(argv) == 4
            and argv[:3] == ["{write:venv}/bin/hatchling", "build", "-d"]
            and argv[3].startswith("{write:build}/")
            and cwd == "{source}"
        )
    if identifier == "python-wheel-venv":
        return (
            len(argv) == 7
            and argv[:6] == ["{python}", "-I", "-m", "venv", "--copies", "--system-site-packages"]
            and argv[6].startswith("{write:pytest}/")
            and argv[6].endswith("/environment")
            and cwd.startswith("{write:pytest}/")
        )
    if identifier == "python-wheel-install":
        return (
            len(argv) == 9
            and argv[0].startswith("{write:pytest}/")
            and argv[0].endswith(("/environment/bin/python", "/environment/Scripts/python.exe"))
            and argv[1:8]
            == ["-I", "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall"]
            and argv[8].startswith("{write:build}/")
            and argv[8].endswith(".whl")
            and cwd.startswith("{write:pytest}/")
        )
    if identifier == "installed-wheel-skill":
        return (
            len(argv) == 5
            and argv[0].startswith("{write:pytest}/")
            and argv[0].endswith(
                ("/environment/bin/music-friend", "/environment/Scripts/music-friend.exe")
            )
            and argv[1:4] == ["skill", "install", "--target"]
            and argv[4].startswith("{write:pytest}/")
            and cwd.startswith("{write:pytest}/")
        )
    if identifier in {"scanner-source", "scanner-test"}:
        if len(argv) != 3:
            return False
        valid_target = (
            argv[2] == "{source}"
            if identifier == "scanner-source"
            else argv[2].startswith("{write:pytest}/")
        )
        return (
            argv[:2] == ["{python}", "{source}/scripts/scan_public_tree.py"]
            and valid_target
            and cwd == "{source}"
        )
    if identifier == "harness-certification":
        return (
            len(argv) == 10
            and argv[:3] == ["/bin/bash", "{source}/scripts/clean-room-phase1.sh", "--python"]
            and argv[3:6] == ["{phase1-python}", "--wheelhouse", "{phase1-wheelhouse}"]
            and argv[6] == "--result"
            and argv[7].startswith("{write:pytest}/")
            and argv[8] == "--commit"
            and re.fullmatch(r"[0-9a-f]{40}", argv[9]) is not None
            and cwd == "{source}"
        )
    if identifier == "exact":
        output_control = argv == [
            "{python}",
            "-c",
            "import sys;sys.stdout.write('x'*4096)",
        ]
        temporary_harness = (
            len(argv) == 10
            and cwd.startswith("{write:pytest}/")
            and argv[0] == "/bin/bash"
            and argv[1] == cwd + "/clean-room-phase1.sh"
            and argv[2:4] == ["--python", "{python}"]
            and argv[4] == "--wheelhouse"
            and argv[5].startswith("{write:pytest}/")
            and argv[6] == "--result"
            and argv[7].startswith("{write:pytest}/")
            and argv[8] == "--commit"
            and re.fullmatch(r"[0-9a-f]{40}", argv[9]) is not None
        )
        return output_control or temporary_harness
    return False


def validate_boundary_evidence(evidence: Mapping[str, Any]) -> None:
    if not isinstance(evidence, Mapping):
        raise ValueError("boundary schema mismatch")
    expected_keys = {
        "binds",
        "connects",
        "sends",
        "browser_handoffs",
        "http_attempts",
        "callback_binds",
        "callback_closes",
        "write_checks",
        "declared_writes",
        "child_commands",
        "status",
    }
    if set(evidence) != expected_keys:
        raise ValueError("boundary schema mismatch")
    if evidence.get("status") != "pass":
        raise ValueError("boundary suite did not pass")
    if any(
        type(evidence[name]) is not int or evidence[name] != 0
        for name in ("binds", "connects", "sends")
    ):
        raise ValueError("real network activity observed")
    browser_handoffs = evidence["browser_handoffs"]
    if not isinstance(browser_handoffs, list) or any(
        not isinstance(item, list) or item != ["accounts.spotify.com", "/authorize"]
        for item in browser_handoffs
    ):
        raise ValueError("unapproved browser handoff observed")
    http_attempts = evidence["http_attempts"]
    if not isinstance(http_attempts, list):
        raise ValueError("HTTP evidence shape mismatch")
    for item in http_attempts:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("HTTP evidence shape mismatch")
        host, scripted = item
        if host not in {"accounts.spotify.com", "api.spotify.com"} or scripted is not True:
            raise ValueError("unapproved HTTP attempt observed")
    binds = evidence["callback_binds"]
    closes = evidence["callback_closes"]
    if not isinstance(binds, list) or not isinstance(closes, list):
        raise ValueError("listener evidence shape mismatch")
    if len(binds) != len(closes):
        raise ValueError("listener lifecycle mismatch")
    if any(item != ["127.0.0.1", 0] for item in binds):
        raise ValueError("callback bind mismatch")
    if any(
        len(item) != 2
        or item[0] != "127.0.0.1"
        or not isinstance(item[1], int)
        or not 1 <= item[1] <= 65535
        for item in closes
    ):
        raise ValueError("callback close mismatch")
    writes = evidence["write_checks"]
    write_names = ("directory", "file", "link", "sqlite")
    if (
        not isinstance(writes, dict)
        or set(writes) != set(write_names)
        or any(type(writes[name]) is not int or writes[name] <= 0 for name in write_names)
    ):
        raise ValueError("write-check evidence mismatch")
    declared = evidence["declared_writes"]
    expected_writes = [
        "artifact-venv",
        "boundary-evidence",
        "build",
        "pytest",
        "temporary",
        "venv",
    ]
    if declared != expected_writes:
        raise ValueError("declared-write evidence mismatch")
    commands = evidence["child_commands"]
    if not isinstance(commands, list):
        raise ValueError("child-command evidence mismatch")
    for command in commands:
        if (
            not isinstance(command, dict)
            or set(command) != {"id", "argv", "cwd", "count"}
            or not isinstance(command["id"], str)
            or not isinstance(command["cwd"], str)
            or type(command["count"]) is not int
            or command["count"] <= 0
            or not isinstance(command["argv"], list)
            or not all(isinstance(value, str) for value in command["argv"])
            or not _valid_observed_child(command)
        ):
            raise ValueError("child-command evidence mismatch")
        if command["id"] not in {
            "exact",
            "git-archive-head",
            "git-ls-all",
            "git-ls-source",
            "git-source-head",
            "git-test-add",
            "git-test-commit",
            "git-test-head",
            "git-test-init",
            "harness-certification",
            "harness-empty",
            "hatchling-build",
            "installed-wheel-skill",
            "python-artifact-smoke",
            "python-build-export",
            "python-build-source",
            "python-wheel-install",
            "python-wheel-venv",
            "scanner-source",
            "scanner-test",
            "spotify-harness",
        } or not (command["cwd"] == "{source}" or command["cwd"].startswith("{write:")):
            raise ValueError("child-command evidence mismatch")
        serialized_command = json.dumps(command, sort_keys=True, separators=(",", ":"))
        if (
            _CREDENTIAL_PATTERN.search(serialized_command)
            or _AUTHORIZATION_URL_PATTERN.search(serialized_command)
            or "/Users/" in serialized_command
            or "/home/" in serialized_command
        ):
            raise ValueError("child-command evidence mismatch")


def _validated_scan_payload(scans: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    if not isinstance(scans, Mapping):
        raise ValueError("scan schema mismatch")
    if set(scans) != set(SCAN_NAMES):
        raise ValueError("scan schema mismatch")
    result: dict[str, object] = {}
    for name in SCAN_NAMES:
        value = scans[name]
        if (
            not isinstance(value, Mapping)
            or set(value) != {"scanned_files", "status"}
            or type(value["scanned_files"]) is not int
            or value["scanned_files"] < 0
            or (name in _REQUIRED_NONEMPTY_SCANS and value["scanned_files"] == 0)
            or value["status"] != "pass"
        ):
            raise ValueError("scan schema mismatch")
        result[name] = {"scanned_files": value["scanned_files"], "status": "pass"}
    return result


def _validated_boundary_payload(boundaries: Mapping[str, Any]) -> dict[str, object]:
    validate_boundary_evidence(boundaries)
    return {
        "binds": boundaries["binds"],
        "browser_handoffs": [list(item) for item in boundaries["browser_handoffs"]],
        "callback_binds": [list(item) for item in boundaries["callback_binds"]],
        "callback_closes": [list(item) for item in boundaries["callback_closes"]],
        "child_commands": [
            {
                "argv": list(command["argv"]),
                "count": command["count"],
                "cwd": command["cwd"],
                "id": command["id"],
            }
            for command in boundaries["child_commands"]
        ],
        "connects": boundaries["connects"],
        "declared_writes": list(boundaries["declared_writes"]),
        "http_attempts": [list(item) for item in boundaries["http_attempts"]],
        "sends": boundaries["sends"],
        "status": "pass",
        "write_checks": {
            name: boundaries["write_checks"][name]
            for name in ("directory", "file", "link", "sqlite")
        },
    }


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def build_evidence(
    *,
    commit: str,
    archive_sha256: str,
    interpreter_version: str,
    interpreter_sha256: str,
    wheelhouse_sha256: str,
    artifacts: Mapping[str, Mapping[str, str]],
    boundaries: Mapping[str, Any],
    scans: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("evidence commit mismatch")
    if (
        not isinstance(interpreter_version, str)
        or re.fullmatch(r"3\.10\.\d+", interpreter_version) is None
    ):
        raise ValueError("evidence interpreter mismatch")
    if not all(
        _valid_sha256(value) for value in (archive_sha256, interpreter_sha256, wheelhouse_sha256)
    ):
        raise ValueError("evidence digest mismatch")
    validated_boundaries = _validated_boundary_payload(boundaries)
    if not isinstance(artifacts, Mapping) or set(artifacts) != {"wheel", "sdist"}:
        raise ValueError("artifact schema mismatch")
    validated_artifacts: dict[str, object] = {}
    for kind in ("wheel", "sdist"):
        artifact = artifacts[kind]
        suffix = ".whl" if kind == "wheel" else ".tar.gz"
        if (
            not isinstance(artifact, Mapping)
            or set(artifact) != {"name", "sha256"}
            or not isinstance(artifact["name"], str)
            or Path(artifact["name"]).name != artifact["name"]
            or not artifact["name"].endswith(suffix)
            or not _valid_sha256(artifact["sha256"])
        ):
            raise ValueError("artifact schema mismatch")
        validated_artifacts[kind] = {"name": artifact["name"], "sha256": artifact["sha256"]}
    payload = {
        "archive_sha256": archive_sha256,
        "artifacts": validated_artifacts,
        "boundaries": validated_boundaries,
        "certification": "development-clean-room",
        "commands": list(COMMAND_IDS),
        "commit": commit,
        "interpreter": {"sha256": interpreter_sha256, "version": interpreter_version},
        "positive_controls": POSITIVE_CONTROL_NODES,
        "scans": _validated_scan_payload(scans),
        "status": "pass",
        "wheelhouse_sha256": wheelhouse_sha256,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if _CREDENTIAL_PATTERN.search(serialized) or _AUTHORIZATION_URL_PATTERN.search(serialized):
        raise ValueError("sensitive evidence payload")
    return payload


def _validate_absolute_input(path: Path, *, kind: str, directory: bool = False) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{kind} must be an absolute non-symlink path")
    if directory:
        valid = path.is_dir()
    else:
        valid = path.is_file() and os.access(path, os.X_OK)
    if not valid:
        raise ValueError(f"invalid {kind}")


def validate_result_path(path: Path, checkout: Path) -> None:
    if not path.is_absolute():
        raise ValueError("result must be absolute")
    parent_fd, _ = _open_absolute_directory(path.parent)
    try:
        parent_path = path.parent.resolve(strict=True)
        if parent_path == checkout or parent_path.is_relative_to(checkout):
            raise ValueError("result must be outside checkout")
    finally:
        os.close(parent_fd)


def _wheelhouse_digest(root: Path) -> str:
    entries = sorted(root.iterdir(), key=lambda path: path.name)
    if not entries or any(
        not path.is_file() or path.is_symlink() or path.suffix != ".whl" for path in entries
    ):
        raise ValueError("wheelhouse must contain only regular wheel files")
    normalized_names = {path.name.split("-", 1)[0].lower().replace("_", "-") for path in entries}
    missing = REQUIRED_WHEELS - normalized_names
    if missing:
        raise ValueError("missing required wheels")
    digest = hashlib.sha256()
    for path in entries:
        digest.update(path.name.encode() + b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _isolated_environment(roots: Mapping[str, Path], wheelhouse: Path) -> dict[str, str]:
    return {
        "COVERAGE_FILE": str(roots["build"] / ".coverage"),
        "HOME": str(roots["home"]),
        "LC_ALL": "C",
        "MF_SPOTIFY_NESTED": "1",
        "PATH": "/usr/bin:/bin",
        "PIP_CACHE_DIR": str(roots["cache"] / "pip"),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(roots["temporary"]),
        "XDG_CACHE_HOME": str(roots["cache"]),
        "XDG_CONFIG_HOME": str(roots["config"]),
    }


def reject_hostile_environment(environment: Mapping[str, str]) -> None:
    hostile = {
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
    }
    if hostile & set(environment):
        raise ValueError("hostile package-index configuration")


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log: Path,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    output_limit: int = MAX_CAPTURE_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    standard_output = bytearray()
    standard_error = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, standard_output)
            selector.register(process.stderr, selectors.EVENT_READ, standard_error)
            captured = 0
            with log.open("wb") as stream:
                while selector.get_map():
                    if time.monotonic() - started > timeout:
                        process.kill()
                        process.wait()
                        raise CertificationFailure("bounded command timed out")
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fd, min(65536, output_limit - captured + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if captured + len(chunk) > output_limit:
                            allowed = output_limit - captured
                            if allowed:
                                key.data.extend(chunk[:allowed])
                                stream.write(chunk[:allowed])
                            process.kill()
                            process.wait()
                            raise CertificationFailure("bounded command output exceeded limit")
                        key.data.extend(chunk)
                        stream.write(chunk)
                        captured += len(chunk)
                returncode = process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    completed = subprocess.CompletedProcess(
        list(argv), returncode, bytes(standard_output), bytes(standard_error)
    )
    if returncode != 0:
        raise CertificationFailure(f"command failed with exit {returncode}")
    return completed


def _scan_public(
    python: Path,
    scanner: Path,
    target: Path,
    *,
    environment: Mapping[str, str],
    log: Path,
) -> dict[str, object]:
    completed = _run(
        (str(python), "-I", str(scanner), str(target)),
        cwd=scanner.parents[1],
        environment=environment,
        log=log,
    )
    payload = json.loads(completed.stdout)
    if payload.get("status") != "pass" or not payload.get("scanned_files"):
        raise CertificationFailure("public scan did not inspect a clean payload")
    return {"scanned_files": payload["scanned_files"], "status": "pass"}


def _extract_archive(archive_path: Path, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not (member.isfile() or member.isdir())
            ):
                raise CertificationFailure("unsafe source archive member")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(root):
                raise CertificationFailure("unsafe source archive member")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise CertificationFailure("invalid source archive member")
                target.write_bytes(source.read())


def _git_environment(base: Mapping[str, str]) -> dict[str, str]:
    return {
        **base,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_AUTHOR_EMAIL": "clean-room@example.invalid",
        "GIT_AUTHOR_NAME": "Clean Room",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_EMAIL": "clean-room@example.invalid",
        "GIT_COMMITTER_NAME": "Clean Room",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _validate_checkout(root: Path, commit: str) -> None:
    root = root.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be a full lowercase SHA")
    git_env = _git_environment({"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    with tempfile.TemporaryDirectory(prefix="music-friend-checkout-validation.") as temporary:
        git_env["TMPDIR"] = temporary
        logs = Path(temporary)
        top = (
            _run(
                ("git", "rev-parse", "--show-toplevel"),
                cwd=root,
                environment=git_env,
                log=logs / "top.log",
                timeout=30,
            )
            .stdout.decode()
            .strip()
        )
        try:
            same_checkout = os.path.samefile(top, root)
        except OSError:
            same_checkout = False
        if not same_checkout:
            raise ValueError("run from repository root")
        status_output = _run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root,
            environment=git_env,
            log=logs / "status.log",
            timeout=30,
        ).stdout
        head = (
            _run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                environment=git_env,
                log=logs / "head.log",
                timeout=30,
            )
            .stdout.decode()
            .strip()
        )
    if status_output:
        raise ValueError("source checkout is not clean")
    if head != commit:
        raise ValueError("source commit is not exact HEAD")


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    return parser.parse_args(argv)


def certify(arguments: argparse.Namespace) -> None:
    repository_root = Path.cwd().resolve()
    reject_hostile_environment(os.environ)
    _validate_checkout(repository_root, arguments.source_commit)
    _validate_absolute_input(arguments.python, kind="Python interpreter")
    _validate_absolute_input(arguments.wheelhouse, kind="wheelhouse", directory=True)
    result_reservation = reserve_result(arguments.result, checkout=repository_root)
    isolated_root: Path | None = None
    stage = "initialize"
    try:
        os.umask(0o077)
        isolated_root = Path(tempfile.mkdtemp(prefix="music-friend-spotify-clean-room."))
        roots = {
            name: isolated_root / name
            for name in (
                "home",
                "config",
                "cache",
                "temporary",
                "build",
                "pytest",
                "logs",
                "working",
                "catalog",
                "credential_fake",
                "artifact_venv",
                "wheelhouse",
            )
        }
        roots["source"] = isolated_root / "source"
        roots["venv"] = isolated_root / "venv"
        for path in roots.values():
            path.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(roots["catalog"] / "catalog.sqlite3")) as catalog:
            with catalog:
                catalog.execute("CREATE TABLE certification (schema_version INTEGER NOT NULL)")
                catalog.execute("INSERT INTO certification VALUES (1)")
        (roots["credential_fake"] / "status.json").write_text(
            '{"connected":false}\n', encoding="utf-8"
        )
        archive = isolated_root / "source.tar"
        boundary_result = isolated_root / "boundaries.json"
        original_python_identity = _identity(arguments.python.stat(follow_symlinks=False))
        original_wheelhouse_identity = _identity(arguments.wheelhouse.stat(follow_symlinks=False))
        wheelhouse_sha256 = _wheelhouse_digest(arguments.wheelhouse)
        interpreter_sha256 = sha256_file(arguments.python)
        for wheel in sorted(arguments.wheelhouse.iterdir(), key=lambda path: path.name):
            copied = roots["wheelhouse"] / wheel.name
            shutil.copyfile(wheel, copied, follow_symlinks=False)
            if sha256_file(copied) != sha256_file(wheel):
                raise CertificationFailure("wheel copy digest mismatch")
        copied_wheelhouse_sha256 = _wheelhouse_digest(roots["wheelhouse"])
        if copied_wheelhouse_sha256 != wheelhouse_sha256:
            raise CertificationFailure("wheelhouse copy digest mismatch")
        environment = _isolated_environment(roots, roots["wheelhouse"])
        git_environment = _git_environment(environment)
        logs = roots["logs"]
        scanner = repository_root / "scripts" / "scan_public_tree.py"

        stage = "validate-toolchain"
        version = (
            _run(
                (
                    str(arguments.python),
                    "-I",
                    "-c",
                    "import platform;print(platform.python_version())",
                ),
                cwd=repository_root,
                environment=environment,
                log=logs / "python-version.log",
                timeout=30,
            )
            .stdout.decode()
            .strip()
        )
        if not version.startswith("3.10."):
            raise CertificationFailure("Python interpreter must be version 3.10")

        stage = "archive-source"
        _run(
            (
                "git",
                "archive",
                "--format=tar",
                f"--output={archive}",
                arguments.source_commit,
            ),
            cwd=repository_root,
            environment=git_environment,
            log=logs / "archive.log",
            timeout=60,
        )
        archive_sha256 = sha256_file(archive)
        scans: dict[str, dict[str, object]] = {}
        scans["archive"] = _scan_public(
            arguments.python,
            scanner,
            archive,
            environment=environment,
            log=logs / "archive-scan.log",
        )
        _extract_archive(archive, roots["source"])

        source_scanner = roots["source"] / "scripts" / "scan_public_tree.py"
        scans["export"] = _scan_public(
            arguments.python,
            source_scanner,
            roots["source"],
            environment=environment,
            log=logs / "export-scan.log",
        )

        stage = "create-environment"
        _run(
            (str(arguments.python), "-I", "-m", "venv", "--copies", str(roots["venv"])),
            cwd=roots["working"],
            environment=environment,
            log=logs / "venv.log",
        )
        venv_python = roots["venv"] / "bin" / "python"
        stage = "offline-install"
        _run(
            (
                str(venv_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(roots["wheelhouse"]),
                "hatchling",
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "install-build-backend.log",
        )
        _run(
            (
                str(venv_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(roots["wheelhouse"]),
                "--no-build-isolation",
                f"{roots['source']}[dev]",
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "install-project.log",
        )

        stage = "prepare-test-checkout"
        for index, command in enumerate(
            (
                ("git", "init", "-q"),
                ("git", "add", "-A"),
                ("git", "commit", "-q", "-m", "certification source"),
            )
        ):
            _run(
                command,
                cwd=roots["source"],
                environment=git_environment,
                log=logs / f"git-{index}.log",
                timeout=60,
            )

        write_roots = {
            "venv": str(roots["venv"]),
            "build": str(roots["build"]),
            "pytest": str(roots["pytest"]),
            "temporary": str(roots["temporary"]),
            "boundary-evidence": str(boundary_result),
            "artifact-venv": str(roots["artifact_venv"]),
        }
        test_environment = {
            **environment,
            "MF_CLEAN_ROOM_ACTIVE": "1",
            "MF_CLEAN_ROOM_BOUNDARY_RESULT": str(boundary_result),
            "MF_CLEAN_ROOM_CHILD_COMMANDS": json.dumps(
                [
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
                    "scanner-source",
                    "scanner-test",
                    "harness-certification",
                    "harness-empty",
                    "spotify-harness",
                ]
            ),
            "MF_CLEAN_ROOM_WRITE_ROOTS": json.dumps(write_roots),
            "MF_PHASE1_TEST_PYTHON": str(arguments.python),
            "MF_PHASE1_TEST_WHEELHOUSE": str(roots["wheelhouse"]),
            "PYTHONPATH": str(roots["source"] / "tests"),
        }

        stage = "positive-controls"
        _run(
            (
                str(venv_python),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "clean_room.boundaries",
                "--basetemp",
                str(roots["pytest"] / "positive"),
                "--log-file",
                str(roots["pytest"] / "positive.log"),
                *POSITIVE_CONTROL_NODES.values(),
            ),
            cwd=roots["source"],
            environment=test_environment,
            log=logs / "positive-controls.log",
        )

        stage = "pytest-with-coverage"
        _run(
            (
                str(venv_python),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "clean_room.boundaries",
                "--basetemp",
                str(roots["pytest"] / "full"),
                "--log-file",
                str(roots["pytest"] / "full.log"),
                "--cov=music_friend",
                "--cov-report=term-missing",
                "--cov-fail-under=90",
            ),
            cwd=roots["source"],
            environment=test_environment,
            log=logs / "pytest.log",
        )
        boundaries = json.loads(boundary_result.read_text(encoding="utf-8"))
        validate_boundary_evidence(boundaries)

        stage = "typecheck"
        _run(
            (
                str(venv_python),
                "-I",
                "-m",
                "mypy",
                "src/music_friend",
                "--cache-dir",
                str(roots["cache"] / "mypy"),
            ),
            cwd=roots["source"],
            environment=environment,
            log=logs / "mypy.log",
        )
        stage = "ruff-check"
        _run(
            (
                str(venv_python),
                "-I",
                "-m",
                "ruff",
                "check",
                "src",
                "tests",
                "scripts",
                "--select",
                "E4,E7,E9,F",
                "--no-cache",
            ),
            cwd=roots["source"],
            environment=environment,
            log=logs / "ruff-check.log",
        )
        stage = "ruff-format"
        _run(
            (str(venv_python), "-I", "-m", "ruff", "format", "--check", "src", "tests", "scripts"),
            cwd=roots["source"],
            environment=environment,
            log=logs / "ruff-format.log",
        )

        stage = "build-artifacts"
        _run(
            (
                str(venv_python),
                "-I",
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(roots["build"]),
                str(roots["source"]),
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "build.log",
        )
        wheels = sorted(roots["build"].glob("*.whl"))
        sdists = sorted(roots["build"].glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise CertificationFailure("build must produce one wheel and one sdist")
        wheel, sdist = wheels[0], sdists[0]
        artifact_digests = {"wheel": sha256_file(wheel), "sdist": sha256_file(sdist)}
        for artifact, digest in (
            (wheel, artifact_digests["wheel"]),
            (sdist, artifact_digests["sdist"]),
        ):
            require_digest(artifact, digest)
        scans["wheel"] = _scan_public(
            venv_python,
            source_scanner,
            wheel,
            environment=environment,
            log=logs / "wheel-scan.log",
        )
        scans["sdist"] = _scan_public(
            venv_python,
            source_scanner,
            sdist,
            environment=environment,
            log=logs / "sdist-scan.log",
        )

        stage = "artifact-install"
        _run(
            (
                str(arguments.python),
                "-I",
                "-m",
                "venv",
                "--copies",
                str(roots["artifact_venv"]),
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "artifact-venv.log",
        )
        artifact_python = roots["artifact_venv"] / "bin" / "python"
        _run(
            (
                str(artifact_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(roots["wheelhouse"]),
                str(wheel),
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "artifact-install.log",
        )
        require_digest(wheel, artifact_digests["wheel"])
        stage = "artifact-import-smoke"
        _run(
            (
                str(artifact_python),
                "-I",
                "-c",
                "import music_friend; from music_friend.providers.spotify import SpotifySource; assert music_friend.__name__ == 'music_friend'; assert SpotifySource.__name__ == 'SpotifySource'",
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "artifact-smoke.log",
            timeout=60,
        )

        stage = "mcp-protocol-gate"
        artifact_console = artifact_python.parent / "music-friend-mcp"
        if not artifact_console.is_file() or artifact_console.is_symlink():
            raise CertificationFailure("artifact console script is missing")
        _run(
            (
                str(artifact_python),
                "-I",
                "-c",
                _MCP_PROTOCOL_GATE,
                str(artifact_console),
            ),
            cwd=roots["working"],
            environment=environment,
            log=logs / "mcp-protocol-gate.log",
            timeout=60,
        )

        stage = "scan-isolated-state"
        sensitive_roots = {
            "home": roots["home"],
            "config": roots["config"],
            "cache": roots["cache"],
            "temporary": roots["temporary"],
            "working": roots["working"],
            "catalog": roots["catalog"],
            "logs": roots["logs"],
            "credential_fake": roots["credential_fake"],
        }
        for name, root in sensitive_roots.items():
            scan = scan_sensitive_root(root)
            if scan["status"] != "pass":
                raise CertificationFailure(f"isolated {name} scan failed")
            scans[name] = {"scanned_files": scan["scanned_files"], "status": "pass"}
        if set(scans) != set(SCAN_NAMES):
            raise CertificationFailure("scan coverage mismatch")

        if (
            _identity(arguments.python.stat(follow_symlinks=False)) != original_python_identity
            or sha256_file(arguments.python) != interpreter_sha256
        ):
            raise CertificationFailure("interpreter identity changed")
        if (
            _identity(arguments.wheelhouse.stat(follow_symlinks=False))
            != original_wheelhouse_identity
            or _wheelhouse_digest(arguments.wheelhouse) != wheelhouse_sha256
            or _wheelhouse_digest(roots["wheelhouse"]) != wheelhouse_sha256
        ):
            raise CertificationFailure("wheelhouse identity changed")

        stage = "write-evidence"
        evidence = build_evidence(
            commit=arguments.source_commit,
            archive_sha256=archive_sha256,
            interpreter_version=version,
            interpreter_sha256=interpreter_sha256,
            wheelhouse_sha256=wheelhouse_sha256,
            artifacts={
                "wheel": {"name": wheel.name, "sha256": artifact_digests["wheel"]},
                "sdist": {"name": sdist.name, "sha256": artifact_digests["sdist"]},
            },
            boundaries=boundaries,
            scans=scans,
        )
        write_reserved_result(result_reservation, evidence)
        shutil.rmtree(isolated_root)
    except BaseException:
        if result_reservation.result_fd >= 0:
            failure = {
                "certification": "development-clean-room",
                "commit": arguments.source_commit,
                "failed_stage": stage,
                "isolated_root": str(isolated_root) if isolated_root else None,
                "status": "fail",
            }
            try:
                write_reserved_result(result_reservation, failure)
            except BaseException:
                os.close(result_reservation.result_fd)
                os.close(result_reservation.parent_fd)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    try:
        certify(_arguments(argv))
    except (CertificationFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        rendered = str(error)
        if _CREDENTIAL_PATTERN.search(rendered) or _AUTHORIZATION_URL_PATTERN.search(rendered):
            rendered = "redacted failure"
        print(f"clean-room certification failed: {rendered}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
