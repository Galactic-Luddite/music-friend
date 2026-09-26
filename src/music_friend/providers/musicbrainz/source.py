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

#: Maximum ``resource`` parameters per /ws/2/url batch call.
_URL_BATCH_SIZE = 100


def _single_artist_relation_mbid(response: object, url: str, batch_size: int) -> str | None:
    """Return the MBID for ``url`` only when it has exactly one artist relation.

    ``/ws/2/url`` with a single ``resource`` parameter returns one object shaped
    like the target URL's own relations; with multiple ``resource`` parameters
    it returns ``{"url-list": [...]}`` reporting the same shape per resource. Any
    other shape, or a URL with zero or more than one ``artist``-type relation, is
    ambiguous or unresolved and must fall back to name search rather than guess.
    """
    if not isinstance(response, Mapping):
        return None
    if batch_size == 1:
        candidate = response
    else:
        url_list = response.get("url-list")
        if not isinstance(url_list, list):
            return None
        candidate = None
        for entry in url_list:
            if isinstance(entry, Mapping) and entry.get("resource") == url:
                candidate = entry
                break
        if candidate is None:
            return None
    relations = candidate.get("relations") if isinstance(candidate, Mapping) else None
    if not isinstance(relations, list):
        return None
    artist_ids: set[str] = set()
    for relation in relations:
        if not isinstance(relation, Mapping) or relation.get("target-type") != "artist":
            continue
        artist = relation.get("artist")
        if isinstance(artist, Mapping):
            artist_id = artist.get("id")
            if isinstance(artist_id, str):
                artist_ids.add(artist_id)
    if len(artist_ids) == 1:
        return next(iter(artist_ids))
    return None


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
        """Report MusicBrainz as healthy without making a network request.

        MusicBrainz is keyless and has no credential or connectivity state worth
        probing here; an unreachable service surfaces through ``RateLimitedError``
        or ``InvalidSourceResponseError`` on the calls that matter (mapping,
        recent_releases), which must propagate rather than being swallowed.
        """
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        """Not supported."""
        raise CapabilityUnsupportedError()

    def lookup_artists_by_spotify_urls(self, spotify_urls: Sequence[str]) -> dict[str, str | None]:
        """Batch-resolve Spotify artist URLs to MusicBrainz artist IDs.

        Calls ``/ws/2/url?resource=...&inc=artist-rels`` with up to 100
        ``resource`` parameters per call, chunking larger inputs. A URL is
        mapped to an MBID only when it carries exactly one ``artist``-type
        relation; a URL with zero or more than one artist relation (or that is
        simply absent from the response) maps to ``None`` so the caller can
        fall back to name search. ``RateLimitedError`` and
        ``InvalidSourceResponseError`` from the transport propagate unchanged
        so pacing/cooldown accounting stays correct.
        """
        if not isinstance(spotify_urls, Sequence):
            raise ValueError("spotify_urls must be a sequence of URLs")
        deduped = list(dict.fromkeys(spotify_urls))
        resolved: dict[str, str | None] = {url: None for url in deduped}
        for start in range(0, len(deduped), _URL_BATCH_SIZE):
            batch = deduped[start : start + _URL_BATCH_SIZE]
            response = self._transport.get(
                "url",
                query={"resource": batch, "inc": "artist-rels"},
            )
            for url in batch:
                resolved[url] = _single_artist_relation_mbid(response, url, len(batch))
        return resolved

    def search_artist_by_name(self, name: str, limit: int = 3) -> list[dict[str, object]]:
        """Search for artists by name in MusicBrainz.

        Args:
            name: Artist name to search for
            limit: Maximum number of results to return

        Returns:
            List of dicts with 'id' and 'score' keys, sorted by score descending.
            Raises ``RateLimitedError``/``InvalidSourceResponseError`` unchanged.
        """
        if not isinstance(name, str) or not name.strip():
            return []
        response = self._transport.get(
            "artist",
            query={
                "query": f'artist:"{name}"',
                "limit": str(min(limit, 100)),
            },
        )
        if not isinstance(response, Mapping):
            raise InvalidSourceResponseError("musicbrainz artist search response must be an object")
        artists = response.get("artists")
        if not isinstance(artists, list):
            raise InvalidSourceResponseError("musicbrainz artist search response missing artists")
        scored: list[tuple[str, int]] = []
        for artist in artists:
            if not isinstance(artist, Mapping):
                continue
            artist_id = artist.get("id")
            score = artist.get("score")
            if isinstance(artist_id, str) and isinstance(score, int):
                scored.append((artist_id, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [{"id": artist_id, "score": score} for artist_id, score in scored[:limit]]

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
