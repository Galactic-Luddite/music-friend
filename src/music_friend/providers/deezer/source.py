"""Read-only Deezer implementation of the provider-neutral source contract.

Deezer artist ids are never resolved by name search here (see the release-source
design doc, section 4): they arrive exclusively as ``SourceReference(source="deezer",
...)`` entries produced by the MusicBrainz identity-mapping pass reading an
artist's ``https://www.deezer.com/artist/<id>`` url-rel. An artist with no such
url-rel is simply uncovered by Deezer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Protocol

from music_friend.domain import (
    Artist,
    CatalogItemBatch,
    Release,
    SourceReference,
)
from music_friend.errors import CapabilityUnsupportedError, InvalidSourceResponseError
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)
from music_friend.providers.deezer.normalize import normalize_album
from music_friend.providers.deezer.transport import DeezerTransport

_MAX_ALBUMS_PER_PAGE = 100


class _Transport(Protocol):
    """The subset of DeezerTransport this source depends on, for test injection."""

    def get(self, path: str, query: Mapping[str, str] | None = None) -> object: ...

    def close(self) -> None: ...


class DeezerSource:
    """Deezer source supporting release discovery only, from a known artist id."""

    def __init__(
        self,
        *,
        transport: _Transport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport: _Transport = transport if transport is not None else DeezerTransport()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._capabilities = ProviderCapabilities(
            supported=frozenset({Capability.RECENT_RELEASES}),
            granted=frozenset({Capability.RECENT_RELEASES}),
        )

    def capabilities(self) -> ProviderCapabilities:
        """Return supported capabilities: releases only."""
        return self._capabilities

    def health(self) -> ProviderHealth:
        """Report Deezer as healthy without making a network request.

        Deezer is keyless and has no credential state worth probing here; an
        unreachable service surfaces through ``RateLimitedError`` or
        ``InvalidSourceResponseError`` on ``recent_releases``, which must
        propagate rather than being swallowed.
        """
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        """Not supported: Deezer artist ids come only from MusicBrainz url-rels."""
        raise CapabilityUnsupportedError()

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        """Not supported."""
        raise CapabilityUnsupportedError()

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        """Not supported."""
        raise CapabilityUnsupportedError()

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        """Not supported."""
        raise CapabilityUnsupportedError()

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        """Not supported."""
        raise CapabilityUnsupportedError()

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        """Discover recent releases for one artist via ``/artist/{id}/albums``.

        Args:
            artist_refs: Artist references (expects exactly one with source "deezer")
            since: Lower bound on release date; applied client-side, matching
                Deezer's documented API (no server-side date filter exists)
            cursor: Pagination cursor (offset/index as a string)

        Returns:
            A page of normalized releases with ``release_date >= since``.
        """
        artist_ref = _single_deezer_ref(artist_refs)
        index = _parse_cursor(cursor)
        response = self._transport.get(
            f"artist/{artist_ref.native_id}/albums",
            query={"limit": str(_MAX_ALBUMS_PER_PAGE), "index": str(index)},
        )
        if not isinstance(response, Mapping):
            raise InvalidSourceResponseError("deezer albums response must be an object")
        albums = response.get("data")
        if not isinstance(albums, list):
            raise InvalidSourceResponseError("deezer albums response missing data")

        now = self._clock()
        releases: list[Release] = []
        for album in albums:
            if not isinstance(album, Mapping):
                continue
            release = normalize_album(album, artist_native_id=artist_ref.native_id, now=now)
            if release is None:
                continue
            if release.release_date < _as_date(since):
                continue
            releases.append(release)

        has_next = isinstance(response.get("next"), str) and bool(response.get("next"))
        next_cursor = str(index + len(albums)) if has_next and albums else None
        return Page(items=tuple(releases), next_cursor=next_cursor)


def _single_deezer_ref(artist_refs: Sequence[SourceReference]) -> SourceReference:
    matches = tuple(ref for ref in artist_refs if ref.source == "deezer")
    if len(matches) != 1:
        raise ValueError("recent_releases requires exactly one deezer artist reference")
    return matches[0]


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        parsed = int(cursor)
    except ValueError as error:
        raise InvalidSourceResponseError("deezer cursor is invalid") from error
    if parsed < 0:
        raise InvalidSourceResponseError("deezer cursor is invalid")
    return parsed


def _as_date(value: datetime) -> date:
    return value.date()


__all__ = ["DeezerSource"]
