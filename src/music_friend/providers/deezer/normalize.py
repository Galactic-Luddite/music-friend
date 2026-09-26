"""Normalize Deezer album JSON to domain Release records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from hashlib import sha256

from music_friend.domain import IdentityConfidence, Release, ReleaseDatePrecision, SourceReference
from music_friend.domain.text import sanitize_display_name

_TEXT_LIMIT = 96


def normalize_album(
    album: Mapping[str, object],
    *,
    artist_native_id: str,
    now: datetime,
) -> Release | None:
    """Convert one Deezer ``/artist/{id}/albums`` album object to a Release.

    Field shape verified live against Deezer's real keyless API on 2026-09-26
    (see ``transport.py`` module docstring): ``id`` (int), ``title`` (str),
    ``release_date`` (``"YYYY-MM-DD"``), ``record_type`` (str, e.g. ``"album"``).
    Returns ``None`` for a record missing any required field rather than raising,
    matching the MusicBrainz normalizer's tolerance for one bad record in a page.
    """
    try:
        native_id = _require_id(album.get("id"))
        title = _require_display_text(album.get("title"), "title")
        record_type = album.get("record_type")
        if not record_type or not isinstance(record_type, str):
            return None
        release_type = record_type.lower()

        release_date_raw = album.get("release_date")
        if not release_date_raw or not isinstance(release_date_raw, str):
            return None
        release_date, precision = _parse_date(release_date_raw)
        if release_date is None or precision is None:
            return None

        link = album.get("link")
        canonical_url = (
            link if isinstance(link, str) and link else f"https://www.deezer.com/album/{native_id}"
        )

        local_id = _local_id_for("deezer", "release", native_id)
        # This placeholder artist id is never a real catalog id (it is a distinct
        # hash namespace from every local artist id), so the release-discovery
        # persistence step always overrides it with the real, already-known local
        # artist id (see release_discovery._for_persistence). Deezer's per-artist
        # albums endpoint gives no other artist-identity information to encode here.
        placeholder_artist_id = _local_id_for("deezer", "artist-placeholder", artist_native_id)
        source_refs = (
            SourceReference(
                source="deezer",
                native_id=native_id,
                canonical_url=canonical_url,
                observed_at=now,
                confidence=IdentityConfidence.SOURCE_ONLY,
            ),
        )
        return Release(
            local_id=local_id,
            title=title,
            release_type=release_type,
            release_date=release_date,
            date_precision=precision,
            artist_refs=(placeholder_artist_id,),
            source_refs=source_refs,
            observed_at=now,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_date(date_str: str) -> tuple[date | None, ReleaseDatePrecision | None]:
    date_str = date_str.strip()
    if not date_str:
        return None, None
    parts = date_str.split("-")
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            return date(int(parts[0]), 1, 1), ReleaseDatePrecision.YEAR
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            return date(int(parts[0]), int(parts[1]), 1), ReleaseDatePrecision.MONTH
        if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
            return date(int(parts[0]), int(parts[1]), int(parts[2])), ReleaseDatePrecision.DAY
    except (ValueError, OverflowError):
        pass
    return None, None


def _require_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("id must be an integer")
    return str(value)


def _require_display_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty string")
    sanitized = sanitize_display_name(value, limit=_TEXT_LIMIT)
    if not sanitized.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return sanitized


def _local_id_for(domain: str, kind: str, native_id: str) -> str:
    combined = f"{domain}:{kind}:{native_id}"
    return sha256(combined.encode("utf-8")).hexdigest()


__all__ = ["normalize_album"]
