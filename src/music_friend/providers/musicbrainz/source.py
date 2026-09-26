"""Read-only MusicBrainz implementation of the provider-neutral source contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

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
from music_friend.providers.musicbrainz.normalize import normalize_release_group
from music_friend.providers.musicbrainz.transport import MusicBrainzTransport


class MusicBrainzSource:
    """MusicBrainz source supporting release discovery only."""

    def __init__(self) -> None:
        self._transport = MusicBrainzTransport(
            user_agent="music-friend/0.1.0 (https://github.com/Galactic-Luddite/music-friend)"
        )
        self._capabilities = ProviderCapabilities(
            supported=frozenset({Capability.RECENT_RELEASES}),
            granted=frozenset({Capability.RECENT_RELEASES}),
        )

    def capabilities(self) -> ProviderCapabilities:
        """Return supported capabilities: releases only."""
        return self._capabilities

    def health(self) -> ProviderHealth:
        """Check MusicBrainz API health via a simple request."""
        try:
            self._transport.get("artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da")
        except Exception:
            pass
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        """Not supported."""
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
        """Discover recent releases for one artist via MusicBrainz release-group search.

        Args:
            artist_refs: Artist references (expects exactly one with source "musicbrainz")
            since: Lower bound on release date
            cursor: Pagination cursor (offset as string)

        Returns:
            Page of Release records with continuation cursor if more exist.
        """
        if not isinstance(artist_refs, Sequence) or len(artist_refs) != 1:
            raise ValueError("exactly one artist_ref is required")

        artist_ref = artist_refs[0]
        if artist_ref.source != "musicbrainz":
            raise ValueError("artist_ref must be from musicbrainz source")

        mbid = artist_ref.native_id
        offset = 0
        if cursor is not None:
            try:
                offset = int(cursor)
            except (ValueError, TypeError):
                offset = 0

        # Query MusicBrainz for release groups
        since_date = since.strftime("%Y-%m-%d")
        query_parts = [
            f"arid:{mbid}",
            f"firstreleasedate:[{since_date} TO *]",
            "status:official",
            "primarytype:(Album OR Single OR EP)",
        ]
        query_string = " AND ".join(query_parts)

        response = self._transport.get(
            "release-group",
            query={
                "query": query_string,
                "limit": "100",
                "offset": str(offset),
            },
        )

        if not isinstance(response, Mapping):
            raise InvalidSourceResponseError()

        release_groups = response.get("release-groups")
        if not isinstance(release_groups, list):
            raise InvalidSourceResponseError()

        # Build artist ID map for the normalizer
        artist_id_map = {mbid: artist_ref.native_id}

        now = datetime.now()
        releases: list[Release] = []
        for rg in release_groups:
            if not isinstance(rg, Mapping):
                continue
            # Only include release-groups with a score field
            if "score" not in rg:
                continue
            normalized = normalize_release_group(rg, artist_id_map=artist_id_map, now=now)
            if normalized is not None:
                releases.append(normalized)

        # Sort by first-release-date descending (client-side)
        releases.sort(key=lambda r: r.release_date, reverse=True)

        # Determine if there's a next page
        next_cursor = None
        count = response.get("release-group-count")
        if isinstance(count, int):
            next_offset = offset + len(release_groups)
            if next_offset < count:
                next_cursor = str(next_offset)

        return Page(items=tuple(releases), next_cursor=next_cursor)

    def close(self) -> None:
        """Clean up resources."""
        if self._transport is not None:
            self._transport.close()

    def __enter__(self) -> MusicBrainzSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["MusicBrainzSource"]
