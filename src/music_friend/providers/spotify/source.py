"""Read-only Spotify implementation of the provider-neutral source contract."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import base64
import calendar
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.errors import InvalidSourceResponseError
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
    require_capability,
)
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.normalize import (
    normalize_artist,
    normalize_release,
    normalize_track_batch,
)
from music_friend.providers.spotify.transport import SpotifyOperation

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
_TIME_RANGES = frozenset({"short_term", "medium_term", "long_term"})
_MAX_OFFSET = 1_000_000


class _TokenProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def _call_deadline(self) -> float: ...

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> Mapping[str, object]: ...


class _SourceFailure(ValueError):
    """Safe private response diagnostic with no source values."""


class SpotifySource:
    """Normalize the complete bounded Spotify read surface."""

    def __init__(
        self,
        *,
        settings: SpotifySettings,
        tokens: _TokenProvider,
        clock: Callable[[], datetime],
    ) -> None:
        if type(settings) is not SpotifySettings:
            raise ValueError("settings must be SpotifySettings")
        snapshot = tokens.capabilities()
        if type(snapshot) is not ProviderCapabilities:
            raise ValueError("tokens must provide canonical capabilities")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._settings = settings
        self._tokens = tokens
        self._clock = clock
        self._capabilities = snapshot

    def capabilities(self) -> ProviderCapabilities:
        """Return the precomputed local capability snapshot without I/O."""
        return self._capabilities

    def health(self) -> ProviderHealth:
        deadline = self._call_deadline()
        self._authorize(Capability.HEALTH)
        self._execute(SpotifyOperation.HEALTH, deadline=deadline)
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        deadline = self._call_deadline()
        if (
            type(query) is not str
            or not query.strip()
            or query.strip() != query
            or len(query) > 256
        ):
            raise ValueError("query must contain 1..256 trimmed characters")
        _bounded_integer(limit, "limit", lower=1, upper=10)
        self._authorize(Capability.SEARCH_ARTISTS)
        data = self._execute(
            SpotifyOperation.SEARCH_ARTISTS,
            query=(("q", query), ("limit", str(limit))),
            deadline=deadline,
        )
        try:
            artists = _object(data.get("artists"), "artists")
            items = _items(artists, maximum=10)
            _nullable_text(artists.get("next"), "search next")
            observed = self._now()
            return Page(tuple(normalize_artist(item, observed_at=observed) for item in items))
        except _SourceFailure as error:
            raise InvalidSourceResponseError() from error

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        deadline = self._call_deadline()
        after = _decode_cursor(cursor, "followed", "after")
        query = (("limit", "50"),) if after is None else (("limit", "50"), ("after", after))
        self._authorize(Capability.FOLLOWED_ARTISTS)
        data = self._execute(SpotifyOperation.FOLLOWED_ARTISTS, query=query, deadline=deadline)
        try:
            artists = _object(data.get("artists"), "artists")
            items = _items(artists, maximum=50)
            next_value = _nullable_text(artists.get("next"), "followed next")
            cursors = _object(artists.get("cursors"), "followed cursors")
            after_value = cursors.get("after")
            if after_value is not None:
                after_value = _id(after_value, "followed after")
            next_cursor = None
            if next_value is not None:
                if after_value is None or not items:
                    raise _SourceFailure("followed continuation cannot advance")
                next_cursor = _encode_cursor("followed", "after", after_value)
            observed = self._now()
            normalized = tuple(normalize_artist(item, observed_at=observed) for item in items)
            return Page(normalized, next_cursor)
        except _SourceFailure as error:
            raise InvalidSourceResponseError() from error

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        deadline = self._call_deadline()
        encoded_offset = _decode_cursor(cursor, "saved", "offset")
        offset = 0 if encoded_offset is None else int(encoded_offset)
        self._authorize(Capability.SAVED_ITEMS)
        data = self._execute(
            SpotifyOperation.SAVED_TRACKS,
            query=(("limit", "50"), ("offset", str(offset))),
            deadline=deadline,
        )
        try:
            items = _items(data, maximum=50)
            next_value = _nullable_text(data.get("next"), "saved next")
            normalized_items: list[CatalogItem] = []
            normalized_artists: dict[str, Artist] = {}
            observed = self._now()
            for item in items:
                wrapper = _object(item, "saved item")
                batch = normalize_track_batch(wrapper.get("track"), observed_at=observed)
                normalized_items.extend(batch.items)
                for artist in batch.artists:
                    normalized_artists.setdefault(artist.local_id, artist)
            next_cursor = None
            if next_value is not None:
                next_offset = offset + len(items)
                if not items or next_offset > _MAX_OFFSET:
                    raise _SourceFailure("saved continuation cannot advance")
                next_cursor = _encode_cursor("saved", "offset", next_offset)
            return CatalogItemBatch(
                tuple(normalized_items), tuple(normalized_artists.values()), next_cursor
            )
        except _SourceFailure as error:
            raise InvalidSourceResponseError() from error

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        deadline = self._call_deadline()
        if type(time_range) is not str or time_range not in _TIME_RANGES:
            raise ValueError("time_range is not supported")
        _bounded_integer(limit, "limit", lower=1, upper=50)
        self._authorize(Capability.TOP_ITEMS)
        data = self._execute(
            SpotifyOperation.TOP_TRACKS,
            query=(("time_range", time_range), ("limit", str(limit))),
            deadline=deadline,
        )
        try:
            items = _items(data, maximum=50)
            _nullable_text(data.get("next"), "top next")
            observed = self._now()
            normalized_items: list[CatalogItem] = []
            normalized_artists: dict[str, Artist] = {}
            for item in items:
                batch = normalize_track_batch(item, observed_at=observed)
                normalized_items.extend(batch.items)
                for artist in batch.artists:
                    normalized_artists.setdefault(artist.local_id, artist)
            return CatalogItemBatch(tuple(normalized_items), tuple(normalized_artists.values()))
        except _SourceFailure as error:
            raise InvalidSourceResponseError() from error

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        deadline = self._call_deadline()
        if type(time_range) is not str or time_range not in _TIME_RANGES:
            raise ValueError("time_range is not supported")
        _bounded_integer(limit, "limit", lower=1, upper=50)
        self._authorize(Capability.TOP_ARTISTS)
        data = self._execute(
            SpotifyOperation.TOP_ARTISTS,
            query=(("time_range", time_range), ("limit", str(limit))),
            deadline=deadline,
        )
        try:
            items = _items(data, maximum=50)
            _nullable_text(data.get("next"), "top artists next")
            observed = self._now()
            return Page(tuple(normalize_artist(item, observed_at=observed) for item in items))
        except _SourceFailure as error:
            raise InvalidSourceResponseError() from error

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        deadline = self._call_deadline()
        artist_ids = _artist_reference_ids(artist_refs)
        _aware_datetime(since, "since")
        if cursor is not None and len(artist_ids) != 1:
            raise ValueError("release cursor requires exactly one artist")
        self._authorize(Capability.RECENT_RELEASES)
        releases: dict[str, Release] = {}
        for artist_id in artist_ids:
            offset = _decode_release_cursor(cursor, artist_id)
            data = self._execute(
                SpotifyOperation.ARTIST_RELEASES,
                query=(
                    ("artist_id", artist_id),
                    ("include_groups", "album,single"),
                    ("limit", "10"),
                    ("offset", str(offset)),
                ),
                deadline=deadline,
            )
            try:
                items = _items(data, maximum=10)
                next_value = _nullable_text(data.get("next"), "release next")
                observed = self._now()
                normalized = tuple(normalize_release(item, observed_at=observed) for item in items)
                if next_value is not None:
                    if not items or offset + len(items) > _MAX_OFFSET:
                        raise _SourceFailure("release continuation cannot advance")
                    if len(artist_ids) == 1:
                        return Page(
                            tuple(
                                release
                                for release in normalized
                                if _could_overlap_since(release, since)
                            ),
                            _encode_release_cursor(artist_id, offset + len(items)),
                        )
                    raise _SourceFailure("release continuation requires one artist")
                for release in normalized:
                    if _could_overlap_since(release, since):
                        releases.setdefault(release.local_id, release)
            except _SourceFailure as error:
                raise InvalidSourceResponseError() from error
        return Page(tuple(releases[key] for key in sorted(releases)))

    def _authorize(self, capability: Capability) -> None:
        require_capability(self._capabilities, capability)

    def _execute(
        self,
        operation: SpotifyOperation,
        *,
        query: tuple[tuple[str, str], ...] = (),
        deadline: float,
    ) -> Mapping[str, object]:
        return self._tokens._execute(operation, query=query, deadline=deadline)

    def _call_deadline(self) -> float:
        return self._tokens._call_deadline()

    def _now(self) -> datetime:
        value = self._clock()
        _aware_datetime(value, "clock result")
        return value


def _object(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise _SourceFailure(f"{field} must be an object")
    return value


def _items(value: Mapping[str, object], *, maximum: int) -> list[object]:
    items = value.get("items")
    if type(items) is not list or len(items) > maximum:
        raise _SourceFailure("items have an invalid shape or size")
    return items


def _nullable_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 4096:
        raise _SourceFailure(f"{field} must be bounded text or null")
    return value


def _id(value: object, field: str) -> str:
    if type(value) is not str or _SPOTIFY_ID.fullmatch(value) is None:
        raise _SourceFailure(f"{field} must be a Spotify identifier")
    return value


def _bounded_integer(value: object, field: str, *, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{field} must be between {lower} and {upper}")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _artist_reference_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("artist_refs must be a sequence")
    if not 1 <= len(value) <= 10:
        raise ValueError("artist_refs must contain 1..10 references")
    ids: list[str] = []
    for reference in value:
        if type(reference) is not SourceReference or reference.source != "spotify":
            raise ValueError("artist_refs must contain Spotify references")
        try:
            ids.append(_id(reference.native_id, "artist reference"))
        except _SourceFailure as error:
            raise ValueError("artist_refs contain an invalid identifier") from error
    if len(set(ids)) != len(ids):
        raise ValueError("artist_refs must be unique")
    return tuple(ids)


def _could_overlap_since(release: Release, since: datetime) -> bool:
    if release.date_precision is ReleaseDatePrecision.YEAR:
        latest = release.release_date.replace(month=12, day=31)
    elif release.date_precision is ReleaseDatePrecision.MONTH:
        latest = release.release_date.replace(
            day=calendar.monthrange(release.release_date.year, release.release_date.month)[1]
        )
    else:
        latest = release.release_date
    return latest >= since.date()


def _encode_cursor(operation: str, field: str, value: str | int) -> str:
    raw = json.dumps(
        {"v": 1, "op": operation, field: value}, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(encoded) > 512:
        raise _SourceFailure("cursor exceeds its bound")
    return encoded


def _decode_cursor(cursor: str | None, operation: str, field: str) -> str | None:
    if cursor is None:
        return None
    if type(cursor) is not str or _CURSOR.fullmatch(cursor) is None:
        raise InvalidSourceResponseError()
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        value = json.loads(raw.decode("ascii"))
        expected_keys = {"v", "op", field}
        if type(value) is not dict or set(value) != expected_keys:
            raise _SourceFailure("cursor keys are invalid")
        if value["v"] != 1 or value["op"] != operation:
            raise _SourceFailure("cursor binding is invalid")
        cursor_value = value[field]
        if field == "after":
            result = _id(cursor_value, "cursor after")
        else:
            if type(cursor_value) is not int or not 0 <= cursor_value <= _MAX_OFFSET:
                raise _SourceFailure("cursor offset is invalid")
            result = str(cursor_value)
        if _encode_cursor(operation, field, cursor_value) != cursor:
            raise _SourceFailure("cursor encoding is not canonical")
        return result
    except (ValueError, UnicodeError, json.JSONDecodeError, _SourceFailure) as error:
        diagnostic = (
            error if isinstance(error, _SourceFailure) else _SourceFailure("cursor invalid")
        )
        raise InvalidSourceResponseError() from diagnostic


def _encode_release_cursor(artist_id: str, offset: int) -> str:
    _bounded_integer(offset, "release cursor offset", lower=0, upper=_MAX_OFFSET)
    raw = json.dumps(
        {"artist_id": artist_id, "offset": offset, "op": "releases", "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(encoded) > 512:
        raise _SourceFailure("release cursor exceeds its bound")
    return encoded


def _decode_release_cursor(cursor: str | None, artist_id: str) -> int:
    if cursor is None:
        return 0
    if type(cursor) is not str or _CURSOR.fullmatch(cursor) is None:
        raise InvalidSourceResponseError()
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        value = json.loads(raw.decode("ascii"))
        if (
            type(value) is not dict
            or set(value) != {"artist_id", "offset", "op", "v"}
            or value["artist_id"] != artist_id
            or value["op"] != "releases"
            or value["v"] != 1
        ):
            raise _SourceFailure("release cursor binding is invalid")
        offset = value["offset"]
        if type(offset) is not int or not 0 <= offset <= _MAX_OFFSET:
            raise _SourceFailure("release cursor offset is invalid")
        if _encode_release_cursor(artist_id, offset) != cursor:
            raise _SourceFailure("release cursor encoding is not canonical")
        return offset
    except (ValueError, UnicodeError, json.JSONDecodeError, _SourceFailure) as error:
        diagnostic = (
            error if isinstance(error, _SourceFailure) else _SourceFailure("cursor invalid")
        )
        raise InvalidSourceResponseError() from diagnostic


__all__ = ["SpotifySource"]
