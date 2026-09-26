"""Tests for the local Spotify-backed MCP stdio runtime."""

from __future__ import annotations

import json
import selectors
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from music_friend.providers.credentials import CredentialKey, CredentialStoreError
from music_friend.runtimes import mcp_stdio


class _Environment(Mapping[str, object]):
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values
        self.requested: list[str] = []

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: object = None) -> object:
        self.requested.append(key)
        return self._values.get(key, default)


class _CloseRecorder:
    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class _CredentialStore:
    def __init__(self) -> None:
        self.loads = 0

    def save(self, _key: object, _value: str) -> None:
        pass

    def load(self, _key: object) -> None:
        self.loads += 1
        return None

    def delete(self, _key: object) -> None:
        pass


class _Server:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.transports: list[str] = []

    def run(self, transport: str) -> None:
        self.transports.append(transport)
        if self.error is not None:
            raise self.error


def test_spotify_environment_maps_only_the_two_allowed_settings() -> None:
    values = _Environment(
        {
            "SPOTIFY_CLIENT_ID": "public-client",
            "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
            "OTHER": "canary",
        }
    )

    assert mcp_stdio.spotify_environment(values) == {
        "SPOTIFY_CLIENT_ID": "public-client",
        "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    }
    assert values.requested == ["SPOTIFY_CLIENT_ID", "SPOTIFY_REDIRECT_URI"]


@pytest.mark.parametrize("provider", (None, "unknown-provider"))
def test_spotify_composition_rejects_absent_or_unknown_provider_without_echoing_it(
    provider: str | None,
) -> None:
    supplied = "unknown-provider" if provider is not None else "missing-provider"

    with pytest.raises(ValueError) as error:
        with mcp_stdio.spotify_source(
            provider=provider,
            values={"SPOTIFY_CLIENT_ID": "public-client"},
        ):
            pass

    assert str(error.value) == "A supported music provider must be selected."
    assert supplied not in str(error.value)


def test_spotify_source_composes_existing_constructors_without_external_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = object()
    store = object()
    transport = _CloseRecorder()
    tokens = object()
    source = object()
    calls: dict[str, tuple[object, ...] | dict[str, object]] = {}

    def token_clock() -> float:
        return 12.0

    def source_clock() -> datetime:
        return datetime(2026, 9, 1, tzinfo=timezone.utc)

    def make_transport(actual_connector: object, *, clock: Callable[[], float]) -> _CloseRecorder:
        calls["transport"] = (actual_connector, clock)
        return transport

    def make_tokens(**kwargs: object) -> object:
        calls["tokens"] = kwargs
        return tokens

    def make_source(**kwargs: object) -> object:
        calls["source"] = kwargs
        return source

    monkeypatch.setattr(mcp_stdio, "SpotifyTransport", make_transport)
    monkeypatch.setattr(mcp_stdio, "SpotifyTokenManager", make_tokens)
    monkeypatch.setattr(mcp_stdio, "SpotifySource", make_source)

    with mcp_stdio.spotify_source(
        provider="spotify",
        values={"SPOTIFY_CLIENT_ID": "public-client"},
        connector_factory=lambda: connector,
        credential_store_factory=lambda: store,
        token_clock=token_clock,
        source_clock=source_clock,
    ) as actual:
        assert actual is source
        assert calls["transport"] == (connector, token_clock)
        assert calls["tokens"] == {
            "settings": mcp_stdio.load_spotify_settings({"SPOTIFY_CLIENT_ID": "public-client"}),
            "transport": transport,
            "store": store,
            "clock": token_clock,
        }
        assert calls["source"] == {
            "settings": mcp_stdio.load_spotify_settings({"SPOTIFY_CLIENT_ID": "public-client"}),
            "tokens": tokens,
            "clock": source_clock,
        }

    assert transport.closes == 1


def test_default_connector_ignores_proxy_and_ca_environment_before_credential_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    certificate_path = tmp_path / "ambient-ca-path-canary.pem"
    store = _CredentialStore()
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
    monkeypatch.setenv("SSL_CERT_DIR", str(certificate_path))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    with mcp_stdio.spotify_source(
        provider="spotify",
        values={"SPOTIFY_CLIENT_ID": "public-client"},
        credential_store_factory=lambda: store,
    ) as source:
        assert source._tokens._transport.trace == ()  # type: ignore[attr-defined]
        assert store.loads == 1

    assert not certificate_path.exists()


def test_stdio_session_closes_transport_after_normal_server_exit() -> None:
    transport = _CloseRecorder()
    server = _Server()

    mcp_stdio.run_stdio_session(
        provider="spotify",
        values={"SPOTIFY_CLIENT_ID": "public-client"},
        connector_factory=object,
        credential_store_factory=object,
        transport_factory=lambda _connector, _clock: transport,
        token_manager_factory=lambda _settings, _transport, _store, _clock: object(),
        source_factory=lambda _settings, _tokens, _clock: object(),
        server_factory=lambda _source: server,  # type: ignore[arg-type]
    )

    assert server.transports == ["stdio"]
    assert transport.closes == 1


def test_stdio_session_closes_transport_after_mcp_failure() -> None:
    transport = _CloseRecorder()
    server = _Server(error=RuntimeError("synthetic MCP failure"))

    with pytest.raises(RuntimeError, match="synthetic MCP failure"):
        mcp_stdio.run_stdio_session(
            provider="spotify",
            values={"SPOTIFY_CLIENT_ID": "public-client"},
            connector_factory=object,
            credential_store_factory=object,
            transport_factory=lambda _connector, _clock: transport,
            token_manager_factory=lambda _settings, _transport, _store, _clock: object(),
            source_factory=lambda _settings, _tokens, _clock: object(),
            server_factory=lambda _source: server,  # type: ignore[arg-type]
        )

    assert transport.closes == 1


def test_stdio_session_closes_transport_when_composition_fails() -> None:
    transport = _CloseRecorder()

    with pytest.raises(RuntimeError, match="synthetic composition failure"):
        mcp_stdio.run_stdio_session(
            provider="spotify",
            values={"SPOTIFY_CLIENT_ID": "public-client"},
            connector_factory=object,
            credential_store_factory=object,
            transport_factory=lambda _connector, _clock: transport,
            token_manager_factory=lambda _settings, _transport, _store, _clock: (
                _ for _ in ()
            ).throw(RuntimeError("synthetic composition failure")),
            source_factory=lambda _settings, _tokens, _clock: object(),
            server_factory=lambda _source: _Server(),  # type: ignore[arg-type]
        )

    assert transport.closes == 1


def test_main_selects_the_local_catalog_session_until_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def run_session(**kwargs: Any) -> None:
        calls.append(kwargs)

    class ConfigStore:
        def load(self) -> object:
            return mcp_stdio.LocalConfig(spotify_client_id="public-client")

    monkeypatch.setattr(mcp_stdio, "LocalConfigStore", ConfigStore)
    monkeypatch.setattr(mcp_stdio, "run_catalog_stdio_session", run_session)

    mcp_stdio.main()

    assert calls[0]["config"] == mcp_stdio.LocalConfig(spotify_client_id="public-client")


def test_main_redacts_all_runtime_failures_at_the_console_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing the process boundary would leak composition and runtime exception details."""
    canaries = "synthetic-credential-value provider-response-value /synthetic/absolute-path"

    class ConfigStore:
        def load(self) -> object:
            return mcp_stdio.LocalConfig()

    def run_session(**_kwargs: object) -> None:
        raise RuntimeError(canaries)

    monkeypatch.setattr(mcp_stdio, "LocalConfigStore", ConfigStore)
    monkeypatch.setattr(mcp_stdio, "run_catalog_stdio_session", run_session)

    with pytest.raises(SystemExit) as error:
        mcp_stdio.main()

    captured = capsys.readouterr()
    assert error.value.code == 1
    assert captured.out == ""
    assert captured.err == "Music Friend MCP could not start.\n"
    for canary in canaries.split():
        assert canary not in captured.err


def test_catalog_stdio_session_composes_refresh_modes_and_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application_closes: list[object] = []

    class Application:
        def close(self) -> None:
            application_closes.append(self)

    application = Application()
    event_transport = _CloseRecorder()
    event_client = object()
    source = object()
    server = _Server()
    refresh_calls: list[dict[str, object]] = []

    class EventClientFactory:
        def __new__(cls, transport: object, store: object, *, now: object) -> object:
            assert transport is event_transport
            assert store == "credential-store"
            assert callable(now)
            return event_client

    @contextmanager
    def source_context(**kwargs: object):  # type: ignore[no-untyped-def]
        assert kwargs["provider"] == "spotify"
        assert kwargs["values"] == {"SPOTIFY_CLIENT_ID": "public-client"}
        yield source

    class FakeMusicBrainzTransport:
        def __init__(self, **kwargs: object) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeMusicBrainzSource:
        def __init__(self, **kwargs: object) -> None:
            pass

        def close(self) -> None:
            pass

    def create_server(actual_application: object, *, refresh: Callable[[str], object]) -> _Server:
        assert actual_application is application
        for kind in ("events", "catalog", "releases", "all"):
            refresh(kind)
        return server

    def run_refresh(actual_application: object, **kwargs: object) -> object:
        assert actual_application is application
        refresh_calls.append(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(mcp_stdio.Catalog, "open", lambda path: path)
    monkeypatch.setattr(mcp_stdio, "MusicFriendApplication", lambda _catalog: application)
    monkeypatch.setattr(mcp_stdio, "TicketmasterTransport", lambda _connector: event_transport)
    monkeypatch.setattr(mcp_stdio, "TicketmasterDiscoveryClient", EventClientFactory)
    monkeypatch.setattr(mcp_stdio, "spotify_source", source_context)
    monkeypatch.setattr(mcp_stdio, "MusicBrainzTransport", FakeMusicBrainzTransport)
    monkeypatch.setattr(mcp_stdio, "MusicBrainzSource", FakeMusicBrainzSource)
    monkeypatch.setattr(mcp_stdio, "refresh_once", run_refresh)
    monkeypatch.setattr(mcp_stdio, "create_music_server", create_server)
    mcp_stdio.run_catalog_stdio_session(
        config=mcp_stdio.LocalConfig(spotify_client_id="public-client"),
        catalog_path=tmp_path / "catalog.sqlite3",
        connector_factory=lambda: object(),
        credential_store_factory=lambda: "credential-store",  # type: ignore[arg-type]
    )

    assert server.transports == ["stdio"]
    assert event_transport.closes == 1
    assert application_closes == [application]
    assert [call["kind"] for call in refresh_calls] == ["events", "catalog", "releases", "all"]
    events_call, catalog_call, releases_call, all_call = refresh_calls
    # events: no source at all.
    assert events_call["source"] is None
    # catalog: the (fake) Spotify source, no distinct release_source since releases
    # doesn't run for this kind.
    assert catalog_call["source"] is source
    assert catalog_call["release_source_name"] is None
    # releases (default release_source=musicbrainz): no Spotify token session opened
    # at all -- source is None, and release_source is a real (unfaked in this test,
    # but never invoked since refresh_once itself is faked) MusicBrainzSource.
    assert releases_call["source"] is None
    assert releases_call["release_source_name"] == "musicbrainz"
    assert isinstance(releases_call["release_source"], mcp_stdio.MusicBrainzSource)
    # all: catalog still uses the Spotify source; releases uses a distinct
    # MusicBrainz release_source.
    assert all_call["source"] is source
    assert all_call["release_source_name"] == "musicbrainz"
    assert isinstance(all_call["release_source"], mcp_stdio.MusicBrainzSource)
    assert all_call["release_source"] is not all_call["source"]


def test_mcp_refresh_releases_uses_musicbrainz_and_never_opens_a_spotify_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC: the MCP refresh_music path, driven through
    mcp_stdio.run_catalog_stdio_session's own refresh() closure (not just
    refresh_once directly), reaches MusicBrainz and never opens a Spotify
    token session for a musicbrainz-only releases refresh."""
    import httpx

    from music_friend.domain import (
        Artist,
        IdentityConfidence,
        SourceReference,
        WatchlistAction,
        WatchlistOverride,
    )
    from music_friend.store import Catalog
    from music_friend.tools import MusicFriendApplication

    NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    catalog_path = tmp_path / "catalog.sqlite3"
    seed_application = MusicFriendApplication(Catalog.open(catalog_path))
    seed_application.put_artist(
        Artist(
            "artist-1",
            "Artist One",
            (SourceReference("spotify", "artist-native", None, NOW),),
            IdentityConfidence.SOURCE_ONLY,
            NOW,
        )
    )
    seed_application.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, NOW))
    seed_application.close()

    musicbrainz_calls: list[httpx.Request] = []

    def _musicbrainz_response(request: httpx.Request) -> httpx.Response:
        musicbrainz_calls.append(request)
        assert request.url.host == "musicbrainz.org"
        return httpx.Response(200, json={"urls": [], "url-count": 0, "url-offset": 0})

    def connector_factory() -> httpx.BaseTransport:
        return httpx.MockTransport(_musicbrainz_response)

    @contextmanager
    def _refuse_spotify_source(**kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("a musicbrainz-only releases refresh must not open Spotify")
        yield  # pragma: no cover

    refresh_results: list[object] = []

    def create_server(actual_application: object, *, refresh: Callable[[str], object]) -> _Server:
        refresh_results.append(refresh("releases"))
        return _Server()

    class _EventStore:
        def save(self, _key: object, _value: str) -> None:
            raise AssertionError("unused")

        def load(self, _key: object) -> str | None:
            return None

        def delete(self, _key: object) -> None:
            raise AssertionError("unused")

    monkeypatch.setattr(mcp_stdio, "spotify_source", _refuse_spotify_source)
    monkeypatch.setattr(mcp_stdio, "create_music_server", create_server)

    mcp_stdio.run_catalog_stdio_session(
        config=mcp_stdio.LocalConfig(),
        catalog_path=catalog_path,
        connector_factory=connector_factory,
        credential_store_factory=lambda: _EventStore(),  # type: ignore[arg-type]
    )

    assert len(refresh_results) == 1
    result = refresh_results[0]
    assert result.run is not None  # type: ignore[union-attr]
    assert result.run.status.value == "succeeded"  # type: ignore[union-attr]
    assert musicbrainz_calls


def test_mcp_refresh_releases_calls_musicbrainz_and_deezer_but_never_spotify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC (issue #42): the MCP refresh_music path actually reaches BOTH
    musicbrainz and deezer hosts when release_sources=("musicbrainz", "deezer"),
    and never opens a Spotify token session, driven through
    mcp_stdio.run_catalog_stdio_session's own refresh() closure."""
    import httpx

    from music_friend.domain import (
        Artist,
        IdentityConfidence,
        SourceReference,
        WatchlistAction,
        WatchlistOverride,
    )
    from music_friend.store import Catalog
    from music_friend.tools import MusicFriendApplication

    NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    catalog_path = tmp_path / "catalog.sqlite3"
    synthetic_mbid = "22222222-2222-2222-2222-222222222222"
    synthetic_deezer_artist_id = "424242"
    seed_application = MusicFriendApplication(Catalog.open(catalog_path))
    seed_application.put_artist(
        Artist(
            "artist-1",
            "Artist One",
            (
                SourceReference("spotify", "artist-native", None, NOW),
                SourceReference("musicbrainz", synthetic_mbid, None, NOW),
            ),
            IdentityConfidence.SOURCE_ONLY,
            NOW,
        )
    )
    seed_application.put_watchlist_override(WatchlistOverride("artist-1", WatchlistAction.ADD, NOW))
    seed_application.close()

    musicbrainz_calls: list[httpx.Request] = []
    deezer_calls: list[httpx.Request] = []

    def _dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "musicbrainz.org":
            musicbrainz_calls.append(request)
            if request.url.path == "/ws/2/release-group":
                return httpx.Response(200, json={"release-groups": []})
            return httpx.Response(
                200,
                json={
                    "relations": [
                        {
                            "type": "free streaming",
                            "url": {
                                "resource": (
                                    f"https://www.deezer.com/artist/{synthetic_deezer_artist_id}"
                                )
                            },
                        }
                    ]
                },
            )
        if request.url.host == "api.deezer.com":
            deezer_calls.append(request)
            return httpx.Response(200, json={"data": [], "total": 0})
        raise AssertionError(f"unexpected host: {request.url.host}")

    def connector_factory() -> httpx.BaseTransport:
        return httpx.MockTransport(_dispatch)

    @contextmanager
    def _refuse_spotify_source(**kwargs: object):  # type: ignore[no-untyped-def]
        raise AssertionError("spotify must not be opened when it is not a configured source")
        yield  # pragma: no cover

    refresh_results: list[object] = []

    def create_server(actual_application: object, *, refresh: Callable[[str], object]) -> _Server:
        refresh_results.append(refresh("releases"))
        return _Server()

    class _EventStore:
        def save(self, _key: object, _value: str) -> None:
            raise AssertionError("unused")

        def load(self, _key: object) -> str | None:
            return None

        def delete(self, _key: object) -> None:
            raise AssertionError("unused")

    monkeypatch.setattr(mcp_stdio, "spotify_source", _refuse_spotify_source)
    monkeypatch.setattr(mcp_stdio, "create_music_server", create_server)

    mcp_stdio.run_catalog_stdio_session(
        config=mcp_stdio.LocalConfig(release_sources=("musicbrainz", "deezer")),
        catalog_path=catalog_path,
        connector_factory=connector_factory,
        credential_store_factory=lambda: _EventStore(),  # type: ignore[arg-type]
    )

    assert len(refresh_results) == 1
    result = refresh_results[0]
    assert result.run is not None  # type: ignore[union-attr]
    assert result.run.status.value == "succeeded"  # type: ignore[union-attr]
    assert musicbrainz_calls
    assert deezer_calls


def test_catalog_stdio_session_starts_without_an_available_native_credential_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A headless Linux session remains usable for local status before credentials exist."""
    application = _CloseRecorder()
    event_transport = _CloseRecorder()
    server = _Server()

    class EventClientFactory:
        def __new__(cls, _transport: object, store: object, *, now: object) -> object:
            assert callable(now)
            with pytest.raises(CredentialStoreError):
                store.load(CredentialKey("ticketmaster", "discovery"))  # type: ignore[attr-defined]
            return object()

    def unavailable_store() -> object:
        raise CredentialStoreError()

    monkeypatch.setattr(mcp_stdio.Catalog, "open", lambda path: path)
    monkeypatch.setattr(mcp_stdio, "MusicFriendApplication", lambda _catalog: application)
    monkeypatch.setattr(mcp_stdio, "TicketmasterTransport", lambda _connector: event_transport)
    monkeypatch.setattr(mcp_stdio, "TicketmasterDiscoveryClient", EventClientFactory)
    monkeypatch.setattr(mcp_stdio, "create_music_server", lambda _application, refresh: server)

    mcp_stdio.run_catalog_stdio_session(
        config=mcp_stdio.LocalConfig(),
        catalog_path=tmp_path / "catalog.sqlite3",
        connector_factory=object,
        credential_store_factory=unavailable_store,  # type: ignore[arg-type]
    )

    assert server.transports == ["stdio"]
    assert event_transport.closes == 1
    assert application.closes == 1


@pytest.mark.parametrize(
    ("config", "path"),
    ((object(), Path("catalog.sqlite3")), (mcp_stdio.LocalConfig(), "catalog.sqlite3")),
)
def test_catalog_stdio_session_rejects_invalid_local_configuration(
    config: object, path: object
) -> None:
    with pytest.raises(ValueError, match="local MCP configuration is invalid"):
        mcp_stdio.run_catalog_stdio_session(  # type: ignore[arg-type]
            config=config,
            catalog_path=path,
        )


def _write_request(process: subprocess.Popen[str], request: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_response(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=5):
            raise TimeoutError("MCP stdio response exceeded the fixed timeout")
    line = process.stdout.readline()
    if not line:
        raise ValueError("MCP stdio closed before responding")
    result = json.loads(line)
    assert isinstance(result, dict)
    return result


@contextmanager
def _catalog_stdio_process(catalog_path: Path) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("catalog_stdio_fixture.py")),
            str(catalog_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path.cwd(),
    )
    try:
        yield process
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def test_catalog_stdio_process_cleans_up_when_flow_raises(tmp_path: Path) -> None:
    """Catches an intermediate RPC failure leaking the child or any owned pipe."""
    process: subprocess.Popen[str] | None = None

    with pytest.raises(RuntimeError, match="synthetic flow failure"):
        with _catalog_stdio_process(tmp_path / "catalog.sqlite3") as running:
            process = running
            raise RuntimeError("synthetic flow failure")

    assert process is not None
    assert process.poll() is not None
    assert process.stdin is not None and process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_catalog_stdio_entrypoint_runs_the_full_local_refresh_and_inbox_flow(
    tmp_path: Path,
) -> None:
    """Catches a normal stdio entrypoint that cannot drive the actual local catalog lifecycle."""
    with _catalog_stdio_process(tmp_path / "catalog.sqlite3") as process:
        _assert_catalog_stdio_flow(process)


def _assert_catalog_stdio_flow(process: subprocess.Popen[str]) -> None:

    def call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        _write_request(
            process,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        return _read_response(process)

    _write_request(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-test", "version": "1.0"},
            },
        },
    )
    assert _read_response(process)["id"] == 1
    _write_request(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    status = call(2, "music_status", {})
    catalog = call(3, "refresh_music", {"kind": "catalog"})
    watchlist = call(4, "list_watchlist", {"limit": 10})
    releases = call(5, "refresh_music", {"kind": "releases"})
    events = call(6, "refresh_music", {"kind": "events"})
    refreshed = call(7, "refresh_music", {"kind": "all"})
    inbox = call(8, "list_inbox", {"limit": 10})
    items = inbox["result"]["structuredContent"]["items"]  # type: ignore[index]
    assert isinstance(items, list) and items
    inbox_id = items[0]["local_id"]
    assert isinstance(inbox_id, str)
    updated = call(9, "update_inbox_item", {"inbox_id": inbox_id, "state": "saved"})
    explained = call(10, "explain_inbox_item", {"inbox_id": inbox_id})

    assert process.stdin is not None
    process.stdin.close()
    process.wait(timeout=5)
    assert process.stdout is not None and process.stderr is not None
    remaining_stdout = process.stdout.read()
    stderr = process.stderr.read()

    assert process.returncode == 0
    assert stderr == ""
    assert remaining_stdout == ""
    assert status["result"]["structuredContent"]["status"] == "ready"  # type: ignore[index]
    for label, result in (
        ("catalog", catalog),
        ("releases", releases),
        ("events", events),
        ("refreshed", refreshed),
    ):
        assert result["result"]["structuredContent"]["status"] == "succeeded", (label, result)  # type: ignore[index]
    watched = watchlist["result"]["structuredContent"]["items"]  # type: ignore[index]
    assert watched == [
        {
            "affinity": {"saved_track_count": 0, "total_points": 100},
            "artist": {
                "display_name": "Artist One",
                "identity_confidence": "source_only",
                "local_id": "artist-1",
            },
            "inclusion_reason": "automatic",
            "release_source_status": "unmapped",
        }
    ]
    assert updated["result"]["structuredContent"]["state"] == "saved"  # type: ignore[index]
    assert explained["result"]["structuredContent"]["entry"]["local_id"] == inbox_id  # type: ignore[index]
