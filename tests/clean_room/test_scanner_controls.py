from __future__ import annotations

import json
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SCANNER = REPOSITORY_ROOT / "scripts" / "scan_public_tree.py"
CANARY = "MF_TEST_SECRET_" + "DO_NOT_PUBLISH_7d2a9c"


def _scan(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("relative_path", "content", "expected_rule"),
    (
        ("credentials.txt", "client_" + "secret=not-a-real-value", "credential-assignment"),
        (
            "spotify.txt",
            "SPOTIFY_CLIENT_" + "SECRET=not-a-real-provider-value",
            "credential-assignment",
        ),
        ("openai.txt", "OPENAI_API_" + "KEY=not-a-real-provider-value", "credential-assignment"),
        (
            "aws.txt",
            "AWS_ACCESS_KEY_" + "ID=not-a-real-provider-value",
            "credential-assignment",
        ),
        (
            "authorization.txt",
            "Authori" + "zation: Bearer not-a-real-bearer-value",
            "bearer-authorization",
        ),
        ("private.pem", "-----BEGIN PRIVATE " + "KEY-----", "private-key-header"),
        ("canary.txt", CANARY, "credential-canary"),
        ("home.txt", "/" + "home/" + "sample-user/project/file.py", "unsafe" + "-path"),
        ("checkout.txt", "/opt/" + "Develop" + "ment/project/file.py", "unsafe" + "-path"),
        ("variation.txt", "plain\ufe0f", "unicode-variation-selector"),
        ("emoji.txt", "rocket \U0001f680", "emoji"),
        (".env", "SAFE_NAME=value", "prohibited-file"),
        ("__pycache__/module.pyc", "bytecode", "generated-content"),
        ("catalog.sqlite3", "not a database", "local-database"),
    ),
)
def test_scanner_reports_each_prohibited_class_without_echoing_content(
    tmp_path: Path,
    relative_path: str,
    content: str,
    expected_rule: str,
) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = _scan(tmp_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["scanned_files"] >= 1
    assert {"path": relative_path, "rule": expected_rule} in payload["findings"]
    assert str(tmp_path) not in result.stdout
    assert content not in result.stdout
    assert CANARY not in result.stdout


def test_scanner_accepts_synthetic_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("VALUE = 'music'\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("CLIENT_ID=\n", encoding="utf-8")

    result = _scan(tmp_path)

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == {
        "findings": [],
        "scanned_files": 2,
        "status": "pass",
    }


@pytest.mark.parametrize(
    "content",
    (
        '{"access_' + 'token":"synthetic-value"}',
        "{'refresh_" + "token': 'synthetic-value'}",
        '"client_' + 'secret": "synthetic-value"',
        '"access_' + 'token": synthetic-value',
        "'refresh_" + "token': synthetic-value",
        "password: synthetic-value",
        'serialized={"access_' + 'token":"synthetic-value"}',
    ),
    ids=(
        "json",
        "mapping",
        "json-fragment",
        "yaml-double-quoted-key",
        "yaml-single-quoted-key",
        "yaml",
        "serialized",
    ),
)
def test_scanner_rejects_credential_material_in_mapping_syntax(
    tmp_path: Path, content: str
) -> None:
    (tmp_path / "payload.txt").write_text(content, encoding="utf-8")

    result = _scan(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "payload.txt", "rule": "credential-assignment"}
    ]
    assert content not in result.stdout


@pytest.mark.parametrize(
    "content",
    (
        '{"access_' + 'token_count": 2}',
        '{"token_type":"Bearer"}',
        '"access_' + 'token_count": SYNTHETIC_VALUE',
        "refresh_" + "tokenizer = 'synthetic'",
        "password_policy: required",
    ),
)
def test_scanner_accepts_safe_credential_adjacent_text(tmp_path: Path, content: str) -> None:
    (tmp_path / "safe.txt").write_text(content, encoding="utf-8")

    result = _scan(tmp_path)

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "payload",
    (
        b"\xff\x00opaque access_" + b"token=synthetic-binary-value\x00",
        b"SQLite format 3\x00binary refresh_" + b"token=synthetic-sqlite-value\xff",
    ),
    ids=("non-utf8", "sqlite-shaped"),
)
def test_scanner_rejects_credential_markers_in_raw_binary_bytes(
    tmp_path: Path, payload: bytes
) -> None:
    (tmp_path / "opaque.bin").write_bytes(payload)

    result = _scan(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "opaque.bin", "rule": "credential-assignment"}
    ]
    assert "synthetic" not in result.stdout


def test_scanner_rejects_credential_in_a_sqlite_record_with_a_header_byte(tmp_path: Path) -> None:
    database = tmp_path / "opaque.bin"
    marker = "access_" + "token=synthetic-sqlite-value"
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute("CREATE TABLE records (payload TEXT NOT NULL)")
            connection.execute("INSERT INTO records (payload) VALUES (?)", (marker,))

    raw = database.read_bytes()
    marker_bytes = marker.encode("ascii")
    marker_offset = raw.rindex(marker_bytes)
    assert raw[marker_offset - 1] == 0x53

    result = _scan(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "opaque.bin", "rule": "credential-assignment"}
    ]
    assert "synthetic" not in result.stdout


def test_scanner_accepts_safe_binary_credential_adjacent_bytes(tmp_path: Path) -> None:
    (tmp_path / "opaque.bin").write_bytes(
        b"SQLite format 3\x00\x02Saccess_" + b"token_count=2\x00token_type=Bearer"
    )

    result = _scan(tmp_path)

    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["scanned_files"] == 1


def test_scanner_rejects_symlink_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-scanner.txt"
    outside.write_text(CANARY, encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)

    result = _scan(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "linked.txt", "rule": "unsafe-symlink"}
    ]
    assert CANARY not in result.stdout


def test_scanner_inspects_regular_archive_members(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "package.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr("package/module.py", CANARY)

    result = _scan(archive)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "package.whl!package/module.py", "rule": "credential-canary"}
    ]
    assert CANARY not in result.stdout


@pytest.mark.parametrize("member", ("../escape.py", "/absolute.py", "C:/drive.py"))
def test_scanner_rejects_unsafe_archive_member_paths(tmp_path: Path, member: str) -> None:
    import zipfile

    archive = tmp_path / "package.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(member, "safe")

    result = _scan(archive)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "package.whl!unsafe-member-0", "rule": "unsafe-archive-member"}
    ]
    assert member not in result.stdout


def test_scanner_rejects_archive_symlink_member(tmp_path: Path) -> None:
    import zipfile

    archive = tmp_path / "package.whl"
    member = zipfile.ZipInfo("package/link.py")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(member, "target.py")

    result = _scan(archive)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "package.whl!unsafe-member-0", "rule": "unsafe-archive-member"}
    ]


@pytest.mark.parametrize("file_type", (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR))
def test_scanner_rejects_every_nonregular_zip_unix_member(tmp_path: Path, file_type: int) -> None:
    import zipfile

    archive = tmp_path / "package.whl"
    member = zipfile.ZipInfo("package/special")
    member.create_system = 3
    member.external_attr = (file_type | 0o600) << 16
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(member, "opaque")

    result = _scan(archive)

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "package.whl!unsafe-member-0", "rule": "unsafe-archive-member"}
    ]
