"""Bounded Spotify release discovery for the existing local watchlist."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256

from music_friend.domain import (
    DAILY_REFRESH_MINUTES,
    Artist,
    ArtistReleaseDiscoveryResult,
    Release,
    ReleaseCandidate,
    ReleaseCandidateKind,
    ReleaseCheckContinuation,
    ReleaseCheckCursor,
    ReleaseDiscovery,
    ReleaseDiscoveryResult,
    ReleaseDiscoveryStatus,
    SourceReference,
)
from music_friend.providers import MusicSource, Page
from music_friend.store import Catalog

_SOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_RELEASES_PER_ARTIST = 100
#: Pages fetched for one artist in one run, whatever the pages contain. A source may
#: drop every row of a page (score or date filters), so the kept-record bound above
#: alone never advances on such pages; this bound always does.
_MAX_PAGES_PER_ARTIST = 5
_FIRST_LOOKBACK = timedelta(days=30)
_OVERLAP = timedelta(hours=48)
#: An artist whose releases were successfully checked within this window makes zero source
#: requests on a later full run, cutting requests per artist on a repeated refresh (AC5).
#: Derived from the scheduled full-refresh cadence with a 4-hour margin so a scheduled run
#: that starts slightly earlier than the previous interval still treats every artist as
#: due, instead of silently skipping a whole cycle.
FRESHNESS_TTL = timedelta(minutes=DAILY_REFRESH_MINUTES) - timedelta(hours=4)


class _SourceCallStopped(RuntimeError):
    """Private control flow used when refresh must not make another source call."""


class _ReleaseDiscoveryInterrupted(RuntimeError):
    """Carry completed artist results and the safe local resume point."""

    def __init__(
        self,
        artist_local_id: str,
        completed: tuple[ArtistReleaseDiscoveryResult, ...],
    ) -> None:
        super().__init__()
        self.artist_local_id = artist_local_id
        self.completed = completed


def discover_releases(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    *,
    checked_at: datetime,
    start_artist_local_id: str | None = None,
) -> ReleaseDiscoveryResult:
    """Discover each monitored artist independently without writing inbox state."""
    if not isinstance(catalog, Catalog):
        raise ValueError("catalog must be a Catalog")
    if type(source_name) is not str or _SOURCE_NAME.fullmatch(source_name) is None:
        raise ValueError("source_name must be a bounded source identifier")
    if not isinstance(source, MusicSource):
        raise ValueError("source must implement the music source contract")
    _require_aware(checked_at, "checked_at")

    entries = catalog._watchlist_entries()
    start = 0
    if start_artist_local_id is not None:
        for position, entry in enumerate(entries):
            if entry.artist.local_id == start_artist_local_id:
                start = position
                break
    completed: list[ArtistReleaseDiscoveryResult] = []
    for entry in entries[start:]:
        if not any(reference.source == source_name for reference in entry.artist.source_refs):
            # Not yet mapped to this release source (e.g. an artist identity
            # mapping hasn't resolved this artist to a musicbrainz identity
            # yet): skip it entirely rather than counting it as a failure.
            # Mapping and its retry window are identity_mapping's job, not
            # discover_releases'.
            continue
        try:
            completed.append(
                _discover_artist(catalog, source_name, source, entry.artist, checked_at)
            )
        except _SourceCallStopped:
            raise _ReleaseDiscoveryInterrupted(entry.artist.local_id, tuple(completed)) from None
    return ReleaseDiscoveryResult(tuple(completed))


def _discover_artist(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    artist: Artist,
    checked_at: datetime,
) -> ArtistReleaseDiscoveryResult:
    try:
        source_reference = _source_reference(artist, source_name)
        continuation = catalog.get_release_check_continuation(source_name, artist.local_id)
        cursor = catalog.get_release_check_cursor(source_name, artist.local_id)
        if (
            continuation is None
            and cursor is not None
            and checked_at - cursor.last_successful_at < FRESHNESS_TTL
        ):
            # Fresh within the TTL: skip the source entirely. No request, no state change.
            return ArtistReleaseDiscoveryResult(
                artist.local_id, ReleaseDiscoveryStatus.SUCCESS, 0, (), None
            )
        if continuation is not None:
            since = continuation.since
            source_cursor = continuation.cursor
        elif cursor is None:
            since = checked_at - _FIRST_LOOKBACK
            source_cursor = None
        else:
            since = cursor.last_successful_at - _OVERLAP
            source_cursor = None
        releases, records_seen, next_cursor = _collect_artist_releases(
            source,
            source_reference,
            since,
            cursor=source_cursor,
        )
        with catalog.transaction():
            candidates = _persist_artist_releases(
                catalog,
                source_name,
                artist.local_id,
                releases,
                checked_at,
            )
            if next_cursor is None:
                catalog.put_release_check_cursor(
                    ReleaseCheckCursor(source_name, artist.local_id, checked_at)
                )
                catalog.remove_release_check_continuation(source_name, artist.local_id)
            else:
                catalog.put_release_check_continuation(
                    ReleaseCheckContinuation(source_name, artist.local_id, next_cursor, since)
                )
        if next_cursor is not None:
            return ArtistReleaseDiscoveryResult(
                artist.local_id,
                ReleaseDiscoveryStatus.PARTIAL,
                records_seen,
                candidates,
                next_cursor,
            )
        return ArtistReleaseDiscoveryResult(
            artist.local_id,
            ReleaseDiscoveryStatus.SUCCESS,
            records_seen,
            candidates,
            None,
        )
    except _SourceCallStopped:
        raise
    except Exception:
        return ArtistReleaseDiscoveryResult(
            artist.local_id,
            ReleaseDiscoveryStatus.FAILED,
            0,
            (),
            None,
        )


def _collect_artist_releases(
    source: MusicSource,
    artist_reference: SourceReference,
    since: datetime,
    *,
    cursor: str | None,
) -> tuple[tuple[Release, ...], int, str | None]:
    releases: list[Release] = []
    records_seen = 0
    seen_cursors = set() if cursor is None else {cursor}
    seen_native_ids: set[str] = set()
    pages = 0
    while True:
        page = source.recent_releases((artist_reference,), since, cursor)
        pages += 1
        if type(page) is not Page:
            raise ValueError("source returned an invalid release page")
        remaining = _MAX_RELEASES_PER_ARTIST - records_seen
        for item in page.items[:remaining]:
            records_seen += 1
            if not isinstance(item, Release):
                raise ValueError("source returned an invalid release")
            native_id = _release_native_id(item, artist_reference.source)
            if native_id not in seen_native_ids:
                seen_native_ids.add(native_id)
                releases.append(item)
        if len(page.items) > remaining:
            raise ValueError("release page exceeds the operational record bound")
        if page.next_cursor is None:
            return tuple(releases), records_seen, None
        if records_seen == _MAX_RELEASES_PER_ARTIST or pages >= _MAX_PAGES_PER_ARTIST:
            return tuple(releases), records_seen, page.next_cursor
        if page.next_cursor in seen_cursors:
            raise ValueError("source release pagination did not advance")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _persist_artist_releases(
    catalog: Catalog,
    source_name: str,
    artist_local_id: str,
    releases: tuple[Release, ...],
    checked_at: datetime,
) -> tuple[ReleaseCandidate, ...]:
    candidates: list[ReleaseCandidate] = []
    with catalog.transaction():
        for release in releases:
            native_id = _release_native_id(release, source_name)
            normalized_title = _normalized_title(release.title)
            existing = catalog.get_release_discovery_by_provider(source_name, native_id)
            if existing is None:
                existing = catalog.find_release_discovery_variant(
                    source_name,
                    artist_local_id,
                    normalized_title,
                    release.release_date,
                    release.date_precision,
                )
                if existing is not None:
                    # Cross-source match (issue #42): attach this source's
                    # SourceReference to the already-known release rather than
                    # creating a new release or inbox item. Same-source variant
                    # matches (the pre-existing behavior) are a no-op here since
                    # the release already carries this source's reference.
                    _attach_source_reference(
                        catalog, existing.release_local_id, release, source_name
                    )
                    catalog.put_release_discovery(replace(existing, last_seen_at=checked_at))
                    continue

            persisted = _for_persistence(catalog, release, artist_local_id)
            material_identity = _material_identity(persisted, source_name, native_id)
            if existing is None:
                catalog.put_release(persisted)
                catalog.put_release_discovery(
                    ReleaseDiscovery(
                        persisted.local_id,
                        source_name,
                        native_id,
                        normalized_title,
                        persisted.release_date,
                        material_identity,
                        checked_at,
                        checked_at,
                    )
                )
                candidates.append(
                    ReleaseCandidate(persisted, artist_local_id, ReleaseCandidateKind.NEW)
                )
                continue

            existing_release = catalog.get_release(existing.release_local_id)
            if existing_release is None:
                raise ValueError("stored release discovery target is missing")
            if existing.release_local_id != persisted.local_id:
                raise ValueError("source release identity conflicts with stored state")
            catalog.put_release(persisted)
            catalog.put_release_discovery(
                ReleaseDiscovery(
                    persisted.local_id,
                    source_name,
                    native_id,
                    normalized_title,
                    persisted.release_date,
                    material_identity,
                    existing.first_seen_at,
                    checked_at,
                )
            )
            if existing.material_identity != material_identity:
                candidates.append(
                    ReleaseCandidate(persisted, artist_local_id, ReleaseCandidateKind.UPDATED)
                )
    return tuple(candidates)


def _attach_source_reference(
    catalog: Catalog,
    release_local_id: str,
    discovered: Release,
    source_name: str,
) -> None:
    """Merge ``discovered``'s SourceReference for ``source_name`` onto the already-
    stored release at ``release_local_id`` (issue #42 cross-source dedupe), unless
    it already carries one for that source."""
    stored = catalog.get_release(release_local_id)
    if stored is None:
        raise ValueError("stored release discovery target is missing")
    if any(ref.source == source_name for ref in stored.source_refs):
        return
    new_ref = next(ref for ref in discovered.source_refs if ref.source == source_name)
    catalog.put_release(
        Release(
            stored.local_id,
            stored.title,
            stored.release_type,
            stored.release_date,
            stored.date_precision,
            stored.artist_refs,
            stored.source_refs + (new_ref,),
            stored.observed_at,
        )
    )


def _source_reference(artist: Artist, source_name: str) -> SourceReference:
    references = tuple(
        reference for reference in artist.source_refs if reference.source == source_name
    )
    if len(references) != 1:
        raise ValueError("monitored artist has invalid source provenance")
    return references[0]


def _release_native_id(release: Release, source_name: str) -> str:
    references = tuple(
        reference for reference in release.source_refs if reference.source == source_name
    )
    if len(references) != 1:
        raise ValueError("release has invalid source provenance")
    return references[0].native_id


def _for_persistence(catalog: Catalog, release: Release, artist_local_id: str) -> Release:
    known_artist_ids = tuple(
        artist_id for artist_id in release.artist_refs if catalog.get_artist(artist_id) is not None
    )
    artist_ids = known_artist_ids if artist_local_id in known_artist_ids else (artist_local_id,)
    if artist_ids == release.artist_refs:
        return release
    return Release(
        release.local_id,
        release.title,
        release.release_type,
        release.release_date,
        release.date_precision,
        artist_ids,
        release.source_refs,
        release.observed_at,
    )


def _normalized_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def _material_identity(release: Release, source_name: str, native_id: str) -> str:
    source_reference = next(
        reference for reference in release.source_refs if reference.source == source_name
    )
    material = {
        "artist_refs": release.artist_refs,
        "native_id": native_id,
        "release_date": release.release_date.isoformat(),
        "release_type": release.release_type,
        "source_url": source_reference.canonical_url,
        "title": release.title,
        "version": 1,
    }
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"release:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["discover_releases"]
