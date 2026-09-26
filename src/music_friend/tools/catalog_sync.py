"""Provider-neutral catalog evidence synchronization."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from hashlib import sha256

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    CatalogItem,
    CatalogItemBatch,
    CatalogSyncCursor,
    CatalogSyncResult,
    SourceCapability,
    SourceReference,
    SyncCapabilityResult,
    SyncCapabilityStatus,
)
from music_friend.providers import MusicSource, Page
from music_friend.store import Catalog
from music_friend.tools.release_discovery import FRESHNESS_TTL

#: A capability that completed successfully within FRESHNESS_TTL (imported from
#: release_discovery so both refresh steps share exactly one TTL constant derived from
#: DAILY_REFRESH_MINUTES) makes zero source requests on a later full run, unless force=True.
_SOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_SYNC_COUNT = 100_000
_EVIDENCE_ID_DOMAIN = "music-friend-affinity-evidence-v1"
_TOP_CAPABILITIES = (
    (
        SourceCapability.TOP_ARTISTS_SHORT_TERM,
        "short_term",
        AffinityEvidenceKind.TOP_SHORT_TERM,
    ),
    (
        SourceCapability.TOP_ARTISTS_MEDIUM_TERM,
        "medium_term",
        AffinityEvidenceKind.TOP_MEDIUM_TERM,
    ),
    (
        SourceCapability.TOP_ARTISTS_LONG_TERM,
        "long_term",
        AffinityEvidenceKind.TOP_LONG_TERM,
    ),
)


@dataclass(slots=True)
class _Progress:
    pages_seen: int = 0
    artists_seen: int = 0
    evidence_count: int = 0

    def increment(self, field: str, amount: int = 1) -> None:
        current = getattr(self, field)
        value = current + amount
        if value > _MAX_SYNC_COUNT:
            raise ValueError("synchronization result exceeds its bound")
        setattr(self, field, value)

    def reserve(self, *, artists: int = 0, evidence: int = 0) -> None:
        next_artists = self.artists_seen + artists
        next_evidence = self.evidence_count + evidence
        if next_artists > _MAX_SYNC_COUNT or next_evidence > _MAX_SYNC_COUNT:
            raise ValueError("synchronization result exceeds its bound")
        self.artists_seen = next_artists
        self.evidence_count = next_evidence


def synchronize_catalog(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    *,
    checked_at: datetime | None = None,
    force: bool = False,
) -> CatalogSyncResult:
    """Synchronize each catalog capability independently and return safe closed results."""
    if not isinstance(catalog, Catalog):
        raise ValueError("catalog must be a Catalog")
    if type(source_name) is not str or _SOURCE_NAME.fullmatch(source_name) is None:
        raise ValueError("source_name must be a bounded source identifier")
    if not isinstance(source, MusicSource):
        raise ValueError("source must implement the music source contract")
    if checked_at is not None:
        _require_aware(checked_at, "checked_at")
    if type(force) is not bool:
        raise ValueError("force must be a boolean")

    results = [
        _run_capability(
            catalog,
            source_name,
            SourceCapability.FOLLOWED_ARTISTS,
            lambda progress: _sync_followed(catalog, source_name, source, progress),
            checked_at=checked_at,
            force=force,
        ),
        _run_capability(
            catalog,
            source_name,
            SourceCapability.SAVED_ITEMS,
            lambda progress: _sync_saved(catalog, source_name, source, progress),
            checked_at=checked_at,
            force=force,
        ),
    ]
    results.extend(
        _run_capability(
            catalog,
            source_name,
            capability,
            partial(
                _sync_top_artists,
                catalog,
                source_name,
                source,
                time_range,
                kind,
            ),
            checked_at=checked_at,
            force=force,
        )
        for capability, time_range, kind in _TOP_CAPABILITIES
    )
    return CatalogSyncResult(tuple(results))


def _run_capability(
    catalog: Catalog,
    source_name: str,
    capability: SourceCapability,
    operation: Callable[[_Progress], None],
    *,
    checked_at: datetime | None = None,
    force: bool = False,
) -> SyncCapabilityResult:
    # Skip the capability entirely when it completed successfully within the TTL.
    if checked_at is not None and not force:
        cursor = catalog.get_catalog_sync_cursor(source_name, capability.value)
        if (
            isinstance(cursor, CatalogSyncCursor)
            and checked_at - cursor.last_successful_at < FRESHNESS_TTL
        ):
            # Fresh within TTL: skip the source entirely
            return SyncCapabilityResult(
                capability,
                SyncCapabilityStatus.SKIPPED_FRESH,
                0,
                0,
                0,
            )

    progress = _Progress()
    try:
        operation(progress)
    except Exception:
        status = SyncCapabilityStatus.FAILED
    else:
        status = SyncCapabilityStatus.SUCCESS
        # Only a full success advances the freshness cursor; a failed run must be retried.
        if checked_at is not None:
            catalog.put_catalog_sync_cursor(
                CatalogSyncCursor(source_name, capability.value, checked_at)
            )

    return SyncCapabilityResult(
        capability,
        status,
        progress.pages_seen,
        progress.artists_seen,
        progress.evidence_count,
    )


def _sync_followed(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    progress: _Progress,
) -> None:
    artists: dict[str, Artist] = {}
    evidence: dict[str, AffinityEvidence] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = source.followed_artists(cursor)
        if type(page) is not Page:
            raise ValueError("source returned an invalid followed page")
        progress.increment("pages_seen")
        for artist in page.items:
            if not isinstance(artist, Artist):
                raise ValueError("source returned an invalid artist")
            native_id = _native_id(artist.source_refs, source_name)
            item_evidence = _evidence(
                source_name,
                AffinityEvidenceKind.FOLLOWED,
                artist.local_id,
                native_id,
                None,
                artist.observed_at,
            )
            progress.reserve(
                artists=int(artist.local_id not in artists),
                evidence=int(artist.local_id not in evidence),
            )
            artists[artist.local_id] = artist
            evidence[artist.local_id] = item_evidence
        if page.next_cursor is None:
            break
        if page.next_cursor in seen_cursors:
            raise ValueError("source pagination did not advance")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    _persist_replacement(
        catalog,
        source_name,
        AffinityEvidenceKind.FOLLOWED,
        tuple(artists.values()),
        tuple(evidence.values()),
    )


def _sync_saved(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    progress: _Progress,
) -> None:
    artists: dict[str, Artist] = {}
    evidence: dict[tuple[str, str], AffinityEvidence] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        batch = source.saved_items(cursor)
        if type(batch) is not CatalogItemBatch:
            raise ValueError("source returned an invalid saved-item batch")
        progress.increment("pages_seen")
        for artist in batch.artists:
            progress.reserve(artists=int(artist.local_id not in artists))
            artists[artist.local_id] = artist
        for item in batch.items:
            _collect_saved_evidence(evidence, item, source_name, progress)
        if batch.next_cursor is None:
            break
        if batch.next_cursor in seen_cursors:
            raise ValueError("source pagination did not advance")
        seen_cursors.add(batch.next_cursor)
        cursor = batch.next_cursor
    _persist_replacement(
        catalog,
        source_name,
        AffinityEvidenceKind.SAVED_TRACK,
        tuple(artists.values()),
        tuple(evidence.values()),
    )


def _collect_saved_evidence(
    evidence: dict[tuple[str, str], AffinityEvidence],
    item: CatalogItem,
    source_name: str,
    progress: _Progress,
) -> None:
    native_id = _native_id(item.source_refs, source_name)
    for artist_local_id in item.artist_refs:
        key = (artist_local_id, native_id)
        item_evidence = _evidence(
            source_name,
            AffinityEvidenceKind.SAVED_TRACK,
            artist_local_id,
            native_id,
            None,
            item.observed_at,
        )
        progress.reserve(evidence=int(key not in evidence))
        evidence[key] = item_evidence


def _sync_top_artists(
    catalog: Catalog,
    source_name: str,
    source: MusicSource,
    time_range: str,
    kind: AffinityEvidenceKind,
    progress: _Progress,
) -> None:
    page = source.top_artists(time_range, 50)
    if type(page) is not Page or len(page.items) > 50:
        raise ValueError("source returned an invalid top-artist page")
    progress.increment("pages_seen")
    artists: dict[str, Artist] = {}
    ranked: dict[str, AffinityEvidence] = {}
    for rank, artist in enumerate(page.items, start=1):
        if not isinstance(artist, Artist):
            raise ValueError("source returned an invalid artist")
        if artist.local_id in ranked:
            continue
        native_id = _native_id(artist.source_refs, source_name)
        item_evidence = _evidence(
            source_name,
            kind,
            artist.local_id,
            native_id,
            rank,
            artist.observed_at,
        )
        progress.reserve(
            artists=int(artist.local_id not in artists),
            evidence=1,
        )
        artists[artist.local_id] = artist
        ranked[artist.local_id] = item_evidence
    _persist_replacement(
        catalog,
        source_name,
        kind,
        tuple(artists.values()),
        tuple(ranked.values()),
    )


def _persist_replacement(
    catalog: Catalog,
    source_name: str,
    kind: AffinityEvidenceKind,
    artists: tuple[Artist, ...],
    evidence: tuple[AffinityEvidence, ...],
) -> None:
    with catalog.transaction():
        for artist in artists:
            catalog.put_artist(artist)
        catalog.replace_affinity_evidence(source_name, kind, evidence)


def _native_id(references: tuple[SourceReference, ...], source_name: str) -> str:
    native_ids = tuple(
        reference.native_id for reference in references if reference.source == source_name
    )
    if len(native_ids) != 1:
        raise ValueError("source record has invalid provenance")
    return native_ids[0]


def _evidence(
    source_name: str,
    kind: AffinityEvidenceKind,
    artist_local_id: str,
    evidence_key: str,
    rank: int | None,
    observed_at: datetime,
) -> AffinityEvidence:
    material = "\0".join(
        (_EVIDENCE_ID_DOMAIN, source_name, kind.value, artist_local_id, evidence_key)
    ).encode("utf-8")
    local_id = f"mf-evidence:{sha256(material).hexdigest()}"
    return AffinityEvidence(
        local_id,
        artist_local_id,
        source_name,
        kind,
        evidence_key,
        rank,
        observed_at,
    )


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["synchronize_catalog"]
