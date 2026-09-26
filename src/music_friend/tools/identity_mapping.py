"""Identity mapping from Spotify artists to MusicBrainz identities.

Runs at the start of the ``releases`` refresh component whenever the
configured release source is not Spotify. Resolves each watchlisted artist's
MusicBrainz identity by batching Spotify-URL relation lookups first, falling
back to a name search for artists the batch call left unresolved, and
recording every attempt (mapped or unmapped) so unmapped artists are retried
only after ``MAPPING_RETRY_INTERVAL`` has elapsed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from music_friend.domain import DAILY_REFRESH_MINUTES, Artist, IdentityConfidence, SourceReference
from music_friend.errors import InvalidSourceResponseError, SourceUnavailableError
from music_friend.store import Catalog

#: Unmapped artists are retried no more often than once per this interval.
MAPPING_RETRY_INTERVAL = timedelta(minutes=7 * DAILY_REFRESH_MINUTES)

#: Minimum score for a name-search hit to be accepted as a mapping.
MIN_NAME_SEARCH_SCORE = 90

_SPOTIFY_ARTIST_URL = "https://open.spotify.com/artist/{spotify_id}"


class IdentityMappingSource(Protocol):
    """The MusicBrainz-specific mapping calls, satisfied by either a bare
    MusicBrainzSource or a _PacedSource wrapping one (the caller decides which,
    and the paced wrapper is preferred so mapping requests count toward the same
    pacing/cooldown budget as release discovery)."""

    def lookup_artists_by_spotify_urls(
        self, spotify_urls: Sequence[str]
    ) -> dict[str, str | None]: ...

    def search_artist_by_name(self, name: str, limit: int = 3) -> list[dict[str, object]]: ...

    def deezer_artist_id(self, mbid: str) -> str | None: ...


def run_identity_mapping(
    catalog: Catalog,
    source: IdentityMappingSource,
    source_name: str,
    checked_at: datetime,
) -> None:
    """Map watchlisted artists lacking a ``source_name`` identity.

    Artists already carrying a ``SourceReference`` for ``source_name`` are
    skipped. Unmapped artists whose most recent attempt is within
    ``MAPPING_RETRY_INTERVAL`` of ``checked_at`` are skipped too. A
    ``RateLimitedError`` from the transport propagates unchanged so the
    caller's paced source records the cooldown; mapping simply stops for this
    run rather than marking the remaining artists unmapped.
    """
    candidates = _artists_needing_mapping(catalog, source_name)
    eligible = [
        artist
        for artist in candidates
        if not _skip_for_retry_window(catalog, artist.local_id, source_name, checked_at)
    ]
    if source_name == "musicbrainz":
        _run_deezer_url_rel_mapping(catalog, source, checked_at)
    if not eligible:
        return

    url_by_artist: dict[str, str] = {}
    for artist in eligible:
        spotify_ref = _spotify_ref(artist)
        if spotify_ref is not None:
            url_by_artist[artist.local_id] = _SPOTIFY_ARTIST_URL.format(
                spotify_id=spotify_ref.native_id
            )

    url_hits: dict[str, str | None] = {}
    if url_by_artist:
        try:
            url_hits = source.lookup_artists_by_spotify_urls(tuple(url_by_artist.values()))
        except (InvalidSourceResponseError, SourceUnavailableError):
            # A malformed or unreachable batch response falls back to name search for
            # every eligible artist rather than aborting the whole mapping pass;
            # RateLimitedError is not caught here and propagates to the caller.
            url_hits = {}

    for artist in eligible:
        url = url_by_artist.get(artist.local_id)
        mbid = url_hits.get(url) if url is not None else None
        if mbid is not None:
            _record_mapping(
                catalog,
                artist,
                source_name,
                mbid,
                IdentityConfidence.EXTERNAL_ID,
                "url_rel",
                checked_at,
            )
            continue

        try:
            hits = source.search_artist_by_name(artist.display_name, limit=3)
        except (InvalidSourceResponseError, SourceUnavailableError):
            # This artist's name search failed; record unmapped and continue with the
            # rest of the batch rather than failing the whole mapping pass.
            catalog.put_artist_identity_mapping(
                artist_local_id=artist.local_id,
                source=source_name,
                status="unmapped",
                method="name_search",
                attempted_at=checked_at,
            )
            continue
        accepted = _accept_name_search(hits)
        if accepted is not None:
            _record_mapping(
                catalog,
                artist,
                source_name,
                accepted,
                IdentityConfidence.SOURCE_ONLY,
                "name_search",
                checked_at,
            )
        else:
            catalog.put_artist_identity_mapping(
                artist_local_id=artist.local_id,
                source=source_name,
                status="unmapped",
                method="name_search",
                attempted_at=checked_at,
            )


def _run_deezer_url_rel_mapping(
    catalog: Catalog,
    source: IdentityMappingSource,
    checked_at: datetime,
) -> None:
    """Resolve Deezer artist ids from the MusicBrainz url-rels batch (issue #42).

    Only artists already carrying a musicbrainz ``SourceReference`` (mapped by
    the caller's Spotify-url batch, this run or an earlier one) and lacking a
    deezer ``SourceReference`` are eligible. There is no Deezer name-search
    fallback: an artist with no Deezer url-rel is simply left unmapped. A
    ``RateLimitedError`` propagates unchanged (via ``_SourceCallStopped`` from
    the caller's paced wrapper) so the caller's cooldown accounting stays
    correct and mapping simply stops for this run.
    """
    candidates = _artists_needing_mapping(catalog, "deezer")
    eligible = [
        artist
        for artist in candidates
        if any(ref.source == "musicbrainz" for ref in artist.source_refs)
        and not _skip_for_retry_window(catalog, artist.local_id, "deezer", checked_at)
    ]
    for artist in eligible:
        mb_ref = next(ref for ref in artist.source_refs if ref.source == "musicbrainz")
        try:
            deezer_id = source.deezer_artist_id(mb_ref.native_id)
        except (InvalidSourceResponseError, SourceUnavailableError):
            # This artist's url-rels lookup failed; record unmapped and continue
            # with the rest of the batch rather than failing the whole pass.
            catalog.put_artist_identity_mapping(
                artist_local_id=artist.local_id,
                source="deezer",
                status="unmapped",
                method="url_rel",
                attempted_at=checked_at,
            )
            continue
        if deezer_id is not None:
            updated_refs = tuple(ref for ref in artist.source_refs if ref.source != "deezer") + (
                SourceReference(
                    source="deezer",
                    native_id=deezer_id,
                    canonical_url=f"https://www.deezer.com/artist/{deezer_id}",
                    observed_at=checked_at,
                    confidence=IdentityConfidence.EXTERNAL_ID,
                ),
            )
            catalog.put_artist(
                Artist(
                    local_id=artist.local_id,
                    display_name=artist.display_name,
                    source_refs=updated_refs,
                    identity_confidence=artist.identity_confidence,
                    observed_at=artist.observed_at,
                )
            )
            catalog.put_artist_identity_mapping(
                artist_local_id=artist.local_id,
                source="deezer",
                status="mapped",
                method="url_rel",
                attempted_at=checked_at,
            )
        else:
            catalog.put_artist_identity_mapping(
                artist_local_id=artist.local_id,
                source="deezer",
                status="unmapped",
                method="url_rel",
                attempted_at=checked_at,
            )


def _artists_needing_mapping(catalog: Catalog, source_name: str) -> tuple[Artist, ...]:
    seen: dict[str, Artist] = {}
    for entry in catalog.list_watchlist(limit=500):
        artist = entry.artist
        if artist.local_id in seen:
            continue
        if any(ref.source == source_name for ref in artist.source_refs):
            continue
        seen[artist.local_id] = artist
    return tuple(seen.values())


def _skip_for_retry_window(
    catalog: Catalog,
    artist_local_id: str,
    source_name: str,
    checked_at: datetime,
) -> bool:
    mapping = catalog.get_artist_identity_mapping(artist_local_id, source_name)
    if mapping is None or mapping.get("status") != "unmapped":
        return False
    attempted_at_raw = mapping.get("attempted_at")
    if not isinstance(attempted_at_raw, str):
        return False
    attempted_at = datetime.fromisoformat(attempted_at_raw)
    return checked_at - attempted_at < MAPPING_RETRY_INTERVAL


def _spotify_ref(artist: Artist) -> SourceReference | None:
    for ref in artist.source_refs:
        if ref.source == "spotify":
            return ref
    return None


def _accept_name_search(hits: list[dict[str, object]]) -> str | None:
    if not hits:
        return None
    top_score = hits[0]["score"]
    if not isinstance(top_score, int) or top_score < MIN_NAME_SEARCH_SCORE:
        return None
    if len(hits) > 1:
        second_score = hits[1]["score"]
        if isinstance(second_score, int) and second_score >= MIN_NAME_SEARCH_SCORE:
            return None
    top_id = hits[0]["id"]
    return top_id if isinstance(top_id, str) else None


def _record_mapping(
    catalog: Catalog,
    artist: Artist,
    source_name: str,
    native_id: str,
    confidence: IdentityConfidence,
    method: str,
    checked_at: datetime,
) -> None:
    updated_refs = tuple(ref for ref in artist.source_refs if ref.source != source_name) + (
        SourceReference(
            source=source_name,
            native_id=native_id,
            canonical_url=f"https://musicbrainz.org/artist/{native_id}",
            observed_at=checked_at,
            confidence=confidence,
        ),
    )
    catalog.put_artist(
        Artist(
            local_id=artist.local_id,
            display_name=artist.display_name,
            source_refs=updated_refs,
            identity_confidence=artist.identity_confidence,
            observed_at=artist.observed_at,
        )
    )
    catalog.put_artist_identity_mapping(
        artist_local_id=artist.local_id,
        source=source_name,
        status="mapped",
        method=method,
        attempted_at=checked_at,
    )


__all__ = ["run_identity_mapping", "MAPPING_RETRY_INTERVAL", "MIN_NAME_SEARCH_SCORE"]
