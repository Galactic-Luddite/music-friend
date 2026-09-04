from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from music_friend.errors import AuthenticationRequiredError
from music_friend.providers import Capability, HealthStatus
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify.callback import _CallbackOutcome
from music_friend.providers.spotify.config import (
    SpotifySettings,
    load_spotify_settings,
)
from music_friend.providers.spotify.oauth import (
    AuthorizationMode,
    AuthorizationResult,
    SpotifyAuthorization,
)
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport

CLIENT_ID = "synthetic-client-id"
SELECTED_CAPABILITIES = frozenset(Capability)
SELECTED_SCOPES = "user-follow-read user-library-read user-read-private user-top-read"
OBSERVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}
        self.loads = 0
        self.saves = 0
        self.deletes = 0

    def save(self, key: CredentialKey, value: str) -> None:
        self.saves += 1
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        self.loads += 1
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.deletes += 1
        self.values.pop(key, None)


class FakeCallbackServer:
    def __init__(self, port: int, outcome: _CallbackOutcome) -> None:
        self.server_address = ("127.0.0.1", 49152 if port == 0 else port)
        self._outcome = outcome
        self.timeout: float | None = None
        self.handles = 0
        self.closes = 0

    def handle_request(self) -> None:
        self.handles += 1

    def server_close(self) -> None:
        self.closes += 1


class FakeServerFactory:
    def __init__(self, outcome: _CallbackOutcome) -> None:
        self.outcome = outcome
        self.binds: list[tuple[str, int]] = []
        self.states: list[str] = []
        self.servers: list[FakeCallbackServer] = []

    def __call__(self, port: int, *, expected_state: str) -> FakeCallbackServer:
        self.binds.append(("127.0.0.1", port))
        self.states.append(expected_state)
        server = FakeCallbackServer(port, self.outcome)
        self.servers.append(server)
        return server


class SpotifyScript:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = tuple(parse_qsl(request.url.query.decode("ascii"), keep_blank_values=True))
        self.requests.append((request.method, str(request.url.copy_with(query=None)), query))
        if request.url.host == "accounts.spotify.com":
            form = dict(parse_qsl(request.content.decode("ascii"), keep_blank_values=True))
            if form.get("grant_type") == "authorization_code":
                assert set(form) == {
                    "client_id",
                    "grant_type",
                    "code",
                    "redirect_uri",
                    "code_verifier",
                }
                return httpx.Response(
                    200,
                    json={
                        "access_" + "token": "initial-access-value",
                        "refresh_" + "token": "persisted-refresh-value",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": SELECTED_SCOPES,
                    },
                )
            assert form == {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_" + "token": "persisted-refresh-value",
            }
            return httpx.Response(
                200,
                json={
                    "access_" + "token": "restarted-access-value",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": SELECTED_SCOPES,
                },
            )

        assert request.url.host == "api.spotify.com"
        artist = {"id": "artist001", "name": "Synthetic Artist"}
        track = {
            "id": "track001",
            "name": "Synthetic Track",
            "artists": [{"id": "artist001", "name": "discarded"}],
        }
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"display_name": "discarded profile field"})
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={"artists": {"items": [artist], "next": None}},
            )
        if request.url.path == "/v1/me/following":
            return httpx.Response(
                200,
                json={"artists": {"items": [artist], "next": None, "cursors": {}}},
            )
        if request.url.path == "/v1/me/tracks":
            return httpx.Response(200, json={"items": [{"track": track}], "next": None})
        if request.url.path == "/v1/me/top/tracks":
            return httpx.Response(200, json={"items": [track], "next": None})
        if request.url.path == "/v1/artists/artist001/albums":
            return httpx.Response(
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
            )
        raise AssertionError("unexpected scripted request")


def deterministic_bytes(*values: bytes) -> Callable[[int], bytes]:
    remaining = iter(values)

    def read(length: int) -> bytes:
        value = next(remaining)
        assert len(value) == length
        return value

    return read


def new_transport(script: SpotifyScript) -> SpotifyTransport:
    return SpotifyTransport(httpx.MockTransport(script))


def test_empty_first_run_authorizes_restarts_reads_and_disconnects() -> None:
    store = MemoryCredentialStore()
    with pytest.raises(ValueError) as missing:
        load_spotify_settings({})
    assert str(missing.value) == ("SPOTIFY_CLIENT_ID must contain 1..256 non-whitespace characters")

    loaded = load_spotify_settings({"SPOTIFY_CLIENT_ID": CLIENT_ID})
    empty_transport = SpotifyTransport(
        httpx.MockTransport(lambda _request: pytest.fail("empty state attempted HTTP"))
    )
    empty_manager = SpotifyTokenManager(settings=loaded, transport=empty_transport, store=store)
    assert empty_manager.status().connected is False
    with pytest.raises(AuthenticationRequiredError):
        empty_manager._access_token()
    empty_transport.close()

    script = SpotifyScript()
    authorization_transport = new_transport(script)
    authorization_tokens = SpotifyTokenManager(
        settings=loaded,
        transport=authorization_transport,
        store=store,
        clock=lambda: 0.0,
    )
    browser_urls: list[str] = []
    servers = FakeServerFactory(_CallbackOutcome.success("synthetic-authorization-code"))
    authorizer = SpotifyAuthorization(
        settings=loaded,
        tokens=authorization_tokens,
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=deterministic_bytes(b"v" * 32, b"s" * 32),
        _server_factory=servers,
    )

    result = authorizer.authorize(
        SELECTED_CAPABILITIES,
        mode=AuthorizationMode.DYNAMIC_LOOPBACK,
    )

    assert result == AuthorizationResult(True, SELECTED_CAPABILITIES)
    assert servers.binds == [("127.0.0.1", 0)]
    assert servers.servers[0].handles == servers.servers[0].closes == 1
    assert store.saves == 1
    assert len(store.values) == 1
    authorization_query = parse_qs(urlsplit(browser_urls[0]).query)
    assert authorization_query["scope"] == [SELECTED_SCOPES]
    assert authorization_query["redirect_uri"] == ["http://127.0.0.1:49152/callback"]
    authorization_transport.close()

    restarted_transport = new_transport(script)
    restarted_tokens = SpotifyTokenManager(
        settings=loaded,
        transport=restarted_transport,
        store=store,
        clock=lambda: 10.0,
    )
    source = SpotifySource(
        settings=loaded,
        tokens=restarted_tokens,
        clock=lambda: OBSERVED_AT,
    )

    assert source.health().status is HealthStatus.HEALTHY
    search = source.search_artists("Synthetic", 1)
    followed = source.followed_artists()
    saved = source.saved_items()
    top = source.top_items("short_term", 1)
    releases = source.recent_releases(
        [followed.items[0].source_refs[0]],
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    artist_id = "mf:28c07c158817da73996f3edc69288f751591dac525b6f45d979b7689b60486ba"
    track_id = "mf:70bb21246e8a71d3120681753f3a2f9b4c0e9e601531c10141ed3a09a25bde3e"
    release_id = "mf:8aa4ee9a56755b011ef3857523a885f72623c1c042d72ff152bf805fdedf9477"
    assert [artist.local_id for artist in search.items] == [artist_id]
    assert [artist.local_id for artist in followed.items] == [artist_id]
    assert [item.local_id for item in saved.items] == [track_id]
    assert [item.local_id for item in top.items] == [track_id]
    assert [release.local_id for release in releases.items] == [release_id]
    assert source.capabilities().granted == SELECTED_CAPABILITIES
    assert {urlsplit(url).hostname for _, url, _ in script.requests} == {
        "accounts.spotify.com",
        "api.spotify.com",
    }

    restarted_tokens.disconnect()
    assert restarted_tokens.status().connected is False
    assert store.values == {}
    with pytest.raises(AuthenticationRequiredError):
        source.health()
    restarted_transport.close()


def test_manual_first_run_uses_reader_without_listener_or_callback_disclosure(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SpotifySettings(CLIENT_ID, "http://127.0.0.1:43210/callback")
    store = MemoryCredentialStore()
    script = SpotifyScript()
    transport = new_transport(script)
    tokens = SpotifyTokenManager(
        settings=settings,
        transport=transport,
        store=store,
        clock=lambda: 0.0,
    )
    browser_urls: list[str] = []
    servers = FakeServerFactory(_CallbackOutcome.invalid())
    callback_targets: list[str] = []
    argv_before = tuple(sys.argv)
    environment_before = dict(os.environ)

    def read_callback() -> str:
        state = parse_qs(urlsplit(browser_urls[0]).query)["state"][0]
        target = f"http://127.0.0.1:43210/callback?code=manual-callback-canary&state={state}"
        callback_targets.append(target)
        return target

    result = SpotifyAuthorization(
        settings=settings,
        tokens=tokens,
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=deterministic_bytes(b"m" * 32, b"n" * 32),
        _server_factory=servers,
    ).authorize(
        SELECTED_CAPABILITIES,
        mode=AuthorizationMode.MANUAL,
        callback_reader=read_callback,
    )

    captured = capsys.readouterr()
    surfaces = repr(result) + captured.out + captured.err + caplog.text
    assert result == AuthorizationResult(True, SELECTED_CAPABILITIES)
    assert tokens.capabilities().granted == SELECTED_CAPABILITIES
    assert tokens.status().granted_scopes == tuple(SELECTED_SCOPES.split())
    assert store.saves == 1
    assert servers.binds == []
    assert tuple(sys.argv) == argv_before
    assert dict(os.environ) == environment_before
    assert callback_targets[0] not in " ".join(sys.argv)
    assert callback_targets[0] not in repr(os.environ)
    assert callback_targets[0] not in surfaces
    assert "manual-callback-canary" not in surfaces
    transport.close()
