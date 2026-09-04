"""Tests for the explicit Spotify configuration boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from music_friend.providers.spotify.config import SpotifySettings, load_spotify_settings

PACKAGE = Path(__file__).parents[3] / "src" / "music_friend" / "providers" / "spotify"


def test_settings_read_only_explicit_allowlist() -> None:
    settings = load_spotify_settings(
        {
            "SPOTIFY_CLIENT_ID": "public-client-id",
            "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
            "SPOTIFY_CLIENT_SECRET": "ignored-canary",
            "SPOTIFY_REFRESH_TOKEN": "ignored-canary",
            "UNRELATED": "ignored-canary",
        }
    )

    assert settings == SpotifySettings(
        client_id="public-client-id",
        redirect_uri="http://127.0.0.1:8888/callback",
    )
    assert "canary" not in repr(settings)


@pytest.mark.parametrize("client_id", (None, "", " ", "x" * 257, 7))
def test_settings_reject_invalid_client_identifiers(client_id: object) -> None:
    with pytest.raises(ValueError):
        load_spotify_settings({"SPOTIFY_CLIENT_ID": client_id})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "redirect_uri",
    (
        "https://127.0.0.1:8888/callback",
        "http://localhost:8888/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:0/callback",
        "http://127.0.0.1:65536/callback",
        "http://user@127.0.0.1:8888/callback",
        "http://127.0.0.1:8888/callback?code=x",
        "http://127.0.0.1:8888/callback#fragment",
        "http://127.0.0.1:8888/callback/extra",
    ),
)
def test_settings_accept_only_exact_fixed_loopback_redirect(redirect_uri: str) -> None:
    with pytest.raises(ValueError):
        load_spotify_settings(
            {"SPOTIFY_CLIENT_ID": "public-client-id", "SPOTIFY_REDIRECT_URI": redirect_uri}
        )


def test_missing_redirect_selects_dynamic_mode_without_storing_a_uri() -> None:
    assert load_spotify_settings({"SPOTIFY_CLIENT_ID": "public-client-id"}).redirect_uri is None


def test_spotify_package_has_no_implicit_environment_or_dotenv_access() -> None:
    forbidden_calls: list[str] = []
    forbidden_imports: list[str] = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden_imports.extend(
                    alias.name for alias in node.names if alias.name in {"dotenv", "os"}
                )
            elif isinstance(node, ast.ImportFrom) and node.module in {"dotenv", "os"}:
                forbidden_imports.append(node.module)
            elif isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in {"load_dotenv", "environ"}:
                    forbidden_calls.append(name)
                for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                    if isinstance(argument, ast.Constant) and argument.value == ".env":
                        forbidden_calls.append(".env")

    assert forbidden_imports == []
    assert forbidden_calls == []
