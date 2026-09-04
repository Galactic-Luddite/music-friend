from __future__ import annotations

import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from . import boundaries
from .boundaries import BoundaryPolicy, BoundaryViolation, GuardedSocket

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_session_network_surfaces_are_guarded(boundary_policy: BoundaryPolicy) -> None:
    assert socket.socket.__name__ == "GuardedSocket"
    assert socket.create_connection.__name__ == "blocked_create_connection"
    assert socket.getaddrinfo.__name__ == "blocked_dns_lookup"
    assert urllib.request.urlopen.__name__ == "blocked_urlopen"
    assert boundary_policy.binds == 0
    assert boundary_policy.connects == 0
    assert boundary_policy.sends == 0


def test_policy_rejects_every_network_operation_without_performing_it() -> None:
    policy = BoundaryPolicy(allowed_write_roots=(), allowed_children=())

    for operation in ("bind", "listen", "connect", "dns", "urllib", "sendto", "sendmsg"):
        with pytest.raises(BoundaryViolation, match="network operation blocked"):
            policy.reject_network(operation)


def test_session_rejects_shell_processes_before_creation() -> None:
    with pytest.raises(BoundaryViolation, match="shell execution blocked"):
        subprocess.Popen(["unused-command"], shell=True)


def test_policy_requires_exact_child_process_declaration() -> None:
    policy = BoundaryPolicy(
        allowed_write_roots=(),
        allowed_children=(("python", "-m", "build"),),
    )

    policy.check_child(("python", "-m", "build"), shell=False)
    with pytest.raises(BoundaryViolation, match="undeclared child process"):
        policy.check_child(("python", "-m", "pip"), shell=False)
    with pytest.raises(BoundaryViolation, match="undeclared child process"):
        policy.check_child(("python", "-m", "build", "--wheel"), shell=False)


def test_policy_compares_absolute_child_paths_after_normalization(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "python"
    scanner = tmp_path / "source" / "scanner.py"
    policy = BoundaryPolicy(
        allowed_write_roots=(),
        allowed_children=((str(interpreter), str(scanner)),),
    )

    policy.check_child(
        (f"{tmp_path}//venv/./python", f"{tmp_path}/source/../source/scanner.py"), shell=False
    )


def test_policy_rejects_arbitrary_interpreter_code() -> None:
    policy = BoundaryPolicy(
        allowed_write_roots=(),
        allowed_children=((sys.executable, "-I", "-c", "approved"),),
    )

    with pytest.raises(BoundaryViolation, match="undeclared child process"):
        policy.check_child((sys.executable, "-I", "-c", "unapproved"), shell=False)


@pytest.mark.parametrize(
    ("method", "arguments"),
    (
        ("bind", (("127.0.0.1", 0),)),
        ("listen", ()),
        ("connect", (("127.0.0.1", 9),)),
        ("connect_ex", (("127.0.0.1", 9),)),
        ("send", (b"data",)),
        ("sendall", (b"data",)),
        ("sendto", (b"data", ("127.0.0.1", 9))),
        ("sendmsg", ([b"data"],)),
    ),
)
def test_every_guarded_socket_method_refuses_before_io(
    method: str, arguments: tuple[object, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = BoundaryPolicy(allowed_write_roots=(), allowed_children=())
    monkeypatch.setattr(boundaries, "_POLICY", policy)
    guarded = GuardedSocket()
    try:
        with pytest.raises(BoundaryViolation, match="network operation blocked"):
            getattr(guarded, method)(*arguments)
    finally:
        guarded.close()


def test_named_child_validators_reject_shaped_bypasses(tmp_path: Path) -> None:
    source = REPOSITORY_ROOT
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    policy = BoundaryPolicy(
        allowed_write_roots={"test": allowed},
        allowed_children=(),
        allowed_child_ids=(
            "harness-certification",
            "python-artifact-smoke",
            "python-build-export",
        ),
        source_root=source,
    )
    python = str(Path(sys.executable).resolve())
    expected_smoke = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(allowed / 'wheel.whl')!r}); "
        "from music_friend.store import Catalog; "
        f"catalog = Catalog.open(Path({str(allowed / 'catalog.sqlite3')!r})); "
        "assert catalog._connection.execute("
        "'SELECT version FROM schema_migrations ORDER BY version').fetchall() "
        "== [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]; catalog.close()"
    )
    shaped_bypasses = (
        (python, "-m", "build", "--arbitrary", str(allowed)),
        (python, "-I", "-c", expected_smoke + "; __import__('os').getcwd()"),
        (
            "/bin/bash",
            str(allowed / "clean-room-phase1.sh"),
            "--python",
            python,
            "--wheelhouse",
            str(allowed),
            "--result",
            str(allowed / "result.json"),
            "--commit",
            "0" * 40,
        ),
    )

    for argv in shaped_bypasses:
        with pytest.raises(BoundaryViolation, match="undeclared child process"):
            policy.check_child(argv, shell=False, cwd=allowed)


def test_named_child_validators_bind_commands_to_declared_roots(tmp_path: Path) -> None:
    source = REPOSITORY_ROOT
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    policy = BoundaryPolicy(
        allowed_write_roots={"build": allowed / "build", "pytest": allowed},
        allowed_children=(),
        allowed_child_ids=(
            "git-archive-head",
            "git-test-add",
            "git-test-commit",
            "git-test-head",
            "git-test-init",
            "hatchling-build",
            "python-build-export",
            "scanner-test",
        ),
        source_root=source,
    )
    python = str(Path(sys.executable).resolve())
    hatchling = str(Path(sys.executable).with_name("hatchling").resolve())
    probes = (
        (("git", "archive", "--format=tar", f"--output={outside / 'source.tar'}", "HEAD"), source),
        (
            (
                "git",
                "archive",
                "--format=tar",
                f"--output={allowed / 'source.tar'}",
                "HEAD",
                "--prefix=x",
            ),
            source,
        ),
        (("git", "init", "-q"), source),
        (("git", "add", "-A"), allowed),
        (("git", "commit", "-m", "test", "extra"), allowed),
        (("git", "rev-parse", "--show-toplevel"), allowed),
        (
            (
                python,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(outside / "dist"),
                str(allowed / "source"),
            ),
            source,
        ),
        ((hatchling, "build", "-d", str(outside)), source),
        ((python, str(source / "scripts" / "scan_public_tree.py"), str(allowed), "extra"), source),
    )

    for argv, cwd in probes:
        with pytest.raises(BoundaryViolation, match="undeclared child process"):
            policy.check_child(argv, shell=False, cwd=cwd)


def test_validated_child_evidence_records_observed_invocation(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    policy = BoundaryPolicy(
        allowed_write_roots={"pytest": allowed},
        allowed_children=(),
        allowed_child_ids=("git-archive-head",),
        source_root=REPOSITORY_ROOT,
    )
    command = (
        "git",
        "archive",
        "--format=tar",
        f"--output={allowed / 'source.tar'}",
        "HEAD",
    )

    policy.check_child(command, shell=False, cwd=REPOSITORY_ROOT)

    assert policy.child_commands == [
        {
            "count": 1,
            "id": "git-archive-head",
            "argv": [
                "git",
                "archive",
                "--format=tar",
                "--output={write:pytest}/source.tar",
                "HEAD",
            ],
            "cwd": "{source}",
        }
    ]


def test_scanner_child_evidence_aggregates_bounded_target_variants(tmp_path: Path) -> None:
    policy = BoundaryPolicy(
        allowed_write_roots={"pytest": tmp_path},
        allowed_children=(),
        allowed_child_ids=("scanner-test",),
        source_root=REPOSITORY_ROOT,
    )
    python = str(Path(sys.executable).resolve())
    scanner = str(REPOSITORY_ROOT / "scripts" / "scan_public_tree.py")

    for target in (tmp_path / "first", tmp_path / "second"):
        policy.check_child((python, scanner, str(target)), shell=False, cwd=REPOSITORY_ROOT)

    assert policy.child_commands == [
        {
            "count": 2,
            "id": "scanner-test",
            "argv": [
                "{python}",
                "{source}/scripts/scan_public_tree.py",
                "{write:pytest}/scan-target",
            ],
            "cwd": "{source}",
        }
    ]


def test_child_evidence_normalizes_external_certification_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "toolchain" / "python"
    wheelhouse = tmp_path / "wheelhouse"
    monkeypatch.setenv("MF_PHASE1_TEST_PYTHON", str(interpreter))
    monkeypatch.setenv("MF_PHASE1_TEST_WHEELHOUSE", str(wheelhouse))
    policy = BoundaryPolicy(
        allowed_write_roots={"pytest": tmp_path},
        allowed_children=(),
        source_root=REPOSITORY_ROOT,
    )

    evidence = boundaries._child_evidence(
        "harness-certification",
        (str(interpreter), str(wheelhouse)),
        REPOSITORY_ROOT,
        policy,
    )

    assert evidence["argv"] == ["{phase1-python}", "{phase1-wheelhouse}"]
