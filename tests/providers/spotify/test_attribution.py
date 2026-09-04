from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]
HEADER = "# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend."
ADAPTED_IMPLEMENTATION_FILES = frozenset(
    {
        "callback.py",
        "credentials.py",
        "keyring_store.py",
        "oauth.py",
        "source.py",
        "tokens.py",
        "transport.py",
    }
)
INDEPENDENT_IMPLEMENTATION_FILES = frozenset(
    {"__init__.py", "config.py", "normalize.py", "scopes.py"}
)


def _missing_attribution(notice: str) -> tuple[str, ...]:
    normalized = " ".join(notice.split())
    required = (
        "Spotify Skill",
        "Copyright 2025 Fabio C.",
        "Apache License, Version 2.0",
        "https://github.com/fabioc-aloha/spotify-skill",
        "modified the adapted PKCE, Spotify request, and test patterns",
        "does not imply endorsement",
    )
    return tuple(value for value in required if value not in normalized)


def test_notice_preserves_precise_upstream_credit_and_modification_notice() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

    assert _missing_attribution(notice) == ()
    assert notice == (
        "Music Friend\n\n"
        "This product includes modified material derived from Spotify Skill:\n"
        "https://github.com/fabioc-aloha/spotify-skill\n\n"
        "Copyright 2025 Fabio C.\n\n"
        "Spotify Skill is licensed under the Apache License, Version 2.0. Music Friend\n"
        "modified the adapted PKCE, Spotify request, and test patterns. This notice gives\n"
        "upstream credit only and does not imply endorsement by the upstream project or\n"
        "its contributors.\n"
    )


def test_attribution_gate_rejects_an_incomplete_notice() -> None:
    assert _missing_attribution("Spotify Skill") == (
        "Copyright 2025 Fabio C.",
        "Apache License, Version 2.0",
        "https://github.com/fabioc-aloha/spotify-skill",
        "modified the adapted PKCE, Spotify request, and test patterns",
        "does not imply endorsement",
    )


def _missing_modified_source_headers(contents: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(name for name in ADAPTED_IMPLEMENTATION_FILES if HEADER not in contents[name])
    )


def test_exact_adapted_implementation_files_carry_a_short_modified_header() -> None:
    package = ROOT / "src" / "music_friend" / "providers" / "spotify"
    contents = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}
    contents["keyring_store.py"] = (
        ROOT / "src" / "music_friend" / "providers" / "keyring_store.py"
    ).read_text(encoding="utf-8")

    assert set(contents) == ADAPTED_IMPLEMENTATION_FILES | INDEPENDENT_IMPLEMENTATION_FILES
    assert _missing_modified_source_headers(contents) == ()
    assert all(HEADER not in contents[name] for name in INDEPENDENT_IMPLEMENTATION_FILES)


def test_modified_source_header_gate_detects_one_missing_applicable_header() -> None:
    contents = {name: HEADER for name in ADAPTED_IMPLEMENTATION_FILES}
    contents["callback.py"] = "independently implemented"

    assert _missing_modified_source_headers(contents) == ("callback.py",)
