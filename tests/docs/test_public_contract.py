"""Executable contracts for the public Music Friend guides and skill."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "music-friend" / "SKILL.md"
EVALS = ROOT / "skills" / "music-friend" / "evals" / "evals.json"

EXPECTED_TOOLS = {
    "music_status",
    "refresh_music",
    "search_catalog",
    "list_watchlist",
    "update_watchlist",
    "list_inbox",
    "update_inbox_item",
    "explain_inbox_item",
}


def test_skill_has_cross_client_frontmatter() -> None:
    """Codex and other Agent Skills clients require standard YAML frontmatter."""
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(?P<header>.*?)\n---\n", text, re.DOTALL)

    assert match is not None
    header = match.group("header").splitlines()
    assert header[0] == "name: music-friend"
    assert len(header) == 2
    assert header[1].startswith("description: Use when ")


def test_skill_uses_only_the_published_normal_music_tool_surface() -> None:
    """A skill edit cannot reintroduce setup, credentials, or extra MCP operations."""
    text = SKILL.read_text(encoding="utf-8")
    mentioned_tools = set(re.findall(r"`([a-z_]+)`", text)).intersection(EXPECTED_TOOLS)

    assert mentioned_tools == EXPECTED_TOOLS
    assert "credential enrollment" not in text.lower()
    assert "ticket purchasing" not in text.lower()
    assert "local CLI" in text


def test_skill_documents_inbox_states_and_explicit_save_authorization() -> None:
    """An explained, identified item can be saved when the person explicitly asks."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "`unread`, `saved`, or `dismissed`" in text
    assert (
        "`save it` authorizes setting the state to `saved` once the item identity is known" in text
    )
    assert "it has been explained" in text


def test_skill_evaluations_cover_normal_requests_and_safe_near_misses() -> None:
    """The examples define observable guidance without requiring a model or provider account."""
    evaluations = json.loads(EVALS.read_text(encoding="utf-8"))

    expected = {
        1: ["check_status", "confirm_refresh", "use_local_inbox"],
        2: ["search_catalog_first", "confirm_identity", "update_watchlist"],
        3: ["explain_inbox_item", "confirm_update", "update_inbox_item"],
        4: ["direct_to_local_cli", "no_credential_request"],
        5: ["decline_purchase", "offer_discovery_link"],
        6: [
            "lead_with_result",
            "freshness_only_when_relevant",
            "at_most_one_next_action",
            "no_internal_process_narration",
        ],
    }
    assert evaluations["skill_name"] == "music-friend"
    cases = evaluations["evals"]
    assert {case["id"] for case in cases} == set(expected)
    for case in cases:
        assert set(case) == {"id", "prompt", "expected_output", "files", "expectations"}
        assert isinstance(case["id"], int)
        assert isinstance(case["prompt"], str) and case["prompt"].strip()
        assert isinstance(case["expected_output"], str) and case["expected_output"].strip()
        assert case["files"] == []
        assert case["expectations"] == expected[case["id"]]
    assert not (ROOT / "skills" / "music-friend" / "evals.json").exists()


def test_skill_evaluations_exercise_the_concise_normal_response_contract() -> None:
    """A consumer evaluation catches replies that narrate work instead of reporting the result."""
    evaluations = json.loads(EVALS.read_text(encoding="utf-8"))
    response_case = next(case for case in evaluations["evals"] if case["id"] == 6)

    assert response_case["prompt"] == "What is new in my inbox?"
    assert response_case["expected_output"] == (
        "Leads with the inbox result, mentions freshness or a partial result only when it applies, "
        "and offers no more than one useful next action without narrating internal work."
    )


def test_skill_package_includes_the_standard_eval_directory() -> None:
    """The source package keeps the skill and its standard eval fixture together."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"/skills/music-friend"' in pyproject


def test_release_surface_is_normal_and_separates_contributor_and_runtime_guidance() -> None:
    """Public release metadata and guides keep their distinct audiences clear."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    contributor_guide = (ROOT / ("AGENTS.md")).read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    mcp = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")

    assert "Pre-Alpha" not in pyproject
    for text in (contributor_guide,):
        for required in (
            "local-first",
            "src/music_friend",
            "Python 3.10 through 3.14",
            "python -m pytest -q",
            "synthetic data",
            "provider-neutral",
        ):
            assert required in text
    assert "repository contributor guidance" in readme
    for required in (
        "MCP schemas",
        "optional runtime skill",
        "skills/music-friend/SKILL.md",
        "contributor guidance",
    ):
        assert required in mcp
    assert "installed from the wheel" in mcp
    assert "music-friend skill install --client codex" in mcp
    assert "music-friend skill install --client claude" in mcp
    assert "music-friend skill install --target SKILLS_DIRECTORY" in mcp
    assert "MCP schemas remain sufficient" in mcp
    assert "stops without writing" in install


def test_public_metadata_and_design_name_the_current_provider_neutral_release() -> None:
    """Public package and design copy cannot retain private framing or a different release line."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "design" / "product-design.md").read_text(encoding="utf-8")

    assert 'description = "A local-first, provider-neutral music companion."' in pyproject
    assert "private, local-first music companion" not in pyproject
    assert design.startswith("# Music Friend v0.1.0 design\n")
    assert "The v0.1.0 boundary excludes" in design
    assert "v1 design" not in design
    assert "v1 boundary" not in design


def test_clean_room_guide_uses_factual_platform_coverage_without_milestone_shorthand() -> None:
    """Public verification guidance must stand alone without undefined planning labels."""
    guide = (ROOT / "docs" / "testing" / "spotify-adapter-clean-room.md").read_text(
        encoding="utf-8"
    )

    assert "M2" not in guide
    assert "macOS Keychain credential storage is verified" in guide
    assert "Windows credential storage is not verified" in guide
    assert "Linux credential storage is not verified" in guide


def test_documentation_links_resolve_and_root_readme_lists_operational_guides() -> None:
    """Public navigation stays useful after files are renamed or split."""
    markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for link, anchor in re.findall(r"\[[^]]+\]\(([^)#]*)(?:#([^)]+))?\)", text):
            if "://" in link or link.startswith("mailto:"):
                continue
            target = (source if not link else source.parent / link).resolve()
            assert target.is_file(), f"{source}: {link}"
            if anchor:
                headings = re.findall(
                    r"^#{1,6}\s+(.+?)\s*$", target.read_text(encoding="utf-8"), re.MULTILINE
                )
                anchors = {_github_anchor(heading) for heading in headings}
                assert unquote(anchor) in anchors, f"{source}: {link}#{anchor}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for guide in ("docs/install.md", "docs/setup.md", "docs/mcp.md", "docs/operations.md"):
        assert guide in readme


def test_intel_macos_install_guidance_covers_the_required_source_build_path() -> None:
    text = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")

    assert "Intel macOS" in text
    assert "No PyPI wheel" in text
    assert "Rust 1.83" in text
    assert "xcode-select --install" in text
    assert "brew install openssl@3 rust" in text
    assert 'OPENSSL_DIR="$(brew --prefix openssl@3)"' in text
    assert "OPENSSL_STATIC=1" in text
    assert "--no-binary cryptography" in text


def test_documented_music_friend_commands_match_the_local_command_surface() -> None:
    """Executable examples cannot drift to removed commands or model-only setup paths."""
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    )
    documented = set(re.findall(r"^music-friend(?:\s+[^\n]+)?$", text, re.MULTILINE))
    allowed = {
        "music-friend --help",
        "music-friend version",
        "music-friend setup",
        "music-friend connect spotify",
        "music-friend disconnect spotify",
        "music-friend status --json",
        "music-friend refresh catalog --json",
        "music-friend refresh releases --json",
        "music-friend refresh events --json",
        "music-friend refresh all --json",
        "music-friend watchlist list --json",
        "music-friend inbox list --json",
        "music-friend inbox show ITEM_ID --json",
        "music-friend diagnostics --json",
        "music-friend data export music-friend-export.json",
        "music-friend data backup music-friend-backup.json",
        "music-friend data import music-friend-export.json",
        "music-friend data restore music-friend-backup.json",
        "music-friend data delete",
        "music-friend schedule status --json",
        "music-friend schedule install",
        "music-friend schedule remove",
        "music-friend skill install --client codex",
        "music-friend skill install --client claude",
        "music-friend skill install --target SKILLS_DIRECTORY",
        "music-friend skill install --target SKILLS_DIRECTORY --replace",
    }
    assert documented <= allowed


def test_credential_and_network_docs_state_the_actual_local_boundaries() -> None:
    """Safety guidance cannot imply that catalog deletion also removes provider credentials."""
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    setup = (ROOT / "docs" / "setup.md").read_text(encoding="utf-8")
    mcp = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")

    assert "credentials remain" in operations.lower()
    assert "disconnect Spotify" in operations
    assert "setup with `-`" in operations
    for document in (setup, mcp):
        assert "interactive CLI only" in document
        assert "approved native credential store" in document
    assert "no network-reachable inbound service" in security
    assert "temporary 127.0.0.1 OAuth callback" in security


def test_public_markdown_files_include_contributor_guidance() -> None:
    """The public residue scan covers the checkout-only contributor guide."""
    assert ROOT / ("AGENTS.md") in _public_markdown_files()


def _public_markdown_files() -> list[Path]:
    return [
        ROOT / ("AGENTS.md"),
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        *sorted((ROOT / "docs").rglob("*.md")),
        SKILL,
        EVALS,
    ]


def _github_anchor(heading: str) -> str:
    normalized = heading.lower().replace("’", "")
    normalized = re.sub(r"[^a-z0-9 _-]", "", normalized)
    return re.sub(r"[ _]+", "-", normalized).strip("-")


def test_public_markdown_has_no_private_path_or_process_residue() -> None:
    """Docs and skill content remain suitable for a public source archive."""
    public_files = _public_markdown_files()
    forbidden = (
        "/Users/",
        "/home/",
        "." + "super" + "powers",
        "progress-" + "ledger",
        "task-" + "8",
        "agent " + "review",
        "internal " + "workflow",
    )
    for path in public_files:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(value in text for value in forbidden), path
