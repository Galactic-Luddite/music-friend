"""Convert untrusted Spotify objects into canonical Music Friend records."""

from __future__ import annotations

import re
from datetime import date, datetime
from hashlib import sha256

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.domain.text import sanitize_display_name
from music_friend.errors import InvalidSourceResponseError

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_MAX_ARTISTS = 50
_TEXT_LIMIT = 96
_LOCAL_ID_DOMAIN = "music-friend-local-id-v1"


class _ParseFailure(ValueError):
    """A safe private parser diagnostic that never contains source values."""


def _local_id(kind: str, native_id: str) -> str:
    material = "\0".join((_LOCAL_ID_DOMAIN, "spotify", kind, native_id)).encode()
    return f"mf:{sha256(material).hexdigest()}"


def _object(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise _ParseFailure(f"{field} must be an object")
    return value


def _spotify_id(value: object, field: str) -> str:
    if type(value) is not str or _SPOTIFY_ID.fullmatch(value) is None:
        raise _ParseFailure(f"{field} must be a Spotify identifier")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise _ParseFailure(f"{field} must be text")
    normalized = sanitize_display_name(value, limit=_TEXT_LIMIT)
    if not normalized.strip():
        raise _ParseFailure(f"{field} must not be empty")
    return normalized


def _observed_at(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _ParseFailure("observed_at must be timezone-aware")
    return value


def _artist_ids(value: object) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= _MAX_ARTISTS:
        raise _ParseFailure("artists must contain 1..50 entries")
    ids: list[str] = []
    for entry in value:
        artist = _object(entry, "artist")
        ids.append(_spotify_id(artist.get("id"), "artist id"))
    if len(set(ids)) != len(ids):
        raise _ParseFailure("artist identifiers must be unique")
    return tuple(ids)


def _source_reference(kind: str, native_id: str, observed_at: datetime) -> SourceReference:
    url_kind = "album" if kind == "release" else kind
    return SourceReference(
        source="spotify",
        native_id=native_id,
        canonical_url=f"https://open.spotify.com/{url_kind}/{native_id}",
        observed_at=observed_at,
    )


def normalize_artist(payload: object, *, observed_at: datetime) -> Artist:
    """Normalize one Spotify artist object and discard every unused field."""
    try:
        value = _object(payload, "artist")
        native_id = _spotify_id(value.get("id"), "artist id")
        observed = _observed_at(observed_at)
        return Artist(
            local_id=_local_id("artist", native_id),
            display_name=_text(value.get("name"), "artist name"),
            source_refs=(_source_reference("artist", native_id, observed),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=observed,
        )
    except (_ParseFailure, TypeError, ValueError) as error:
        diagnostic = error if isinstance(error, _ParseFailure) else _ParseFailure("artist invalid")
        raise InvalidSourceResponseError() from diagnostic


def normalize_track(payload: object, *, observed_at: datetime) -> CatalogItem:
    """Normalize one Spotify track object and discard every unused field."""
    try:
        value = _object(payload, "track")
        native_id = _spotify_id(value.get("id"), "track id")
        artist_ids = _artist_ids(value.get("artists"))
        observed = _observed_at(observed_at)
        return CatalogItem(
            kind="track",
            local_id=_local_id("track", native_id),
            title=_text(value.get("name"), "track name"),
            artist_refs=tuple(_local_id("artist", artist_id) for artist_id in artist_ids),
            source_refs=(_source_reference("track", native_id, observed),),
            observed_at=observed,
        )
    except (_ParseFailure, TypeError, ValueError) as error:
        diagnostic = error if isinstance(error, _ParseFailure) else _ParseFailure("track invalid")
        raise InvalidSourceResponseError() from diagnostic


def normalize_track_batch(payload: object, *, observed_at: datetime) -> CatalogItemBatch:
    """Normalize one track and every complete credited artist record in the same batch."""
    try:
        value = _object(payload, "track")
        raw_artists = value.get("artists")
        if type(raw_artists) is not list or not 1 <= len(raw_artists) <= _MAX_ARTISTS:
            raise _ParseFailure("artists must contain 1..50 entries")
        observed = _observed_at(observed_at)
        item = normalize_track(value, observed_at=observed)
        artists = tuple(
            normalize_artist(raw_artist, observed_at=observed) for raw_artist in raw_artists
        )
        return CatalogItemBatch((item,), artists)
    except InvalidSourceResponseError:
        raise
    except (_ParseFailure, TypeError, ValueError) as error:
        diagnostic = error if isinstance(error, _ParseFailure) else _ParseFailure("track invalid")
        raise InvalidSourceResponseError() from diagnostic


def _release_date(value: object, precision_value: object) -> tuple[date, ReleaseDatePrecision]:
    if type(value) is not str or type(precision_value) is not str:
        raise _ParseFailure("release date and precision must be text")
    try:
        precision = ReleaseDatePrecision(precision_value)
    except ValueError:
        raise _ParseFailure("release date precision is invalid") from None
    expected_lengths = {
        ReleaseDatePrecision.YEAR: 4,
        ReleaseDatePrecision.MONTH: 7,
        ReleaseDatePrecision.DAY: 10,
    }
    if len(value) != expected_lengths[precision]:
        raise _ParseFailure("release date does not match its precision")
    expanded = {
        ReleaseDatePrecision.YEAR: f"{value}-01-01",
        ReleaseDatePrecision.MONTH: f"{value}-01",
        ReleaseDatePrecision.DAY: value,
    }[precision]
    try:
        parsed = date.fromisoformat(expanded)
    except ValueError:
        raise _ParseFailure("release date is invalid") from None
    if parsed.isoformat()[: len(value)] != value:
        raise _ParseFailure("release date is not canonical")
    return parsed, precision


def normalize_release(payload: object, *, observed_at: datetime) -> Release:
    """Normalize one Spotify album object and discard every unused field."""
    try:
        value = _object(payload, "release")
        native_id = _spotify_id(value.get("id"), "release id")
        artist_ids = _artist_ids(value.get("artists"))
        release_date, precision = _release_date(
            value.get("release_date"), value.get("release_date_precision")
        )
        observed = _observed_at(observed_at)
        return Release(
            local_id=_local_id("release", native_id),
            title=_text(value.get("name"), "release name"),
            release_type=_text(value.get("album_type"), "release type"),
            release_date=release_date,
            date_precision=precision,
            artist_refs=tuple(_local_id("artist", artist_id) for artist_id in artist_ids),
            source_refs=(_source_reference("release", native_id, observed),),
            observed_at=observed,
        )
    except (_ParseFailure, TypeError, ValueError) as error:
        diagnostic = error if isinstance(error, _ParseFailure) else _ParseFailure("release invalid")
        raise InvalidSourceResponseError() from diagnostic


__all__ = ["normalize_artist", "normalize_release", "normalize_track", "normalize_track_batch"]
