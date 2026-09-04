"""Behavior and packaging tests for the optional Agent Skill installer."""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from music_friend.runtimes import cli

ROOT = Path(__file__).parents[2]
CANONICAL_SKILL = ROOT / "skills" / "music-friend" / "SKILL.md"
PACKAGED_SKILL = "music_friend/agent_skill/SKILL.md"


def _agent_skill() -> object:
    try:
        return importlib.import_module("music_friend.agent_skill")
    except ModuleNotFoundError:
        pytest.fail("music_friend.agent_skill is not implemented")


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        ({}, "select exactly one"),
        ({"client": "codex", "target": Path("skills")}, "select exactly one"),
        ({"client": "unknown"}, "unsupported"),
        ({"target": "skills"}, "must be a directory"),
    ),
)
def test_direct_installer_rejects_invalid_destination_selection(
    arguments: dict[str, object], message: str
) -> None:
    """Catches callers bypassing CLI validation with an ambiguous or invalid destination."""
    module = _agent_skill()

    with pytest.raises(module.SkillInstallError, match=message):
        module.install_skill(**arguments)


def test_cli_installs_for_each_supported_destination_without_opening_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches skill installation falling through to catalog startup or choosing a wrong root."""
    calls: list[tuple[object, object, bool]] = []
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "install_agent_skill",
        lambda *, client, target, replace: calls.append((client, target, replace)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_application_or_default",
        lambda _application: (_ for _ in ()).throw(AssertionError("catalog opened")),
    )

    cases = (
        (["skill", "install", "--client", "codex"], "codex", None, False),
        (["skill", "install", "--client", "claude", "--replace"], "claude", None, True),
        (["skill", "install", "--target", str(tmp_path)], None, tmp_path, False),
    )
    for argv, client, target, replace in cases:
        stdout, stderr = io.StringIO(), io.StringIO()
        result = cli.run_cli(argv, stdout=stdout, stderr=stderr)
        assert (result, stdout.getvalue(), stderr.getvalue()) == (
            0,
            "Music Friend skill installed.\n",
            "",
        )
        assert calls[-1] == (client, target, replace)


@pytest.mark.parametrize(
    "argv",
    (
        ["skill", "install"],
        ["skill", "install", "--client", "unknown"],
        ["skill", "install", "--client", "codex", "--target", "skills"],
        ["skill", "install", "--target"],
        ["skill", "install", "--replace", "--replace", "--client", "codex"],
    ),
)
def test_cli_rejects_invalid_skill_install_shapes_without_side_effects(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Catches ambiguous or unsupported installers reaching the filesystem."""
    monkeypatch.setattr(
        cli,
        "install_agent_skill",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("installer called")),
        raising=False,
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    result = cli.run_cli(argv, stdout=stdout, stderr=stderr, application=object())  # type: ignore[arg-type]

    assert result == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue().startswith("Usage: music-friend")


def test_cli_reports_idempotent_and_conflicting_skill_installations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unchanged install or a conflict being reported as a new successful write."""
    monkeypatch.setattr(cli, "install_agent_skill", lambda **_kwargs: False)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(["skill", "install", "--client", "codex"], stdout=stdout, stderr=stderr)
    assert (result, stdout.getvalue(), stderr.getvalue()) == (
        0,
        "Music Friend skill is already installed.\n",
        "",
    )

    def conflict(**_kwargs: object) -> bool:
        raise cli.SkillInstallError("different content")

    monkeypatch.setattr(cli, "install_agent_skill", conflict)
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(["skill", "install", "--client", "codex"], stdout=stdout, stderr=stderr)
    assert (result, stdout.getvalue(), stderr.getvalue()) == (
        1,
        "",
        "Music Friend could not install the skill.\n",
    )


def test_custom_install_is_idempotent_and_replace_is_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches silent overwrite of locally changed guidance or duplicate writes."""
    module = _agent_skill()
    content = b"portable skill\n"
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: content)

    installed = module.install_skill(target=tmp_path)
    destination = tmp_path / "music-friend" / "SKILL.md"
    first_mtime = destination.stat().st_mtime_ns
    assert installed is True
    assert destination.read_bytes() == content

    assert module.install_skill(target=tmp_path) is False
    assert destination.stat().st_mtime_ns == first_mtime

    destination.write_bytes(b"local change\n")
    with pytest.raises(module.SkillInstallError, match="already exists"):
        module.install_skill(target=tmp_path)
    assert destination.read_bytes() == b"local change\n"

    assert module.install_skill(target=tmp_path, replace=True) is True
    assert destination.read_bytes() == content


@pytest.mark.parametrize(
    ("client", "relative_destination"),
    (
        ("codex", Path(".agents/skills/music-friend/SKILL.md")),
        ("claude", Path(".claude/skills/music-friend/SKILL.md")),
    ),
)
def test_client_install_creates_the_expected_private_directory_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
    relative_destination: Path,
) -> None:
    """Catches client selection writing to another client's discovery location."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)

    assert module.install_skill(client=client) is True
    assert (tmp_path / relative_destination).read_bytes() == b"skill\n"


def test_explicit_replace_uses_a_same_directory_temporary_file_and_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches direct writes that can expose a partially written skill."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    replacements: list[tuple[str, str, int, int]] = []
    real_replace = os.replace

    def observed_replace(
        source: object,
        destination: object,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        replacements.append((os.fspath(source), os.fspath(destination), src_dir_fd, dst_dir_fd))
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(module.os, "replace", observed_replace)

    module.install_skill(target=tmp_path, replace=True)

    assert len(replacements) == 1
    source, destination, src_dir_fd, dst_dir_fd = replacements[0]
    assert Path(source).parent == Path(".")
    assert destination == "SKILL.md"
    assert src_dir_fd == dst_dir_fd
    assert (tmp_path / "music-friend" / "SKILL.md").read_bytes() == b"skill\n"


def test_no_replace_uses_descriptor_relative_no_clobber_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches no-replace installation using an overwriting commit primitive."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    links: list[tuple[str, str, int, int, bool]] = []
    real_link = os.link

    def observed_link(
        source: object,
        destination: object,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        links.append(
            (
                os.fspath(source),
                os.fspath(destination),
                src_dir_fd,
                dst_dir_fd,
                follow_symlinks,
            )
        )
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(module.os, "link", observed_link)
    monkeypatch.setattr(module.os, "supports_dir_fd", module.os.supports_dir_fd | {observed_link})
    monkeypatch.setattr(
        module.os,
        "supports_follow_symlinks",
        module.os.supports_follow_symlinks | {observed_link},
    )
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("replace called")),
    )

    assert module.install_skill(target=tmp_path) is True

    assert len(links) == 1
    source, destination, src_dir_fd, dst_dir_fd, follow_symlinks = links[0]
    assert Path(source).parent == Path(".")
    assert destination == "SKILL.md"
    assert src_dir_fd == dst_dir_fd
    assert follow_symlinks is False
    assert (tmp_path / "music-friend" / "SKILL.md").read_bytes() == b"skill\n"


def test_no_replace_commit_collision_preserves_competing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a destination created at the commit point being overwritten."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"packaged skill\n")
    competing = b"competing local skill\n"
    real_link = os.link

    def collide_then_link(
        source: object,
        destination: object,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dst_dir_fd,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(competing)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(module.os, "link", collide_then_link)
    monkeypatch.setattr(
        module.os, "supports_dir_fd", module.os.supports_dir_fd | {collide_then_link}
    )
    monkeypatch.setattr(
        module.os,
        "supports_follow_symlinks",
        module.os.supports_follow_symlinks | {collide_then_link},
    )

    with pytest.raises(module.SkillInstallError, match="already exists"):
        module.install_skill(target=tmp_path)

    destination = tmp_path / "music-friend" / "SKILL.md"
    assert destination.read_bytes() == competing
    assert list(destination.parent.iterdir()) == [destination]


def test_parent_swap_cannot_redirect_atomic_replace_outside_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a checked destination parent being replaced by a symlink before commit."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    root = tmp_path / "skills"
    root.mkdir()
    parent = root / "music-friend"
    displaced = root / "displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_replace = os.replace
    calls = 0

    def swapping_replace(
        source: object,
        destination: object,
        **kwargs: object,
    ) -> None:
        nonlocal calls
        calls += 1
        parent.rename(displaced)
        parent.symlink_to(outside, target_is_directory=True)
        if not kwargs:
            source_name = Path(os.fspath(source)).name
            real_replace(displaced / source_name, outside / source_name)
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "replace", swapping_replace)

    assert module.install_skill(target=root, replace=True) is True

    assert calls == 1
    assert not (outside / "SKILL.md").exists()
    assert (displaced / "SKILL.md").read_bytes() == b"skill\n"


def test_failed_atomic_replace_removes_the_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a failed replacement leaving partial guidance beside the destination."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        module.install_skill(target=tmp_path, replace=True)

    destination = tmp_path / "music-friend" / "SKILL.md"
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_install_rejects_non_directory_and_symlink_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches writes through roots, components, or targets that redirect outside the root."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    outside = tmp_path / "outside"
    outside.mkdir()

    root_file = tmp_path / "root-file"
    root_file.write_text("not a directory", encoding="utf-8")
    symlinked_root = tmp_path / "symlinked-root"
    symlinked_root.symlink_to(outside, target_is_directory=True)
    root_with_symlinked_component = tmp_path / "root-with-component"
    root_with_symlinked_component.mkdir()
    (root_with_symlinked_component / "music-friend").symlink_to(outside, target_is_directory=True)
    root_with_symlinked_target = tmp_path / "root-with-target"
    (root_with_symlinked_target / "music-friend").mkdir(parents=True)
    (root_with_symlinked_target / "music-friend" / "SKILL.md").symlink_to(outside / "escaped.md")
    root_with_directory_target = tmp_path / "root-with-directory-target"
    (root_with_directory_target / "music-friend" / "SKILL.md").mkdir(parents=True)

    for root in (
        root_file,
        symlinked_root,
        root_with_symlinked_component,
        root_with_symlinked_target,
        root_with_directory_target,
    ):
        with pytest.raises(module.SkillInstallError):
            module.install_skill(target=root, replace=True)
    assert list(outside.iterdir()) == []


def test_install_rejects_a_destination_that_escapes_the_selected_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a malformed packaged destination escaping the selected skills directory."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    monkeypatch.setattr(module, "_SKILL_RELATIVE_PATH", Path("../escaped.md"))

    with pytest.raises(module.SkillInstallError, match="outside"):
        module.install_skill(target=tmp_path)

    assert not (tmp_path.parent / "escaped.md").exists()


def test_install_fails_safely_without_directory_relative_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches an unsupported platform falling back to vulnerable pathname writes."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    monkeypatch.setattr(module.os, "supports_dir_fd", set())

    with pytest.raises(module.SkillInstallError, match="unavailable on this platform"):
        module.install_skill(target=tmp_path)

    assert not (tmp_path / "music-friend").exists()


def test_install_fails_safely_without_descriptor_relative_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a platform capability check that omits the no-clobber primitive."""
    module = _agent_skill()
    monkeypatch.setattr(module, "_read_packaged_skill", lambda: b"skill\n")
    monkeypatch.setattr(module.os, "supports_dir_fd", module.os.supports_dir_fd - {module.os.link})

    with pytest.raises(module.SkillInstallError, match="unavailable on this platform"):
        module.install_skill(target=tmp_path)

    assert not (tmp_path / "music-friend").exists()


def test_wheel_contains_the_exact_canonical_skill_and_installs_it(
    tmp_path: Path,
) -> None:
    """Catches wheel builds omitting, renaming, duplicating, or rewriting the canonical skill."""
    dist = tmp_path / "dist"
    raw_roots = os.environ.get("MF_CLEAN_ROOM_WRITE_ROOTS")
    if raw_roots:
        dist = Path(json.loads(raw_roots)["build"]) / tmp_path.name / "dist"
        dist.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(Path(sys.executable).with_name("hatchling")), "build", "-d", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if name == PACKAGED_SKILL]
        assert matches == [PACKAGED_SKILL]
        assert archive.read(PACKAGED_SKILL) == CANONICAL_SKILL.read_bytes()

    environment = tmp_path / "environment"
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "venv",
            "--copies",
            "--system-site-packages",
            str(environment),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    executable_directory = environment / ("Scripts" if os.name == "nt" else "bin")
    environment_python = executable_directory / ("python.exe" if os.name == "nt" else "python")
    command = executable_directory / ("music-friend.exe" if os.name == "nt" else "music-friend")
    child_environment = dict(os.environ)
    child_environment.update(PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1")
    subprocess.run(
        [
            str(environment_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ],
        cwd=tmp_path,
        env=child_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    target = tmp_path / "skills"
    target.mkdir()
    result = subprocess.run(
        [str(command), "skill", "install", "--target", str(target)],
        cwd=tmp_path,
        env=child_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    if os.name == "nt":
        assert result.returncode == 1
        assert not (target / "music-friend" / "SKILL.md").exists()
        return

    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "Music Friend skill installed.\n",
        "",
    )
    assert (target / "music-friend" / "SKILL.md").read_bytes() == CANONICAL_SKILL.read_bytes()
