from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from .boundaries import BoundaryPolicy

REPOSITORY_ROOT = Path(__file__).parents[2]
HARNESS = REPOSITORY_ROOT / "scripts" / "clean-room-phase1.sh"
SCANNER = REPOSITORY_ROOT / "scripts" / "scan_public_tree.py"


def test_harness_requires_explicit_certification_inputs() -> None:
    result = subprocess.run(
        [str(Path("/bin/bash").resolve()), str(HARNESS)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--python" in result.stderr
    assert "--wheelhouse" in result.stderr
    assert "--result" in result.stderr
    assert "--commit" in result.stderr


def test_harness_success_is_offline_isolated_bounded_and_digest_bound(tmp_path: Path) -> None:
    python = os.environ.get("MF_PHASE1_TEST_PYTHON")
    wheelhouse = os.environ.get("MF_PHASE1_TEST_WHEELHOUSE")
    if not python or not wheelhouse or os.environ.get("MF_PHASE1_NESTED") == "1":
        pytest.skip("enabled by the outer clean-room certification")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result_path = tmp_path / "success.json"
    nested_temporary_parent = tmp_path / "nested-temporary"
    nested_temporary_parent.mkdir()
    environment = {
        **os.environ,
        "MF_PHASE1_NESTED": "1",
        "TMPDIR": str(nested_temporary_parent),
    }

    completed = subprocess.run(
        [
            str(Path("/bin/bash").resolve()),
            str(HARNESS),
            "--python",
            python,
            "--wheelhouse",
            wheelhouse,
            "--result",
            str(result_path),
            "--commit",
            commit,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert list(nested_temporary_parent.glob("music-friend-clean-room.*")) == []
    platform_cache = nested_temporary_parent / "xcrun_db"
    if platform_cache.exists():
        platform_cache.unlink()
    assert list(nested_temporary_parent.iterdir()) == []
    evidence = json.loads(result_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "pass"
    assert evidence["commit"] == commit
    independent_archive = tmp_path / "independent-source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={independent_archive}", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    assert (
        evidence["archive_sha256"] == hashlib.sha256(independent_archive.read_bytes()).hexdigest()
    )
    wheelhouse_hash = hashlib.sha256()
    wheelhouse_entries = sorted(Path(wheelhouse).iterdir(), key=lambda path: path.name)
    assert wheelhouse_entries
    for entry in wheelhouse_entries:
        assert entry.is_file() and not entry.is_symlink() and entry.suffix == ".whl"
        wheelhouse_hash.update(entry.name.encode() + b"\0")
        wheelhouse_hash.update(hashlib.sha256(entry.read_bytes()).digest())
    assert evidence["wheelhouse_sha256"] == wheelhouse_hash.hexdigest()
    assert (
        evidence["interpreter"]["sha256"] == hashlib.sha256(Path(python).read_bytes()).hexdigest()
    )

    independent_dist = tmp_path / "independent-dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(independent_dist),
            str(REPOSITORY_ROOT),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(independent_dist.glob("*.whl"))
    source_distributions = list(independent_dist.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(source_distributions) == 1
    independent_artifacts = {
        artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()
        for artifact in (*wheels, *source_distributions)
    }
    assert {item["name"]: item["sha256"] for item in evidence["artifacts"]} == independent_artifacts
    assert evidence["boundaries"]["binds"] == 0
    assert evidence["boundaries"]["connects"] == 0
    assert evidence["boundaries"]["sends"] == 0
    assert evidence["boundaries"]["declared_writes"] == sorted(
        ["boundary-evidence", "build", "pytest", "temporary", "venv"]
    )
    assert result_path.stat().st_mode & 0o777 == 0o600
    assert result_path.stat().st_size <= 16384
    assert "music-friend-clean-room." not in result_path.read_text(encoding="utf-8")


def test_harness_places_archive_scan_before_extraction_and_offline_install() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert source.index("archive-scan.json") < source.index('tar -xf "$archive_path"')
    assert '--no-index --find-links "$wheelhouse"' in source
    assert "PIP_CONFIG_FILE=/dev/null" in source
    assert "env -i" in source
    assert "DEVELOPER_DIR" in source


def test_harness_enforces_installed_package_coverage_and_bounded_evidence() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    configuration = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.coverage.run]" in configuration
    assert 'source = ["music_friend"]' in configuration
    assert "[tool.coverage.report]" in configuration
    assert "fail_under = 95" in configuration
    assert "precision = 2" in configuration
    assert "show_missing = true" in configuration
    assert "--cov=music_friend" in source
    assert "--cov-fail-under=95" in source
    assert '"coverage"' in source
    assert '"statements"' in source
    assert '"covered"' in source
    assert '"missed"' in source
    assert '"threshold"' in source
    assert '"isolated_root"' not in source
    failure_writer = source.split("write_failure() {", 1)[1].split("\n}", 1)[0]
    assert "coverage.json" in failure_writer
    assert '"coverage"' in failure_writer


def test_harness_proves_package_import_resolves_under_venv_site_packages() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert "site.getsitepackages()" in source
    assert "music_friend.__file__" in source
    assert "package import did not resolve beneath isolated site-packages" in source


@pytest.mark.parametrize("target_kind", ("inside-checkout", "directory", "symlink", "fifo"))
def test_harness_refuses_unsafe_result_targets(
    tmp_path: Path,
    target_kind: str,
    boundary_policy: BoundaryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copy2(HARNESS, repository / HARNESS.name)
    git_environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    for command in (["git", "init"], ["git", "add", HARNESS.name], ["git", "commit", "-m", "test"]):
        subprocess.run(
            command, cwd=repository, env=git_environment, check=True, capture_output=True
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    target = (
        repository / "result.json" if target_kind == "inside-checkout" else tmp_path / "result.json"
    )
    if target_kind == "directory":
        target.mkdir()
    elif target_kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif target_kind == "fifo":
        os.mkfifo(target)

    command = (
        str(Path("/bin/bash").resolve()),
        str(repository / HARNESS.name),
        "--python",
        str(Path(sys.executable).resolve()),
        "--wheelhouse",
        str(wheelhouse),
        "--result",
        str(target),
        "--commit",
        commit,
    )
    monkeypatch.setattr(
        boundary_policy,
        "allowed_children",
        boundary_policy.allowed_children
        + (command[:7] + (str(target.resolve(strict=False)),) + command[8:],),
    )
    completed = subprocess.run(
        command,
        cwd=repository,
        env=git_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "invalid result target" in completed.stderr


def test_harness_preserves_failure_exit_status_and_writes_local_diagnostics(
    tmp_path: Path,
    boundary_policy: BoundaryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copy2(HARNESS, repository / HARNESS.name)
    git_environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "TMPDIR": str(tmp_path),
    }
    for command in (["git", "init"], ["git", "add", HARNESS.name], ["git", "commit", "-m", "test"]):
        subprocess.run(
            command, cwd=repository, env=git_environment, check=True, capture_output=True
        )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        env=git_environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wheelhouse = tmp_path / "empty-wheelhouse"
    wheelhouse.mkdir()
    result_path = tmp_path / "failure.json"
    command = (
        str(Path("/bin/bash").resolve()),
        str(repository / HARNESS.name),
        "--python",
        str(Path(sys.executable).resolve()),
        "--wheelhouse",
        str(wheelhouse),
        "--result",
        str(result_path),
        "--commit",
        commit,
    )
    monkeypatch.setattr(
        boundary_policy,
        "allowed_children",
        boundary_policy.allowed_children + (command,),
    )

    result = subprocess.run(
        command,
        cwd=repository,
        env=git_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    evidence = json.loads(result_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "fail"
    assert evidence["failed_stage"] == "validate-wheelhouse"
    assert "isolated_root" not in evidence
    assert list(tmp_path.glob("music-friend-clean-room.*"))


def test_built_artifacts_match_tracked_source_payload(tmp_path: Path) -> None:
    source_archive = tmp_path / "source.tar"
    source_root = tmp_path / "source"
    source_root.mkdir()
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={source_archive}", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    with tarfile.open(source_archive) as archive:
        for member in archive.getmembers():
            destination = source_root / member.name
            assert destination.resolve().is_relative_to(source_root.resolve())
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            assert member.isfile()
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            assert extracted is not None
            destination.write_bytes(extracted.read())
    output = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(output),
            str(source_root),
        ],
        check=True,
        capture_output=True,
    )
    wheel = next(output.glob("*.whl"))
    source_distribution = next(output.glob("*.tar.gz"))
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    expected_sdist = {
        item.decode("utf-8") for item in tracked if item and not item.startswith(b"tests/")
    }

    with tarfile.open(source_distribution, "r:gz") as archive:
        members = archive.getmembers()
        regular = {member.name.split("/", 1)[1] for member in members if member.isfile()}
        assert expected_sdist <= regular
        assert not any(name.startswith("tests/") for name in regular)
        pyproject = next(member for member in members if member.name.endswith("/pyproject.toml"))
        extracted = archive.extractfile(pyproject)
        assert extracted is not None
        assert extracted.read() == (source_root / "pyproject.toml").read_bytes()

    expected_wheel: dict[str, bytes] = {}
    for raw in tracked:
        if not raw.startswith(b"src/music_friend/"):
            continue
        relative = raw.decode("utf-8").removeprefix("src/")
        expected_wheel[relative] = (source_root / raw.decode("utf-8")).read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        for name, expected in expected_wheel.items():
            assert archive.read(name) == expected

    assert not _release_artifacts_include_planning(source_distribution, wheel)

    for artifact in (wheel, source_distribution):
        scan = subprocess.run(
            [sys.executable, str(SCANNER), str(artifact)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert scan.returncode == 0, scan.stdout
        assert json.loads(scan.stdout)["scanned_files"] > 0


def _release_artifacts_include_planning(source_distribution: Path, wheel: Path) -> bool:
    with tarfile.open(source_distribution, "r:gz") as archive:
        source_members = {member.name for member in archive.getmembers() if member.isfile()}
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = set(archive.namelist())
    return any("docs/planning/" in name for name in (*source_members, *wheel_members))


def test_scanner_rejects_unsafe_tar_member_without_extracting(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    payload = b"safe"
    member = tarfile.TarInfo("../outside.txt")
    member.size = len(payload)
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    result = subprocess.run(
        [sys.executable, str(SCANNER), str(archive_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        {"path": "unsafe.tar.gz!unsafe-member-0", "rule": "unsafe-archive-member"}
    ]
    assert "../outside.txt" not in result.stdout
    assert not (tmp_path.parent / "outside.txt").exists()
