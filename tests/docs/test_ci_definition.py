"""Contracts for the local, non-publishing validation definition."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_local_validation_covers_supported_platforms_and_release_checks() -> None:
    """A CI edit cannot silently drop an OS, audit, scan, build, or clean-install check."""
    workflow = (ROOT / ".github" / "workflows" / "local-validation.yml").read_text(encoding="utf-8")

    for required in (
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
        '"3.10"',
        '"3.14"',
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "gitleaks_8.18.4_linux_x64.tar.gz",
        "ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d",
        "gitleaks detect --redact --no-banner --source .",
        "pip_audit",
        "piplicenses",
        "ruff check",
        "mypy --strict",
        "python -m pytest -q --ignore=tests/clean_room",
        "python -m build --outdir dist",
        "scan_public_tree.py dist",
        "clean-install.py",
        "clean-room-phase1.sh",
    ):
        assert required in workflow

    assert "publish" not in workflow.lower()
    for action in re.findall(r"^\s*- uses: [^\s]+@([^\s#]+)", workflow, re.M):
        assert re.fullmatch(r"[0-9a-f]{40}", action), action

    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert workflow.count("timeout-minutes:") == 5
    assert workflow.count("--only-binary=:all:") == 2
    assert workflow.count("hatchling") >= 2
    assert "gitleaks/gitleaks-action" not in workflow
    assert "Path(sys.executable).resolve()" in workflow

    source_scan = workflow.split("  source-scan:", 1)[1].split("  build-and-install:", 1)[0]
    checkout = source_scan.split("actions/checkout@", 1)[1].split("      - name:", 1)[0]
    assert "fetch-depth: 0" in checkout

    build_gate = workflow.split("  build-and-install:", 1)[1].split("  clean-room:", 1)[0]
    for required in (
        "ubuntu-latest",
        "windows-latest",
        '"3.10"',
        '"3.14"',
        "wheelhouse-${{",
        "--distribution wheel-dist",
        "source.glob('*.whl')",
    ):
        assert required in build_gate

    clean_room = workflow.split("  clean-room:", 1)[1]
    assert '--dest "$RUNNER_TEMP/wheelhouse"' in clean_room
    assert '--wheelhouse "$RUNNER_TEMP/wheelhouse"' in clean_room
    assert "if: failure()" in clean_room
    assert 'cat "$RUNNER_TEMP/music-friend-clean-room.json"' in clean_room


def test_dependabot_checks_actions_and_python_dependencies_weekly() -> None:
    configuration = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert configuration.count('interval: "weekly"') == 2
    assert 'package-ecosystem: "github-actions"' in configuration
    assert 'package-ecosystem: "pip"' in configuration
    assert configuration.count("open-pull-requests-limit:") == 2
