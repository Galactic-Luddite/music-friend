from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

from . import boundaries
from .boundaries import BoundaryPolicy, BoundaryViolation, GuardedSocket

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_wheel_install_accepts_only_the_declared_offline_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    monkeypatch.setenv("MF_PHASE1_TEST_WHEELHOUSE", str(wheelhouse))
    policy = BoundaryPolicy(
        allowed_write_roots={"pytest": tmp_path, "build": tmp_path / "build"},
        allowed_children=(),
        allowed_child_ids=("python-wheel-install",),
    )
    command = [
        str(tmp_path / "environment" / "bin" / "python"),
        "-I",
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--force-reinstall",
        str(tmp_path / "build" / "test.whl"),
    ]
    policy.check_child(command, shell=False, cwd=tmp_path)
    command[7] = str(tmp_path / "other")
    with pytest.raises(BoundaryViolation):
        policy.check_child(command, shell=False, cwd=tmp_path)
    command[7] = str(wheelhouse)
    command.remove("--no-index")
    with pytest.raises(BoundaryViolation):
        policy.check_child(command, shell=False, cwd=tmp_path)


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
    # A fake wheel directory with no wheel file present makes _artifact_smoke_code() return
    # None (see its `len(wheels) != 1` guard), so this literal is built by hand in the same
    # shape rather than through that helper; the migration-version tuple is still derived from
    # bundled_migrations() so it cannot drift from the real bundled schema.
    from music_friend.store.migrations import bundled_migrations

    expected_versions = [(migration.version,) for migration in bundled_migrations()]
    expected_smoke = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(allowed / 'wheel.whl')!r}); "
        "from music_friend.store import Catalog; "
        f"catalog = Catalog.open(Path({str(allowed / 'catalog.sqlite3')!r})); "
        "assert catalog._connection.execute("
        "'SELECT version FROM schema_migrations ORDER BY version').fetchall() "
        f"== {expected_versions!r}; catalog.close()"
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


def test_af_unix_connect_to_a_filesystem_path_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC (issue #65 round 2): an AF_UNIX socket that is NOT one of asyncio's own
    self-pipe ends is still full egress and must still be rejected -- exempting
    every AF_UNIX socket (the round-1 fix) wrongly let this through, since a local
    daemon reachable over AF_UNIX (e.g. a Docker socket) is egress too."""
    policy = BoundaryPolicy(allowed_write_roots=(), allowed_children=())
    monkeypatch.setattr(boundaries, "_POLICY", policy)
    # AF_UNIX paths are limited to ~104 bytes on macOS/BSD; tmp_path under pytest
    # can exceed that, so use a short path directly under the system temp root.
    socket_path = tempfile.mktemp(suffix=".sock", prefix="mf-cr-")

    # The listener must be a genuine, unguarded socket (not the module-patched
    # socket.socket, which is GuardedSocket in this session): it is test setup,
    # not the thing under test.
    listener = boundaries._ORIGINAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(socket_path)
        listener.listen(1)

        guarded = boundaries.GuardedSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(BoundaryViolation, match="network operation blocked: connect"):
                guarded.connect(socket_path)
        finally:
            guarded.close()
    finally:
        listener.close()
        Path(socket_path).unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "linux", reason="abstract-namespace AF_UNIX is Linux-only")
def test_af_unix_connect_to_an_abstract_namespace_address_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (issue #65 round 2): an abstract-namespace AF_UNIX address (leading NUL
    byte, no filesystem path at all) is still rejected -- it is not one of the
    two ends asyncio's own ``socket.socketpair()`` call returns."""
    policy = BoundaryPolicy(allowed_write_roots=(), allowed_children=())
    monkeypatch.setattr(boundaries, "_POLICY", policy)
    abstract_address = "\0clean-room-test-abstract"

    listener = boundaries._ORIGINAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(abstract_address)
        listener.listen(1)

        guarded = boundaries.GuardedSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(BoundaryViolation, match="network operation blocked: connect"):
                guarded.connect(abstract_address)
        finally:
            guarded.close()
    finally:
        listener.close()


def test_af_inet_and_af_inet6_connect_are_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard (issue #65 round 2): real AF_INET/AF_INET6 egress attempts
    must still be rejected exactly as before the self-pipe exemption existed."""
    policy = BoundaryPolicy(allowed_write_roots=(), allowed_children=())
    monkeypatch.setattr(boundaries, "_POLICY", policy)

    for family, address in (
        (socket.AF_INET, ("127.0.0.1", 9)),
        (socket.AF_INET6, ("::1", 9)),
    ):
        guarded = boundaries.GuardedSocket(family, socket.SOCK_STREAM)
        try:
            with pytest.raises(BoundaryViolation, match="network operation blocked: connect"):
                guarded.connect(address)
        finally:
            guarded.close()


def test_asyncio_to_thread_still_wakes_the_event_loop_under_the_guard(
    boundary_policy: BoundaryPolicy,
) -> None:
    """AC (issue #65 round 2): explicitly proves the thing the self-pipe exemption
    exists to keep working, rather than relying on the broader suite passing.

    Before the round-1 fix, this hung forever: the worker thread's
    ``call_soon_threadsafe`` wakeup write to the self-pipe was silently rejected by
    the guard, so the main loop's selector never woke up. A bounded wait here turns
    any regression back into a fast, explicit test failure instead of a CI hang.
    """
    import asyncio
    import queue
    import threading

    result: queue.Queue[str] = queue.Queue()

    def run() -> None:
        async def work() -> str:
            return await asyncio.to_thread(lambda: "done")

        result.put(asyncio.run(work()))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "asyncio.to_thread hung under the clean-room guard"
    assert result.get_nowait() == "done"
