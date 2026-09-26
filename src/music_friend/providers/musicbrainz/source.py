"""Read-only MusicBrainz implementation of the provider-neutral source contract."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Protocol

from music_friend import __version__
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

#: Mandatory, meaningful User-Agent for the keyless MusicBrainz API.
_DEFAULT_USER_AGENT = (
    f"music-friend/{__version__} (https://github.com/Galactic-Luddite/music-friend)"
)

_DEEZER_ARTIST_URL = re.compile(r"https://www\.deezer\.com/artist/(\d+)")


def _url_entries(response: object) -> dict[str, Mapping[str, object]]:
    """Map each resolved URL to its response entry.

    Verified live against the real MusicBrainz API (2026-09): a batch
    ``/ws/2/url?resource=...&inc=artist-rels`` call (``resource`` repeated, even
    for a single URL, to always take this response shape and avoid the
    unwrapped single-resource form and its 404-on-unknown-URL behavior) returns
    ``{"url-count": N, "url-offset": 0, "urls": [{"resource": "<url>",
    "relations": [...]}, ...]}``. A URL with no artist relation is simply
    OMITTED from ``urls`` entirely -- not an error, not a null entry -- so
    "unresolved" is computed by the caller as (requested URLs) minus (URLs
    present in this map), never by looking for an error or an empty entry per
    input.
    """
    if not isinstance(response, Mapping):
        return {}
    urls = response.get("urls")
    if not isinstance(urls, list):
        return {}
    entries: dict[str, Mapping[str, object]] = {}
    for entry in urls:
        if not isinstance(entry, Mapping):
            continue
        resource = entry.get("resource")
        if isinstance(resource, str):
            entries[resource] = entry
    return entries


def _single_artist_relation_mbid(entry: Mapping[str, object]) -> str | None:
    """Return the MBID for a resolved URL entry only when it has exactly one
    artist relation.

    ``inc=artist-rels`` scopes ``relations`` to artist relations server-side, so
    every entry here should carry an ``artist`` object; a relation is accepted
    when it has one regardless of whether it also carries a ``target-type``
    field (unverified: live reports have disagreed on whether ``target-type``
    is present at all), but a relation that DOES carry an explicit
    ``target-type`` other than ``"artist"`` is excluded defensively. A URL
    resolving to more than one distinct artist id is ambiguous and must fall
    back to name search rather than guess.
    """
    relations = entry.get("relations")
    if not isinstance(relations, list):
        return None
    artist_ids: set[str] = set()
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        target_type = relation.get("target-type")
        if target_type is not None and target_type != "artist":
            continue
        artist = relation.get("artist")
        if isinstance(artist, Mapping):
            artist_id = artist.get("id")
            if isinstance(artist_id, str):
                artist_ids.add(artist_id)
    if len(artist_ids) == 1:
        return next(iter(artist_ids))
    return None


class _Transport(Protocol):
    """The subset of MusicBrainzTransport this source depends on, for test injection."""

    def get(self, path: str, query: Mapping[str, str | list[str]] | None = None) -> object: ...

    def close(self) -> None: ...


class MusicBrainzSource:
    """MusicBrainz source supporting release discovery only."""

    def __init__(
        self,
        *,
        transport: _Transport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport: _Transport = (
            transport
            if transport is not None
            else MusicBrainzTransport(user_agent=_DEFAULT_USER_AGENT)
        )
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
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

        Always calls ``/ws/2/url?resource=...&inc=artist-rels`` with the
        ``resource`` parameter repeated (even for a single URL) so the response
        is always the batch ``{"urls": [...]}`` shape and never the unwrapped
        single-resource shape, which 404s on an unknown URL. Up to 100
        ``resource`` parameters per call, chunking larger inputs. A URL is
        mapped to an MBID only when it carries exactly one artist relation; a
        URL with zero or more than one artist relation, or that is simply
        omitted from the response (an unknown URL, per verified live API
        behavior), maps to ``None`` so the caller can fall back to name search.
        ``RateLimitedError`` and ``InvalidSourceResponseError`` from the
        transport propagate unchanged so pacing/cooldown accounting stays
        correct.
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
            entries = _url_entries(response)
            for url in batch:
                entry = entries.get(url)
                resolved[url] = None if entry is None else _single_artist_relation_mbid(entry)
        return resolved

    def deezer_artist_id(self, mbid: str) -> str | None:
        """Resolve one MusicBrainz artist's Deezer id from its url-rels (issue #42).

        Calls ``GET /ws/2/artist/{mbid}?inc=url-rels``. Verified live against the
        real MusicBrainz API on 2026-09-26: a public artist's ``relations`` list
        includes entries shaped ``{"type": "free streaming", "url": {"resource":
        "https://www.deezer.com/artist/<id>"}}`` alongside Spotify and Apple Music
        links (73 total relations observed for one artist, including two distinct
        Deezer artist links). Only one distinct Deezer id is accepted: an artist
        with zero or with more than one distinct Deezer url-rel returns ``None``
        rather than guessing, matching the ambiguity rule used for the Spotify-url
        batch lookup above. There is no Deezer name-search fallback (see the
        release-source design doc, section 4): an artist with no such url-rel is
        simply uncovered by Deezer.
        """
        if not isinstance(mbid, str) or not mbid:
            raise ValueError("mbid must be a non-empty string")
        response = self._transport.get(f"artist/{mbid}", query={"inc": "url-rels"})
        if not isinstance(response, Mapping):
            raise InvalidSourceResponseError("musicbrainz artist lookup response must be an object")
        relations = response.get("relations")
        if not isinstance(relations, list):
            raise InvalidSourceResponseError("musicbrainz artist lookup response missing relations")
        deezer_ids: set[str] = set()
        for relation in relations:
            if not isinstance(relation, Mapping):
                continue
            url = relation.get("url")
            if not isinstance(url, Mapping):
                continue
            resource = url.get("resource")
            if not isinstance(resource, str):
                continue
            match = _DEEZER_ARTIST_URL.fullmatch(resource)
            if match is not None:
                deezer_ids.add(match.group(1))
        if len(deezer_ids) == 1:
            return next(iter(deezer_ids))
        return None

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

        now = self._now()
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

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock result must be timezone-aware")
        return value


__all__ = ["MusicBrainzSource"]
