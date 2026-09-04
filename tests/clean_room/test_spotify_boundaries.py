from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args, get_origin
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from music_friend.providers import Capability
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.callback import _CallbackOutcome
from music_friend.providers.spotify.config import SpotifySettings, load_spotify_settings
from music_friend.providers.spotify.oauth import (
    AuthorizationMode,
    AuthorizationResult,
    SpotifyAuthorization,
)
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport

from . import boundaries
from .boundaries import BoundaryPolicy, BoundaryViolation

REPOSITORY_ROOT = Path(__file__).parents[2]
PHASE_ONE_HARNESS = REPOSITORY_ROOT / "scripts" / "clean-room-phase1.sh"
SPOTIFY_HARNESS = REPOSITORY_ROOT / "scripts" / "clean-room-spotify.py"
EXPECTED_EVIDENCE_KEYS = {
    "archive_sha256",
    "artifacts",
    "boundaries",
    "certification",
    "commands",
    "commit",
    "interpreter",
    "positive_controls",
    "scans",
    "status",
    "wheelhouse_sha256",
}
DOCUMENTATION = REPOSITORY_ROOT / "docs" / "testing" / "spotify-adapter-clean-room.md"


def _load_harness():
    spec = importlib.util.spec_from_file_location("clean_room_spotify", SPOTIFY_HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_spotify_harness_requires_explicit_certification_inputs() -> None:
    result = subprocess.run(
        [sys.executable, str(SPOTIFY_HARNESS)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--source-commit" in result.stderr
    assert "--python" in result.stderr
    assert "--wheelhouse" in result.stderr
    assert "--result" in result.stderr


def test_spotify_harness_installs_build_backend_before_project_metadata() -> None:
    source = SPOTIFY_HARNESS.read_text(encoding="utf-8")

    assert source.index('logs / "install-build-backend.log"') < source.index(
        'logs / "install-project.log"'
    )


def test_spotify_harness_retains_pytest_logging_capture_for_redaction_controls() -> None:
    source = SPOTIFY_HARNESS.read_text(encoding="utf-8")

    assert '"no:logging"' not in source
    assert source.count('"--log-file"') == 2

    phase_one = PHASE_ONE_HARNESS.read_text(encoding="utf-8")
    assert "no:logging" not in phase_one
    assert '--log-file "$pytest_root/phase1.log"' in phase_one


def test_spotify_harness_uses_the_phase_one_stable_ruff_gate() -> None:
    source = SPOTIFY_HARNESS.read_text(encoding="utf-8")

    selector = source.index('"--select"')
    stable_rules = source.index('"E4,E7,E9,F"', selector)
    no_cache = source.index('"--no-cache"', stable_rules)
    assert selector < stable_rules < no_cache


def test_evidence_schema(tmp_path: Path) -> None:
    harness = _load_harness()
    evidence = harness.build_evidence(
        commit="a" * 40,
        archive_sha256="b" * 64,
        interpreter_version="3.10.20",
        interpreter_sha256="c" * 64,
        wheelhouse_sha256="d" * 64,
        artifacts={
            "wheel": {"name": "music_friend-0.1-py3-none-any.whl", "sha256": "e" * 64},
            "sdist": {"name": "music_friend-0.1.tar.gz", "sha256": "f" * 64},
        },
        boundaries={
            "binds": 0,
            "connects": 0,
            "sends": 0,
            "browser_handoffs": [["accounts.spotify.com", "/authorize"]],
            "http_attempts": [["accounts.spotify.com", True], ["api.spotify.com", True]],
            "callback_binds": [["127.0.0.1", 0]],
            "callback_closes": [["127.0.0.1", 49152]],
            "write_checks": {"directory": 1, "file": 1, "link": 1, "sqlite": 1},
            "declared_writes": [
                "artifact-venv",
                "boundary-evidence",
                "build",
                "pytest",
                "temporary",
                "venv",
            ],
            "child_commands": [
                {
                    "count": 1,
                    "id": "scanner-source",
                    "argv": [
                        "{python}",
                        "{source}/scripts/scan_public_tree.py",
                        "{source}",
                    ],
                    "cwd": "{source}",
                },
                {
                    "count": 1,
                    "id": "python-wheel-venv",
                    "argv": [
                        "{python}",
                        "-I",
                        "-m",
                        "venv",
                        "--copies",
                        "--system-site-packages",
                        "{write:pytest}/wheel/environment",
                    ],
                    "cwd": "{write:pytest}/wheel",
                },
                {
                    "count": 1,
                    "id": "python-wheel-install",
                    "argv": [
                        "{write:pytest}/wheel/environment/bin/python",
                        "-I",
                        "-m",
                        "pip",
                        "install",
                        "--no-index",
                        "--no-deps",
                        "--force-reinstall",
                        "{write:build}/wheel/music_friend-0.1.0-py3-none-any.whl",
                    ],
                    "cwd": "{write:pytest}/wheel",
                },
                {
                    "count": 1,
                    "id": "installed-wheel-skill",
                    "argv": [
                        "{write:pytest}/wheel/environment/bin/music-friend",
                        "skill",
                        "install",
                        "--target",
                        "{write:pytest}/wheel/skills",
                    ],
                    "cwd": "{write:pytest}/wheel",
                },
            ],
            "status": "pass",
        },
        scans={name: {"scanned_files": 1, "status": "pass"} for name in harness.SCAN_NAMES},
    )

    assert set(evidence) == EXPECTED_EVIDENCE_KEYS
    assert evidence["certification"] == "development-clean-room"
    assert evidence["status"] == "pass"
    assert set(evidence["artifacts"]) == {"wheel", "sdist"}
    assert evidence["commands"] == list(harness.COMMAND_IDS)
    assert evidence["positive_controls"] == harness.POSITIVE_CONTROL_NODES
    assert evidence["boundaries"]["write_checks"] == {
        "directory": 1,
        "file": 1,
        "link": 1,
        "sqlite": 1,
    }
    assert len(json.dumps(evidence, separators=(",", ":")).encode()) <= harness.MAX_EVIDENCE_BYTES
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    assert harness.scan_sensitive_root(tmp_path)["status"] == "pass"


def test_evidence_schema_records_the_fixed_mcp_protocol_gate() -> None:
    """A certification cannot pass while omitting the artifact protocol gate."""
    harness = _load_harness()

    assert "mcp-protocol-gate" in harness.COMMAND_IDS
    assert harness.POSITIVE_CONTROL_NODES["mcp-protocol-stdout"] == (
        "tests/mcp/test_protocol_compatibility.py::"
        "test_mcp_protocol_positive_control_rejects_non_protocol_stdout"
    )


def test_boundary_popen_guard_preserves_generic_subscriptability() -> None:
    """MCP imports annotate Popen and must not bypass the active child-process guard."""
    annotation = boundaries.GuardedPopen[bytes]

    assert get_origin(annotation) is boundaries._ORIGINAL_POPEN
    assert get_args(annotation) == (bytes,)


def test_boundary_evidence_rejects_a_listener_left_open() -> None:
    harness = _load_harness()
    evidence = {
        "binds": 0,
        "connects": 0,
        "sends": 0,
        "browser_handoffs": [],
        "http_attempts": [],
        "callback_binds": [["127.0.0.1", 0]],
        "callback_closes": [],
        "write_checks": {"directory": 1, "file": 1, "link": 1, "sqlite": 1},
        "declared_writes": [
            "artifact-venv",
            "boundary-evidence",
            "build",
            "pytest",
            "temporary",
            "venv",
        ],
        "child_commands": [],
        "status": "pass",
    }

    with pytest.raises(ValueError, match="listener lifecycle mismatch"):
        harness.validate_boundary_evidence(evidence)


def test_evidence_builder_rejects_wrong_top_level_and_nested_values() -> None:
    harness = _load_harness()
    boundary = {
        "binds": 0,
        "connects": 0,
        "sends": 0,
        "browser_handoffs": [],
        "http_attempts": [],
        "callback_binds": [],
        "callback_closes": [],
        "write_checks": {"directory": 1, "file": 1, "link": 1, "sqlite": 1},
        "declared_writes": [
            "artifact-venv",
            "boundary-evidence",
            "build",
            "pytest",
            "temporary",
            "venv",
        ],
        "child_commands": [],
        "status": "pass",
    }
    base = {
        "commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "interpreter_version": "3.10.20",
        "interpreter_sha256": "c" * 64,
        "wheelhouse_sha256": "d" * 64,
        "artifacts": {
            "wheel": {"name": "music_friend.whl", "sha256": "e" * 64},
            "sdist": {"name": "music_friend.tar.gz", "sha256": "f" * 64},
        },
        "boundaries": boundary,
        "scans": {name: {"scanned_files": 1, "status": "pass"} for name in harness.SCAN_NAMES},
    }
    probes = (
        {**base, "commit": 1},
        {**base, "archive_sha256": "short"},
        {**base, "interpreter_version": "3.11.0"},
        {
            **base,
            "artifacts": {
                **base["artifacts"],
                "wheel": {"name": "../music_friend.whl", "sha256": "e" * 64},
            },
        },
        {
            **base,
            "scans": {
                **base["scans"],
                "archive": {"scanned_files": 1, "status": "pass", "extra": True},
            },
        },
    )

    for probe in probes:
        with pytest.raises(ValueError):
            harness.build_evidence(**probe)


@pytest.mark.parametrize("name", ("catalog", "credential_fake"))
def test_required_isolated_scan_categories_reject_zero_counts(name: str) -> None:
    harness = _load_harness()
    scans = {scan_name: {"scanned_files": 1, "status": "pass"} for scan_name in harness.SCAN_NAMES}
    scans[name] = {"scanned_files": 0, "status": "pass"}

    with pytest.raises(ValueError, match="scan schema mismatch"):
        harness._validated_scan_payload(scans)


@pytest.mark.parametrize(
    ("name", "content", "rule"),
    (("credential.txt", "access_" + "token=synthetic-file-value", "credential-material"),),
)
def test_sensitive_scan_positive_controls(
    tmp_path: Path, name: str, content: str, rule: str
) -> None:
    harness = _load_harness()
    (tmp_path / name).write_text(content, encoding="utf-8")

    assert harness.scan_sensitive_root(tmp_path) == {
        "findings": [{"path": name, "rule": rule}],
        "scanned_files": 1,
        "status": "fail",
    }
    (tmp_path / name).unlink()
    assert harness.scan_sensitive_root(tmp_path)["status"] == "pass"


@pytest.mark.parametrize(
    "payload",
    (
        b"\xff\x00opaque access_" + b"token=synthetic-binary-value\x00",
        b"SQLite format 3\x00binary refresh_" + b"token=synthetic-sqlite-value\xff",
    ),
    ids=("non-utf8", "sqlite-shaped"),
)
def test_sensitive_scan_rejects_credential_markers_in_raw_binary_bytes(
    tmp_path: Path, payload: bytes
) -> None:
    harness = _load_harness()
    (tmp_path / "opaque.bin").write_bytes(payload)

    assert harness.scan_sensitive_root(tmp_path) == {
        "findings": [{"path": "opaque.bin", "rule": "credential-material"}],
        "scanned_files": 1,
        "status": "fail",
    }


def test_sensitive_scan_accepts_safe_binary_credential_adjacent_bytes(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    (tmp_path / "opaque.bin").write_bytes(
        b"SQLite format 3\x00\xffaccess_" + b"token_count=2\x00token_type=Bearer"
    )

    assert harness.scan_sensitive_root(tmp_path) == {
        "findings": [],
        "scanned_files": 1,
        "status": "pass",
    }


def test_authorization_diagnostic_scan_positive_control(tmp_path: Path) -> None:
    harness = _load_harness()
    diagnostic = tmp_path / "diagnostic.txt"
    diagnostic.write_text(
        "https://accounts.spotify.com/authorize?client_id=synthetic-client&state=synthetic",
        encoding="utf-8",
    )

    assert harness.scan_sensitive_root(tmp_path) == {
        "findings": [{"path": "diagnostic.txt", "rule": "complete-authorization-url"}],
        "scanned_files": 1,
        "status": "fail",
    }
    diagnostic.unlink()
    assert harness.scan_sensitive_root(tmp_path)["status"] == "pass"


@pytest.mark.parametrize(
    "content",
    (
        '{"access_' + 'token":"synthetic-value"}',
        "{'refresh_" + "token': 'synthetic-value'}",
        '"access_' + 'token": synthetic-value',
        "'refresh_" + "token': synthetic-value",
        "password: synthetic-value",
    ),
)
def test_isolated_scan_rejects_mapping_credential_material(tmp_path: Path, content: str) -> None:
    harness = _load_harness()
    (tmp_path / "payload.txt").write_text(content, encoding="utf-8")

    result = harness.scan_sensitive_root(tmp_path)

    assert result["status"] == "fail"
    assert result["findings"] == [{"path": "payload.txt", "rule": "credential-material"}]


@pytest.mark.parametrize(
    "content",
    (
        '{"access_' + 'token_count":2}',
        '"access_' + 'token_count": SYNTHETIC_VALUE',
        '{"token_type":"Bearer"}',
        "password_policy: required",
    ),
)
def test_isolated_scan_accepts_safe_credential_adjacent_text(tmp_path: Path, content: str) -> None:
    harness = _load_harness()
    (tmp_path / "safe.txt").write_text(content, encoding="utf-8")

    assert harness.scan_sensitive_root(tmp_path)["status"] == "pass"


def test_evidence_schema_rejects_recursive_extra_secret_and_wrong_types() -> None:
    harness = _load_harness()
    boundary = {
        "binds": 0,
        "connects": 0,
        "sends": 0,
        "browser_handoffs": [],
        "http_attempts": [],
        "callback_binds": [],
        "callback_closes": [],
        "write_checks": {"directory": 1, "file": 1, "link": 1, "sqlite": 1},
        "declared_writes": [
            "artifact-venv",
            "boundary-evidence",
            "build",
            "pytest",
            "temporary",
            "venv",
        ],
        "child_commands": [],
        "status": "pass",
    }
    probes = (
        {**boundary, "extra": {"access_" + "token": "synthetic-value"}},
        {**boundary, "binds": False},
        {**boundary, "browser_handoffs": [["unapproved.invalid", "/authorize"]]},
        {**boundary, "http_attempts": [["api.spotify.com", False]]},
        {**boundary, "write_checks": {"file": 0}},
        {
            **boundary,
            "write_checks": {
                "directory": 1,
                "file": 1,
                "link": 1,
                "sqlite": 1,
                "unexpected": 1,
            },
        },
        {
            **boundary,
            "write_checks": {
                "directory": 1,
                "file": 1,
                "link": 1,
                "sqlite": 1,
                "access_" + "token": 1,
            },
        },
        {**boundary, "child_commands": [{"id": "scanner", "argv": "wrong", "cwd": "source"}]},
        {
            **boundary,
            "child_commands": [
                {
                    "argv": ["unexpected"],
                    "count": 1,
                    "cwd": "{source}",
                    "id": "scanner-source",
                }
            ],
        },
    )

    for probe in probes:
        with pytest.raises(ValueError):
            harness.validate_boundary_evidence(probe)


def test_reserved_result_detects_final_component_swap(tmp_path: Path) -> None:
    harness = _load_harness()
    target = tmp_path / "result.json"
    reservation = harness.reserve_result(target)
    displaced = tmp_path / "displaced.json"
    target.rename(displaced)
    target.symlink_to(tmp_path / "replacement.json")

    with pytest.raises(ValueError, match="result location changed"):
        harness.write_reserved_result(reservation, {"status": "pass"})


def test_reserved_result_detects_parent_swap(tmp_path: Path) -> None:
    harness = _load_harness()
    parent = tmp_path / "results"
    parent.mkdir()
    reservation = harness.reserve_result(parent / "result.json")
    displaced = tmp_path / "displaced"
    parent.rename(displaced)
    parent.mkdir()

    with pytest.raises(ValueError, match="result location changed"):
        harness.write_reserved_result(reservation, {"status": "pass"})


def test_reserved_result_detects_higher_ancestor_relocation_into_checkout(
    tmp_path: Path,
) -> None:
    harness = _load_harness()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    parent = outside / "higher" / "results"
    parent.mkdir(parents=True)
    reservation = harness.reserve_result(parent / "result.json", checkout=checkout)
    displaced = tmp_path / "displaced-higher"
    (outside / "higher").rename(displaced)
    relocated = checkout / "relocated"
    displaced.rename(relocated)
    (outside / "higher").symlink_to(relocated)

    with pytest.raises(ValueError, match="result location changed"):
        harness.write_reserved_result(reservation, {"status": "pass"})


def test_runner_stops_at_hard_output_limit(
    tmp_path: Path, boundary_policy: BoundaryPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    command = (str(Path(sys.executable).resolve()), "-c", "import sys;sys.stdout.write('x'*4096)")
    monkeypatch.setattr(
        boundary_policy,
        "allowed_children",
        boundary_policy.allowed_children + (command,),
    )

    with pytest.raises(harness.CertificationFailure, match="output exceeded limit"):
        harness._run(
            command,
            cwd=tmp_path,
            environment=os.environ,
            log=tmp_path / "output.log",
            output_limit=1024,
        )

    assert (tmp_path / "output.log").stat().st_size <= 1024


def test_input_validation_rejects_symlinks_and_missing_wheels(tmp_path: Path) -> None:
    harness = _load_harness()
    executable = tmp_path / "python"
    executable.write_text("synthetic", encoding="utf-8")
    executable.chmod(0o700)
    link = tmp_path / "python-link"
    link.symlink_to(executable)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "pytest-1-py3-none-any.whl").write_bytes(b"synthetic")
    wheelhouse_link = tmp_path / "wheelhouse-link"
    wheelhouse_link.symlink_to(wheelhouse)

    with pytest.raises(ValueError, match="non-symlink"):
        harness._validate_absolute_input(link, kind="Python interpreter")
    with pytest.raises(ValueError, match="non-symlink"):
        harness._validate_absolute_input(wheelhouse_link, kind="wheelhouse", directory=True)
    with pytest.raises(ValueError, match="missing required wheels"):
        harness._wheelhouse_digest(wheelhouse)


def test_hostile_package_index_configuration_is_rejected() -> None:
    harness = _load_harness()

    with pytest.raises(ValueError, match="package-index configuration"):
        harness.reject_hostile_environment({"PIP_INDEX_URL": "https://example.invalid/simple"})


def test_result_path_rejects_checkout_and_symlink_ancestors(tmp_path: Path) -> None:
    harness = _load_harness()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with pytest.raises(ValueError, match="outside checkout"):
        harness.validate_result_path(checkout / "result.json", checkout)
    with pytest.raises(ValueError, match="outside checkout"):
        harness.reserve_result(checkout / "result.json", checkout=checkout)
    assert not (checkout / "result.json").exists()

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent)
    with pytest.raises(OSError):
        harness.validate_result_path(linked_parent / "result.json", checkout)


def test_checkout_validation_rejects_dirty_and_nonexact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
    }
    boundaries._ORIGINAL_POPEN(("git", "init", "-q"), cwd=repository, env=environment).wait()
    (repository / "tracked.txt").write_text("tracked", encoding="utf-8")
    boundaries._ORIGINAL_POPEN(("git", "add", "-A"), cwd=repository, env=environment).wait()
    boundaries._ORIGINAL_POPEN(
        ("git", "commit", "-q", "-m", "synthetic"), cwd=repository, env=environment
    ).wait()
    head_process = boundaries._ORIGINAL_POPEN(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    head, _ = head_process.communicate()
    assert head_process.returncode == 0
    monkeypatch.setattr(harness.subprocess, "Popen", boundaries._ORIGINAL_POPEN)

    with pytest.raises(ValueError, match="exact HEAD"):
        harness._validate_checkout(repository, "0" * 40)
    (repository / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="not clean"):
        harness._validate_checkout(repository, head.strip())


def test_artifact_digest_mismatch_positive_control(tmp_path: Path) -> None:
    harness = _load_harness()
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"exact artifact")

    with pytest.raises(ValueError, match="artifact digest mismatch"):
        harness.require_digest(artifact, "0" * 64)


def test_result_reservation_rejects_overwrite_and_symlink_races(tmp_path: Path) -> None:
    harness = _load_harness()
    result = tmp_path / "result.json"
    result.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="result target must be absent"):
        harness.reserve_result(result)

    result.unlink()
    result.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="result target must be absent"):
        harness.reserve_result(result)


def test_successful_result_is_created_once_with_mode_0600(tmp_path: Path) -> None:
    harness = _load_harness()
    result = tmp_path / "result.json"
    descriptor = harness.reserve_result(result)
    harness.write_reserved_result(descriptor, {"status": "pass"})

    assert json.loads(result.read_text(encoding="utf-8")) == {"status": "pass"}
    assert result.stat().st_mode & 0o777 == 0o600


def test_development_documentation_is_generic_tuple_bounded_and_non_release() -> None:
    document = DOCUMENTATION.read_text(encoding="utf-8")

    assert "development-clean-room" in document
    assert "macOS Keychain" in document
    assert "Windows credential storage is not verified by this command" in document
    assert "Linux credential storage is not verified by this command" in document
    assert "M2" not in document
    assert "Live certification and release certification are separate" in document
    assert "/absolute/path/to/python3.10" in document
    assert "/absolute/path/to/wheelhouse" in document
    assert "/absolute/path/to/result.json" in document
    for prohibited in (
        "/" + "Users" + "/",
        "/" + "home" + "/",
        "Develop" + "ment/",
        ".super" + "powers",
        "Task" + " 6",
        "MF" + "-104",
        "ag" + "ent",
        "re" + "view machinery",
        "publicly supported",
        "release approved",
        "live Spotify authorization completed",
    ):
        assert prohibited not in document


def test_documented_evidence_fields_match_executable_schema() -> None:
    harness = _load_harness()
    document = DOCUMENTATION.read_text(encoding="utf-8")
    documented = {
        line.removeprefix("- `").split("`", 1)[0]
        for line in document.splitlines()
        if line.startswith("- `")
    }

    assert documented == EXPECTED_EVIDENCE_KEYS
    assert set(harness.build_evidence.__annotations__) >= {
        "commit",
        "archive_sha256",
        "interpreter_version",
        "interpreter_sha256",
        "wheelhouse_sha256",
        "artifacts",
        "boundaries",
        "scans",
    }


_VALID_BROWSER_HANDOFF = (
    "https://accounts.spotify.com/authorize?client_id=synthetic-client&response_type=code"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A49152%2Fcallback&state=synthetic-state"
    "&scope=user-read-private&code_challenge_method=S256&code_challenge=synthetic-challenge"
)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


class ScriptedHTTP:
    def __init__(self, policy: BoundaryPolicy, responses: list[httpx.Response]) -> None:
        self._policy = policy
        self.responses = responses

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        assert host is not None
        self._policy.record_http_attempt(host, scripted=True)
        return self.responses.pop(0)


class RecordingBrowser:
    def __init__(self, policy: BoundaryPolicy) -> None:
        self._policy = policy
        self.target: str | None = None

    def __call__(self, url: str) -> bool:
        self._policy.record_browser_handoff(url)
        self.target = url
        return True


class LifecycleServer:
    def __init__(
        self,
        *,
        port: int,
        outcome: _CallbackOutcome,
        policy: BoundaryPolicy,
        raises: bool,
    ) -> None:
        assigned = 49152 if port == 0 else port
        self.server_address = ("127.0.0.1", assigned)
        self._outcome = outcome
        self.timeout: float | None = None
        self._policy = policy
        self._raises = raises
        self.handles = 0
        policy.record_callback_bind("127.0.0.1", port)

    def handle_request(self) -> None:
        self.handles += 1
        if self._raises:
            raise RuntimeError("synthetic listener failure")

    def server_close(self) -> None:
        host, port = self.server_address
        self._policy.record_callback_close(host, port)


class ServerFactory:
    def __init__(
        self,
        policy: BoundaryPolicy,
        outcome: _CallbackOutcome,
        *,
        raises: bool = False,
    ) -> None:
        self.policy = policy
        self.outcome = outcome
        self.raises = raises
        self.servers: list[LifecycleServer] = []

    def __call__(self, port: int, *, expected_state: str) -> LifecycleServer:
        assert expected_state
        server = LifecycleServer(
            port=port,
            outcome=self.outcome,
            policy=self.policy,
            raises=self.raises,
        )
        self.servers.append(server)
        return server


def _deterministic_bytes(*values: bytes) -> Callable[[int], bytes]:
    remaining = iter(values)

    def read(length: int) -> bytes:
        value = next(remaining)
        assert len(value) == length
        return value

    return read


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_" + "token": "synthetic-access-value",
            "refresh_" + "token": "synthetic-refresh-value",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "user-follow-read user-library-read user-read-private user-top-read",
        },
    )


def _tokens(
    settings: SpotifySettings,
    script: ScriptedHTTP,
) -> tuple[SpotifyTokenManager, SpotifyTransport]:
    transport = SpotifyTransport(httpx.MockTransport(script))
    manager = SpotifyTokenManager(
        settings=settings,
        transport=transport,
        store=MemoryCredentialStore(),
        clock=lambda: 0.0,
    )
    return manager, transport


def _isolated_policy(tmp_path: Path) -> BoundaryPolicy:
    return BoundaryPolicy(allowed_write_roots=(tmp_path,), allowed_children=())


def test_dynamic_authorization_records_browser_http_and_one_fake_listener(
    boundary_policy: BoundaryPolicy,
) -> None:
    script = ScriptedHTTP(boundary_policy, [_token_response()])
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "synthetic-client"})
    tokens, transport = _tokens(settings, script)
    browser = RecordingBrowser(boundary_policy)
    servers = ServerFactory(boundary_policy, _CallbackOutcome.success("synthetic-code"))
    before_binds = len(boundary_policy.callback_binds)
    before_closes = len(boundary_policy.callback_closes)
    before_browser = len(boundary_policy.browser_handoffs)
    before_http = len(boundary_policy.http_attempts)

    result = SpotifyAuthorization(
        settings=settings,
        tokens=tokens,
        browser_opener=browser,
        random_bytes=_deterministic_bytes(b"v" * 32, b"s" * 32),
        _server_factory=servers,
    ).authorize(frozenset({Capability.HEALTH}), mode=AuthorizationMode.DYNAMIC_LOOPBACK)

    assert result == AuthorizationResult(True, frozenset({Capability.HEALTH}))
    assert boundary_policy.callback_binds[before_binds:] == [("127.0.0.1", 0)]
    assert boundary_policy.callback_closes[before_closes:] == [("127.0.0.1", 49152)]
    assert boundary_policy.browser_handoffs[before_browser:] == [
        ("accounts.spotify.com", "/authorize")
    ]
    assert boundary_policy.http_attempts[before_http:] == [("accounts.spotify.com", True)]
    assert boundary_policy.binds == boundary_policy.connects == boundary_policy.sends == 0
    transport.close()


def test_manual_authorization_records_zero_listener_events(
    boundary_policy: BoundaryPolicy,
) -> None:
    script = ScriptedHTTP(boundary_policy, [_token_response()])
    settings = SpotifySettings("synthetic-client", "http://127.0.0.1:43210/callback")
    tokens, transport = _tokens(settings, script)
    browser = RecordingBrowser(boundary_policy)
    factory = ServerFactory(boundary_policy, _CallbackOutcome.invalid())
    before_binds = len(boundary_policy.callback_binds)
    before_closes = len(boundary_policy.callback_closes)

    def callback_reader() -> str:
        assert browser.target is not None
        state = parse_qs(urlsplit(browser.target).query)["state"][0]
        return f"http://127.0.0.1:43210/callback?code=synthetic-code&state={state}"

    result = SpotifyAuthorization(
        settings=settings,
        tokens=tokens,
        browser_opener=browser,
        random_bytes=_deterministic_bytes(b"m" * 32, b"n" * 32),
        _server_factory=factory,
    ).authorize(
        frozenset({Capability.HEALTH}),
        mode=AuthorizationMode.MANUAL,
        callback_reader=callback_reader,
    )

    assert result.authorized is True
    assert factory.servers == []
    assert boundary_policy.callback_binds[before_binds:] == []
    assert boundary_policy.callback_closes[before_closes:] == []
    transport.close()


@pytest.mark.parametrize(
    ("outcome", "raises"),
    (
        (_CallbackOutcome.denied(), False),
        (_CallbackOutcome.invalid(), False),
        (_CallbackOutcome.timeout(), False),
        (_CallbackOutcome.invalid(), True),
    ),
)
def test_every_loopback_failure_closes_the_single_fake_listener(
    outcome: _CallbackOutcome,
    raises: bool,
    tmp_path: Path,
) -> None:
    policy = _isolated_policy(tmp_path)
    script = ScriptedHTTP(policy, [])
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "synthetic-client"})
    tokens, transport = _tokens(settings, script)
    factory = ServerFactory(policy, outcome, raises=raises)

    result = SpotifyAuthorization(
        settings=settings,
        tokens=tokens,
        browser_opener=RecordingBrowser(policy),
        random_bytes=_deterministic_bytes(b"f" * 32, b"g" * 32),
        _server_factory=factory,
    ).authorize(frozenset({Capability.HEALTH}), mode=AuthorizationMode.DYNAMIC_LOOPBACK)

    assert result == AuthorizationResult(False, frozenset())
    assert policy.callback_binds == [("127.0.0.1", 0)]
    assert policy.callback_closes == [("127.0.0.1", 49152)]
    assert factory.servers[0].handles == 1
    assert policy.http_attempts == []
    transport.close()


def test_all_read_paths_record_scripted_hosts_and_zero_listener_events(
    boundary_policy: BoundaryPolicy,
) -> None:
    artist = {"id": "artist001", "name": "Synthetic Artist"}
    track = {
        "id": "track001",
        "name": "Synthetic Track",
        "artists": [{"id": "artist001", "name": "discarded"}],
    }
    responses = [
        _token_response(),
        _token_response(),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"artists": {"items": [artist], "next": None}}),
        httpx.Response(
            200,
            json={"artists": {"items": [artist], "next": None, "cursors": {}}},
        ),
        httpx.Response(200, json={"items": [{"track": track}], "next": None}),
        httpx.Response(200, json={"items": [track], "next": None}),
        httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "release001",
                        "name": "Synthetic Release",
                        "album_type": "album",
                        "release_date": "2026-08-31",
                        "release_date_precision": "day",
                        "artists": [{"id": "artist001", "name": "discarded"}],
                    }
                ],
                "next": None,
            },
        ),
    ]
    script = ScriptedHTTP(boundary_policy, responses)
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "synthetic-client"})
    store = MemoryCredentialStore()
    authorization_transport = SpotifyTransport(httpx.MockTransport(script))
    authorization_tokens = SpotifyTokenManager(
        settings=settings, transport=authorization_transport, store=store, clock=lambda: 0.0
    )
    authorization_tokens._exchange_authorization_code(
        "synthetic-code",
        redirect_uri="http://127.0.0.1:43210/callback",
        verifier="synthetic-verifier",
        granted_scopes=frozenset(
            {"user-follow-read", "user-library-read", "user-read-private", "user-top-read"}
        ),
    )
    authorization_transport.close()
    read_transport = SpotifyTransport(httpx.MockTransport(script))
    restarted = SpotifyTokenManager(
        settings=settings, transport=read_transport, store=store, clock=lambda: 10.0
    )
    source = SpotifySource(
        settings=settings,
        tokens=restarted,
        clock=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )
    before_binds = len(boundary_policy.callback_binds)
    before_closes = len(boundary_policy.callback_closes)
    before_browser = len(boundary_policy.browser_handoffs)
    before_http = len(boundary_policy.http_attempts)

    source.health()
    source.search_artists("Synthetic", 1)
    followed = source.followed_artists()
    source.saved_items()
    source.top_items("short_term", 1)
    source.recent_releases(
        [followed.items[0].source_refs[0]], datetime(2026, 8, 1, tzinfo=timezone.utc)
    )

    assert boundary_policy.callback_binds[before_binds:] == []
    assert boundary_policy.callback_closes[before_closes:] == []
    assert boundary_policy.browser_handoffs[before_browser:] == []
    attempts = boundary_policy.http_attempts[before_http:]
    assert attempts == [("accounts.spotify.com", True)] + [("api.spotify.com", True)] * 6
    assert script.responses == []
    read_transport.close()


def test_unapproved_scripted_host_is_recorded_before_rejection(tmp_path: Path) -> None:
    policy = _isolated_policy(tmp_path)

    with pytest.raises(BoundaryViolation, match="HTTP host blocked"):
        policy.record_http_attempt("unapproved.invalid", scripted=True)

    assert policy.http_attempts == [("unapproved.invalid", True)]


@pytest.mark.parametrize(
    "target",
    (
        _VALID_BROWSER_HANDOFF.replace("accounts.spotify.com", "accounts.spotify.com:443"),
        _VALID_BROWSER_HANDOFF.replace("accounts.spotify.com", "user@accounts.spotify.com"),
        _VALID_BROWSER_HANDOFF + "#synthetic-fragment",
        _VALID_BROWSER_HANDOFF.replace("accounts.spotify.com", "accounts.spotify.com."),
        "https://accounts.spotify.com/authorize",
    ),
    ids=("custom-port", "userinfo", "fragment", "alternate-netloc", "missing-query"),
)
def test_browser_handoff_guard_rejects_non_exact_authorities_and_targets(
    target: str,
    tmp_path: Path,
) -> None:
    policy = _isolated_policy(tmp_path)

    with pytest.raises(BoundaryViolation, match="browser handoff blocked"):
        policy.record_browser_handoff(target)

    assert len(policy.browser_handoffs) == 1


def test_non_scripted_http_transport_is_rejected(tmp_path: Path) -> None:
    policy = _isolated_policy(tmp_path)

    with pytest.raises(BoundaryViolation, match="scripted mock transport required"):
        policy.record_http_attempt("api.spotify.com", scripted=False)

    assert policy.http_attempts == [("api.spotify.com", False)]


def test_listener_close_recorder_positive_control(tmp_path: Path) -> None:
    policy = _isolated_policy(tmp_path)
    server = LifecycleServer(
        port=0, outcome=_CallbackOutcome.invalid(), policy=policy, raises=False
    )

    server.server_close()

    assert policy.callback_closes == [("127.0.0.1", 49152)]


@pytest.mark.parametrize(
    ("surface", "operation"),
    (
        ("file", lambda path: boundaries._guarded_open(path, "w")),
        ("directory", lambda path: boundaries._guarded_mkdir(path)),
        ("link", lambda path: boundaries._guarded_symlink("target", path)),
        ("sqlite", lambda path: boundaries._guarded_sqlite_connect(path)),
    ),
    ids=("file", "directory", "link", "sqlite"),
)
def test_each_filesystem_guard_has_an_independent_positive_control(
    surface: str,
    operation: Callable[[Path], object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _isolated_policy(tmp_path)
    monkeypatch.setattr(boundaries, "_POLICY", policy)

    with pytest.raises(BoundaryViolation, match="filesystem write blocked"):
        operation(Path("/clean-room-positive-control") / surface)

    assert policy.write_checks == {surface: 1}


def test_configuration_token_and_read_paths_attempt_no_filesystem_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _isolated_policy(tmp_path)
    monkeypatch.setattr(boundaries, "_POLICY", policy)
    script = ScriptedHTTP(policy, [_token_response(), httpx.Response(200, json={})])
    settings = load_spotify_settings({"SPOTIFY_CLIENT_ID": "synthetic-client"})
    store = MemoryCredentialStore()
    transport = SpotifyTransport(httpx.MockTransport(script))
    manager = SpotifyTokenManager(settings=settings, transport=transport, store=store)
    manager._exchange_authorization_code(
        "synthetic-code",
        redirect_uri="http://127.0.0.1:43210/callback",
        verifier="synthetic-verifier",
        granted_scopes=frozenset({"user-read-private"}),
    )
    source = SpotifySource(
        settings=settings,
        tokens=manager,
        clock=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )

    source.health()

    assert policy.write_checks == {}
    transport.close()
