"""On-demand local stdio runtime for Music Friend MCP reads."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from platformdirs import user_data_path

from music_friend.configuration import LocalConfig, LocalConfigStore
from music_friend.mcp import create_music_server
from music_friend.mcp.read_server import create_read_server
from music_friend.providers import MusicSource
from music_friend.providers.credentials import CredentialStore
from music_friend.providers.keyring_store import KeyringCredentialStore
from music_friend.providers.spotify.config import SpotifySettings, load_spotify_settings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport
from music_friend.providers.ticketmaster import TicketmasterDiscoveryClient
from music_friend.providers.ticketmaster.transport import TicketmasterTransport
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import refresh_once

_SPOTIFY_ENVIRONMENT_KEYS = ("SPOTIFY_CLIENT_ID", "SPOTIFY_REDIRECT_URI")
_MISSING = object()

ConnectorFactory = Callable[[], httpx.BaseTransport]
CredentialStoreFactory = Callable[[], CredentialStore]
TransportFactory = Callable[[httpx.BaseTransport, Callable[[], float]], SpotifyTransport]
TokenManagerFactory = Callable[
    [SpotifySettings, SpotifyTransport, CredentialStore, Callable[[], float]], SpotifyTokenManager
]
SourceFactory = Callable[
    [SpotifySettings, SpotifyTokenManager, Callable[[], datetime]], SpotifySource
]
ServerFactory = Callable[[MusicSource], MCPServer]


def _default_connector() -> httpx.BaseTransport:
    return httpx.HTTPTransport(trust_env=False)


def spotify_environment(values: Mapping[str, object]) -> dict[str, object]:
    """Copy the only two Spotify settings the local runtime may read."""
    mapped: dict[str, object] = {}
    for key in _SPOTIFY_ENVIRONMENT_KEYS:
        value = values.get(key, _MISSING)
        if value is not _MISSING:
            mapped[key] = value
    return mapped


@contextmanager
def spotify_source(
    *,
    provider: str | None,
    values: Mapping[str, object],
    connector_factory: ConnectorFactory = _default_connector,
    credential_store_factory: CredentialStoreFactory = KeyringCredentialStore,
    token_clock: Callable[[], float] = time.monotonic,
    source_clock: Callable[[], datetime] | None = None,
    transport_factory: TransportFactory | None = None,
    token_manager_factory: TokenManagerFactory | None = None,
    source_factory: SourceFactory | None = None,
) -> Iterator[MusicSource]:
    """Compose one Spotify source and always close its owned HTTP transport."""
    _require_spotify(provider)
    settings = load_spotify_settings(spotify_environment(values))
    actual_source_clock = _utc_now if source_clock is None else source_clock
    connector = connector_factory()
    transport = (
        SpotifyTransport(connector, clock=token_clock)
        if transport_factory is None
        else transport_factory(connector, token_clock)
    )
    try:
        store = credential_store_factory()
        tokens = (
            SpotifyTokenManager(
                settings=settings,
                transport=transport,
                store=store,
                clock=token_clock,
            )
            if token_manager_factory is None
            else token_manager_factory(settings, transport, store, token_clock)
        )
        source = (
            SpotifySource(settings=settings, tokens=tokens, clock=actual_source_clock)
            if source_factory is None
            else source_factory(settings, tokens, actual_source_clock)
        )
        yield source
    finally:
        transport.close()


def run_stdio_session(
    *,
    provider: str | None,
    values: Mapping[str, object],
    connector_factory: ConnectorFactory = _default_connector,
    credential_store_factory: CredentialStoreFactory = KeyringCredentialStore,
    token_clock: Callable[[], float] = time.monotonic,
    source_clock: Callable[[], datetime] | None = None,
    transport_factory: TransportFactory | None = None,
    token_manager_factory: TokenManagerFactory | None = None,
    source_factory: SourceFactory | None = None,
    server_factory: ServerFactory = create_read_server,
) -> None:
    """Run one bounded public MCP stdio session."""
    with spotify_source(
        provider=provider,
        values=values,
        connector_factory=connector_factory,
        credential_store_factory=credential_store_factory,
        token_clock=token_clock,
        source_clock=source_clock,
        transport_factory=transport_factory,
        token_manager_factory=token_manager_factory,
        source_factory=source_factory,
    ) as source:
        server_factory(source).run("stdio")


def run_catalog_stdio_session(
    *,
    config: LocalConfig,
    catalog_path: Path,
    connector_factory: ConnectorFactory = _default_connector,
    credential_store_factory: CredentialStoreFactory = KeyringCredentialStore,
) -> None:
    """Run the normal local catalog MCP server over stdio only."""
    if type(config) is not LocalConfig or not isinstance(catalog_path, Path):
        raise ValueError("local MCP configuration is invalid")
    application = MusicFriendApplication(Catalog.open(catalog_path))
    event_transport = TicketmasterTransport(connector_factory())
    try:
        event_client = TicketmasterDiscoveryClient(
            event_transport,
            credential_store_factory(),
            now=_utc_now,
        )

        def refresh(kind: str) -> object:
            if kind == "events":
                return refresh_once(
                    application,
                    kind=kind,
                    source_name="spotify",
                    source=None,
                    config=config,
                    event_client=event_client,
                    checked_at=_utc_now(),
                    lock_path=catalog_path.with_name("refresh.lock"),
                )
            source_values = {
                "SPOTIFY_CLIENT_ID": config.spotify_client_id,
            }
            with spotify_source(
                provider="spotify",
                values=source_values,
                connector_factory=connector_factory,
                credential_store_factory=credential_store_factory,
            ) as source:
                return refresh_once(
                    application,
                    kind=kind,
                    source_name="spotify",
                    source=source,
                    config=config,
                    event_client=event_client,
                    checked_at=_utc_now(),
                    lock_path=catalog_path.with_name("refresh.lock"),
                )

        create_music_server(application, refresh=refresh).run("stdio")
    finally:
        event_transport.close()
        application.close()


def main() -> None:
    """Launch the catalog-backed local server only through the console entry point."""
    try:
        config = LocalConfigStore().load()
        run_catalog_stdio_session(
            config=config,
            catalog_path=Path(user_data_path("music-friend", appauthor=False)) / "catalog.sqlite3",
        )
    except Exception:
        print("Music Friend MCP could not start.", file=sys.stderr)
        raise SystemExit(1) from None


def _require_spotify(provider: str | None) -> None:
    if provider != "spotify":
        raise ValueError("A supported music provider must be selected.")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
