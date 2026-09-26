"""Normalize MusicBrainz release-group JSON to domain Release records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from hashlib import sha256

from music_friend.domain import IdentityConfidence, Release, ReleaseDatePrecision, SourceReference
from music_friend.domain.text import sanitize_display_name

_TEXT_LIMIT = 96


def normalize_release_group(
    release_group: Mapping[str, object],
    *,
    artist_id_map: Mapping[str, str],
    now: datetime,
) -> Release | None:
    """Convert a MusicBrainz release-group record to a Release, or None if invalid.

    Args:
        release_group: The release-group object from MusicBrainz API
        artist_id_map: Map from MBID to local artist_id for resolution
        now: Observed timestamp for the Release and SourceReference

    Returns:
        A normalized Release or None if the record is incomplete/invalid.
    """
    try:
        # Extract required fields
        rgid = _require_text(release_group.get("id"), "id")
        title = _require_display_text(release_group.get("title"), "title")
        primary_type = release_group.get("primary-type")
        if not primary_type or not isinstance(primary_type, str):
            return None
        release_type = primary_type.lower()

        # Parse date with precision
        first_release_date = release_group.get("first-release-date")
        if not first_release_date or not isinstance(first_release_date, str):
            # Empty or missing first-release-date is rejected
            return None

        release_date, date_precision = _parse_date(first_release_date)
        if release_date is None or date_precision is None:
            return None

        # Extract artist refs from artist-credit
        artist_credit = release_group.get("artist-credit")
        if not isinstance(artist_credit, list):
            return None

        artist_refs: list[str] = []
        for credit_item in artist_credit:
            if not isinstance(credit_item, Mapping):
                continue
            artist_obj = credit_item.get("artist")
            if not isinstance(artist_obj, Mapping):
                continue
            artist_mbid = artist_obj.get("id")
            if not isinstance(artist_mbid, str):
                continue
            # Look up the local artist ID
            if artist_mbid in artist_id_map:
                artist_refs.append(artist_id_map[artist_mbid])

        if not artist_refs:
            return None

        # Generate local ID from MusicBrainz GUID
        local_id = _local_id_for("musicbrainz", "release", rgid)

        source_refs = (
            SourceReference(
                source="musicbrainz",
                native_id=rgid,
                canonical_url=f"https://musicbrainz.org/release-group/{rgid}",
                observed_at=now,
                confidence=IdentityConfidence.SOURCE_ONLY,
            ),
        )

        return Release(
            local_id=local_id,
            title=title,
            release_type=release_type,
            release_date=release_date,
            date_precision=date_precision,
            artist_refs=tuple(artist_refs),
            source_refs=source_refs,
            observed_at=now,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_date(date_str: str) -> tuple[date | None, ReleaseDatePrecision | None]:
    """Parse a date string with precision.

    Returns:
        (date_object, precision) or (None, None) if parsing fails.
    """
    if not isinstance(date_str, str):
        return None, None

    date_str = date_str.strip()
    if not date_str:
        return None, None

    parts = date_str.split("-")
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            # YYYY
            year = int(parts[0])
            return date(year, 1, 1), ReleaseDatePrecision.YEAR
        elif len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            # YYYY-MM
            year = int(parts[0])
            month = int(parts[1])
            return date(year, month, 1), ReleaseDatePrecision.MONTH
        elif len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
            # YYYY-MM-DD
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            return date(year, month, day), ReleaseDatePrecision.DAY
    except (ValueError, OverflowError):
        pass

    return None, None


def _require_text(value: object, field: str) -> str:
    """Require a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_display_text(value: object, field: str) -> str:
    """Require non-empty provider display text, sanitized against untrusted framing."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty string")
    sanitized = sanitize_display_name(value, limit=_TEXT_LIMIT)
    if not sanitized.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return sanitized


def _local_id_for(domain: str, kind: str, native_id: str) -> str:
    """Generate a stable local ID from domain, kind, and native ID."""
    combined = f"{domain}:{kind}:{native_id}"
    return sha256(combined.encode("utf-8")).hexdigest()


__all__ = ["normalize_release_group"]
