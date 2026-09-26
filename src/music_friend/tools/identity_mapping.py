"""Identity mapping from Spotify artists to MusicBrainz IDs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from music_friend.domain import (
    DAILY_REFRESH_MINUTES,
    Artist,
    IdentityConfidence,
    SourceReference,
)
from music_friend.errors import RateLimitedError
from music_friend.providers import MusicSource
from music_friend.store import Catalog

#: Retry interval for unmapped artists: 7 days
MAPPING_RETRY_INTERVAL = timedelta(minutes=7 * DAILY_REFRESH_MINUTES)

#: Minimum score for artist name search acceptance
MIN_NAME_SEARCH_SCORE = 90


def run_identity_mapping(
    catalog: Catalog,
    source: MusicSource,
    source_name: str,
    checked_at: datetime,
) -> None:
    """Map watchlist artists to MusicBrainz identities.

    For each watched artist without a musicbrainz SourceReference:
    1. Try to resolve via Spotify URL -> MBID relation
    2. Fall back to artist name search with score thresholds
    3. Record unmapped artists with retry gating

    Args:
        catalog: The catalog for artist/mapping storage
        source: A MusicBrainz MusicSource instance
        source_name: The source name (e.g., "musicbrainz")
        checked_at: The timestamp for this mapping run
    """
    # Get all watched artists who don't have a musicbrainz identity yet
    artists_needing_mapping = _get_artists_needing_mapping(catalog)

    for artist in artists_needing_mapping:
        # Check if this artist was recently tried and marked unmapped
        if _should_skip_unmapped_retry(catalog, artist.local_id, checked_at):
            continue

        try:
            # First, try to resolve via Spotify URL relations
            mapping_result = _try_url_based_mapping(source, artist)

            if mapping_result is None:
                # Fall back to name-based search
                mapping_result = _try_name_based_mapping(source, artist)

            if mapping_result is not None:
                mbid, confidence, method = mapping_result
                # Store the successful mapping
                catalog.put_artist(
                    Artist(
                        local_id=artist.local_id,
                        name=artist.name,
                        refs=[
                            *artist.refs,
                            SourceReference(
                                source=source_name,
                                identity=mbid,
                                confidence=confidence,
                            ),
                        ],
                    ),
                    updated_at=checked_at,
                )
                # Record successful mapping
                catalog.put_artist_identity_mapping(
                    artist_local_id=artist.local_id,
                    source=source_name,
                    status="mapped",
                    identity=mbid,
                    method=method,
                    checked_at=checked_at,
                )
            else:
                # No mapping found; record as unmapped
                catalog.put_artist_identity_mapping(
                    artist_local_id=artist.local_id,
                    source=source_name,
                    status="unmapped",
                    identity=None,
                    method=None,
                    checked_at=checked_at,
                )
        except RateLimitedError:
            # Stop processing if we hit a rate limit
            return


def _get_artists_needing_mapping(catalog: Catalog) -> Sequence[Artist]:
    """Get all watched artists without a musicbrainz SourceReference."""
    watchlist = catalog.list_watchlist(limit=10000)
    needing_mapping = []

    for entry in watchlist:
        artist = catalog.get_artist(entry.artist_local_id)
        if artist is None:
            continue

        # Check if already has musicbrainz reference
        has_musicbrainz = any(ref.source == "musicbrainz" for ref in artist.refs)
        if not has_musicbrainz:
            needing_mapping.append(artist)

    return needing_mapping


def _should_skip_unmapped_retry(
    catalog: Catalog,
    artist_local_id: str,
    checked_at: datetime,
) -> bool:
    """Check if an unmapped artist should be skipped due to retry gating."""
    mapping = catalog.get_artist_identity_mapping(artist_local_id, "musicbrainz")
    if mapping is None:
        return False

    if mapping.get("status") != "unmapped":
        return False

    last_checked = mapping.get("checked_at")
    if last_checked is None:
        return False

    last_checked_dt = (
        last_checked if isinstance(last_checked, datetime) else datetime.fromisoformat(last_checked)
    )

    time_since_last_attempt = checked_at - last_checked_dt
    return time_since_last_attempt < MAPPING_RETRY_INTERVAL


def _try_url_based_mapping(
    source: MusicSource,
    artist: Artist,
) -> tuple[str, IdentityConfidence, str] | None:
    """Try to resolve via Spotify URL -> MBID relation.

    Returns (mbid, confidence, method) or None if no mapping found.
    """
    # Get the Spotify URL from artist refs
    spotify_url = None
    for ref in artist.refs:
        if ref.source == "spotify":
            spotify_url = f"https://open.spotify.com/artist/{ref.identity}"
            break

    if spotify_url is None:
        return None

    # Query MusicBrainz for artist relations via URL
    # This uses the source's URL lookup capability if available
    try:
        mbid = _lookup_mbid_via_url(source, spotify_url)
        if mbid is not None:
            return (mbid, IdentityConfidence.EXTERNAL_ID, "url_relation")
    except Exception:
        pass

    return None


def _try_name_based_mapping(
    source: MusicSource,
    artist: Artist,
) -> tuple[str, IdentityConfidence, str] | None:
    """Try to resolve via artist name search.

    Accepts only if top score >= 90 AND (no second hit OR second hit score < 90).
    Returns (mbid, confidence, method) or None if no mapping found.
    """
    try:
        results = _search_artists_by_name(source, artist.name)
        if not results:
            return None

        # Must have top result with score >= 90
        top_score = results[0]["score"]
        if top_score < MIN_NAME_SEARCH_SCORE:
            return None

        # Must not have a second result with score >= 90
        if len(results) > 1 and results[1]["score"] >= MIN_NAME_SEARCH_SCORE:
            return None

        # Accept the top result
        mbid = results[0]["id"]
        return (mbid, IdentityConfidence.SOURCE_ONLY, "name_search")
    except Exception:
        pass

    return None


def _lookup_mbid_via_url(source: MusicSource, url: str) -> str | None:
    """Look up MBID from a Spotify artist URL via MusicBrainz."""
    from music_friend.providers.musicbrainz import MusicBrainzSource

    if not isinstance(source, MusicBrainzSource):
        return None

    return source.lookup_artist_by_spotify_url(url)


def _search_artists_by_name(source: MusicSource, name: str) -> list[dict[str, object]]:
    """Search for artists by name in MusicBrainz.

    Returns a list of results with 'id' and 'score' keys, sorted by score descending.
    """
    from music_friend.providers.musicbrainz import MusicBrainzSource

    if not isinstance(source, MusicBrainzSource):
        return []

    return source.search_artist_by_name(name, limit=3)


__all__ = ["run_identity_mapping", "MAPPING_RETRY_INTERVAL"]
