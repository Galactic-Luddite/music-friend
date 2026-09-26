"""Synthetic executable fixture for the catalog MCP stdio runtime test."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from catalog_stdio_guard import FixtureGuard

CATALOG_PATH = Path(sys.argv[1])
FIXTURE_GUARD = FixtureGuard(CATALOG_PATH)
RESTORE_GUARD = FIXTURE_GUARD.install()
FIXTURE_GUARD.assert_restrictions()


import httpx  # noqa: E402

from music_friend.configuration import LocalConfig  # noqa: E402
from music_friend.domain import (  # noqa: E402
    Artist,
    CatalogItemBatch,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)  # noqa: E402
from music_friend.providers import (  # noqa: E402
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)  # noqa: E402
from music_friend.providers.ticketmaster import (  # noqa: E402
    TicketmasterAttraction,
    TicketmasterEvent,
)
from music_friend.runtimes import mcp_stdio  # noqa: E402

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class Credentials:
    def save(self, key: object, value: object) -> None:
        raise AssertionError("unused")

    def load(self, key: object) -> None:
        return None

    def delete(self, key: object) -> None:
        raise AssertionError("unused")


class Source:
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supported=frozenset(Capability), granted=frozenset(Capability))

    def health(self) -> ProviderHealth:
        capabilities = self.capabilities()
        return ProviderHealth(HealthStatus.HEALTHY, capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        return Page((), None)

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        return Page(
            (
                Artist(
                    "artist-1",
                    "Artist One",
                    (SourceReference("spotify", "artist-native", None, NOW),),
                    IdentityConfidence.SOURCE_ONLY,
                    NOW,
                ),
            ),
            None,
        )

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        return CatalogItemBatch((), (), None)

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        return CatalogItemBatch((), (), None)

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        return Page((), None)

    def recent_releases(
        self,
        artist_refs: tuple[SourceReference, ...],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        return Page(
            (
                Release(
                    "release-1",
                    "Release One",
                    "album",
                    date(2026, 9, 1),
                    ReleaseDatePrecision.DAY,
                    ("artist-1",),
                    (SourceReference("spotify", "release-native", None, NOW),),
                    NOW,
                ),
            ),
            None,
        )


class Events:
    def is_configured(self) -> bool:
        return True

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]:
        return (TicketmasterAttraction("attraction-1", artist_name),)

    def events_for_attraction(
        self, attraction: TicketmasterAttraction, config: LocalConfig
    ) -> tuple[TicketmasterEvent, ...]:
        return (
            TicketmasterEvent(
                "event-1",
                "Artist One Live",
                NOW,
                "minute",
                "Venue",
                "City",
                "https://example.test/event",
                "https://example.test/tickets",
                "Ticketmaster",
            ),
        )


@contextmanager
def source_context(**kwargs: object):
    yield Source()


class FakeMusicBrainzSource:
    """Synthetic musicbrainz release source: no real network, no keyless API call."""

    def __init__(self, *, transport: object = None, clock: object = None) -> None:
        pass

    def close(self) -> None:
        pass

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported=frozenset({Capability.RECENT_RELEASES}),
            granted=frozenset({Capability.RECENT_RELEASES}),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self.capabilities())

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        raise AssertionError("unused")

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        raise AssertionError("unused")

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        raise AssertionError("unused")

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def recent_releases(
        self,
        artist_refs: tuple[SourceReference, ...],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        return Page((), None)

    def lookup_artists_by_spotify_urls(self, spotify_urls: object) -> dict[str, str | None]:
        return {url: None for url in spotify_urls}  # type: ignore[union-attr]

    def search_artist_by_name(self, name: str, limit: int = 3) -> list[dict[str, object]]:
        return []


def main() -> None:
    mcp_stdio.spotify_source = source_context  # type: ignore[assignment]
    mcp_stdio.TicketmasterDiscoveryClient = lambda *args, **kwargs: Events()  # type: ignore[assignment]
    mcp_stdio.MusicBrainzSource = FakeMusicBrainzSource  # type: ignore[misc]
    try:
        mcp_stdio.run_catalog_stdio_session(
            config=LocalConfig(
                spotify_client_id="synthetic-client",
                event_country_code="US",
                event_postal_code="94103",
                event_radius=50,
                event_radius_unit="miles",
            ),
            catalog_path=CATALOG_PATH,
            connector_factory=lambda: httpx.MockTransport(lambda request: httpx.Response(500)),
            credential_store_factory=Credentials,  # type: ignore[arg-type]
        )
    finally:
        RESTORE_GUARD()


if __name__ == "__main__":
    main()
