"""Shared catalog-backed application boundary for Music Friend interfaces."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    AffinityScore,
    Artist,
    CatalogSyncResult,
    Event,
    EventDiscovery,
    EventDiscoveryResult,
    InboxEntry,
    InboxState,
    LocalPreference,
    LocalPreferenceKey,
    RefreshRun,
    Release,
    ReleaseDiscovery,
    ReleaseDiscoveryResult,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    WatchlistAction,
    WatchlistEntry,
    WatchlistOverride,
)
from music_friend.providers import MusicSource
from music_friend.providers.ticketmaster import TicketmasterClient
from music_friend.store import Catalog
from music_friend.store.portable import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    ExportResult,
    ImportResult,
    PurgeResult,
    delete_catalog,
    export_catalog,
    import_catalog,
    purge_source,
)
from music_friend.store.spotify_history import (
    HistorySummary,
    SpotifyHistoryImportResult,
    import_spotify_history,
    summarize_history,
)
from music_friend.tools.catalog_sync import synchronize_catalog
from music_friend.tools.event_discovery import discover_ticketmaster_events
from music_friend.tools.release_discovery import discover_releases


class MusicFriendApplication:
    """Delegate approved v1 operations to one explicitly supplied catalog."""

    def __init__(self, catalog: Catalog) -> None:
        if not isinstance(catalog, Catalog):
            raise ValueError("catalog must be a Catalog")
        self._catalog = catalog

    def transaction(self) -> AbstractContextManager[None]:
        return self._catalog.transaction()

    def close(self) -> None:
        self._catalog.close()

    def put_artist(self, artist: Artist) -> None:
        self._catalog.put_artist(artist)

    def get_artist(self, local_id: str) -> Artist | None:
        return self._catalog.get_artist(local_id)

    def search_artists(self, query: str, *, limit: int) -> tuple[Artist, ...]:
        return self._catalog.search_artists(query, limit=limit)

    def put_release(self, release: Release) -> None:
        self._catalog.put_release(release)

    def get_release(self, local_id: str) -> Release | None:
        return self._catalog.get_release(local_id)

    def list_release_discoveries_without_current_signal(
        self, *, limit: int
    ) -> tuple[ReleaseDiscovery, ...]:
        return self._catalog.list_release_discoveries_without_current_signal(limit=limit)

    def put_event(self, event: Event) -> None:
        self._catalog.put_event(event)

    def get_event(self, local_id: str) -> Event | None:
        return self._catalog.get_event(local_id)

    def list_event_discoveries_without_current_signal(
        self, *, limit: int
    ) -> tuple[EventDiscovery, ...]:
        return self._catalog.list_event_discoveries_without_current_signal(limit=limit)

    def put_affinity_evidence(self, evidence: AffinityEvidence) -> None:
        self._catalog.put_affinity_evidence(evidence)

    def get_affinity_evidence(self, local_id: str) -> AffinityEvidence | None:
        return self._catalog.get_affinity_evidence(local_id)

    def list_affinity_evidence(
        self, artist_local_id: str, *, limit: int
    ) -> tuple[AffinityEvidence, ...]:
        return self._catalog.list_affinity_evidence(artist_local_id, limit=limit)

    def replace_affinity_evidence(
        self,
        source: str,
        kind: AffinityEvidenceKind,
        evidence: tuple[AffinityEvidence, ...],
    ) -> None:
        self._catalog.replace_affinity_evidence(source, kind, evidence)

    def put_watchlist_override(self, override: WatchlistOverride) -> None:
        self._catalog.put_watchlist_override(override)

    def get_watchlist_override(self, artist_local_id: str) -> WatchlistOverride | None:
        return self._catalog.get_watchlist_override(artist_local_id)

    def list_watchlist_overrides(self, *, limit: int) -> tuple[WatchlistOverride, ...]:
        return self._catalog.list_watchlist_overrides(limit=limit)

    def remove_watchlist_override(self, artist_local_id: str) -> None:
        self._catalog.remove_watchlist_override(artist_local_id)

    def synchronize_catalog(
        self,
        source_name: str,
        source: MusicSource,
        *,
        checked_at: datetime | None = None,
        force: bool = False,
    ) -> CatalogSyncResult:
        return synchronize_catalog(
            self._catalog, source_name, source, checked_at=checked_at, force=force
        )

    def discover_releases(
        self,
        source_name: str,
        source: MusicSource,
        *,
        checked_at: datetime,
        start_artist_local_id: str | None = None,
    ) -> ReleaseDiscoveryResult:
        return discover_releases(
            self._catalog,
            source_name,
            source,
            checked_at=checked_at,
            start_artist_local_id=start_artist_local_id,
        )

    def discover_ticketmaster_events(
        self,
        *,
        config: LocalConfig,
        client: TicketmasterClient,
        checked_at: datetime,
    ) -> EventDiscoveryResult:
        return discover_ticketmaster_events(
            self._catalog,
            config=config,
            client=client,
            checked_at=checked_at,
        )

    def get_affinity_score(self, artist_local_id: str) -> AffinityScore:
        return self._catalog.get_affinity_score(artist_local_id)

    def list_watchlist(self, *, limit: int) -> tuple[WatchlistEntry, ...]:
        return self._catalog.list_watchlist(limit=limit)

    def explain_watchlist(self, artist_local_id: str) -> WatchlistEntry | None:
        return self._catalog.explain_watchlist(artist_local_id)

    def set_watchlist_add(self, artist_local_id: str, *, updated_at: datetime) -> None:
        self._set_watchlist_action(artist_local_id, WatchlistAction.ADD, updated_at)

    def set_watchlist_pin(self, artist_local_id: str, *, updated_at: datetime) -> None:
        self._set_watchlist_action(artist_local_id, WatchlistAction.PIN, updated_at)

    def set_watchlist_mute(self, artist_local_id: str, *, updated_at: datetime) -> None:
        self._set_watchlist_action(artist_local_id, WatchlistAction.MUTE, updated_at)

    def _set_watchlist_action(
        self,
        artist_local_id: str,
        action: WatchlistAction,
        updated_at: datetime,
    ) -> None:
        self._catalog.put_watchlist_override(WatchlistOverride(artist_local_id, action, updated_at))

    def put_local_preference(self, preference: LocalPreference) -> None:
        self._catalog.put_local_preference(preference)

    def get_local_preference(self, key: LocalPreferenceKey) -> LocalPreference | None:
        return self._catalog.get_local_preference(key)

    def list_local_preferences(self, *, limit: int) -> tuple[LocalPreference, ...]:
        return self._catalog.list_local_preferences(limit=limit)

    def remove_local_preference(self, key: LocalPreferenceKey) -> None:
        self._catalog.remove_local_preference(key)

    def put_refresh_run(self, run: RefreshRun) -> None:
        self._catalog.put_refresh_run(run)

    def get_refresh_run(self, local_id: str) -> RefreshRun | None:
        return self._catalog.get_refresh_run(local_id)

    def list_refresh_runs(self, *, limit: int) -> tuple[RefreshRun, ...]:
        return self._catalog.list_refresh_runs(limit=limit)

    def put_source_cursor(self, cursor: SourceCursor) -> None:
        self._catalog.put_source_cursor(cursor)

    def get_source_cursor(self, source: str, capability: SourceCapability) -> SourceCursor | None:
        return self._catalog.get_source_cursor(source, capability)

    def list_source_cursors(self, source: str, *, limit: int) -> tuple[SourceCursor, ...]:
        return self._catalog.list_source_cursors(source, limit=limit)

    def remove_source_cursor(self, source: str, capability: SourceCapability) -> None:
        self._catalog.remove_source_cursor(source, capability)

    def put_source_limit(self, observation: SourceLimitObservation) -> None:
        self._catalog.put_source_limit(observation)

    def get_source_limit(self, source: str) -> SourceLimitObservation | None:
        return self._catalog.get_source_limit(source)

    def put_signal(self, signal: Signal) -> None:
        self._catalog.put_signal(signal)

    def get_signal(self, local_id: str) -> Signal | None:
        return self._catalog.get_signal(local_id)

    def find_signal(
        self,
        provider: str,
        kind: SignalKind,
        provider_native_id: str,
        material_version: str,
    ) -> Signal | None:
        return self._catalog.find_signal(provider, kind, provider_native_id, material_version)

    def list_signals(self, kind: SignalKind | None, *, limit: int) -> tuple[Signal, ...]:
        return self._catalog.list_signals(kind, limit=limit)

    def list_signals_without_inbox_entries(self, *, limit: int) -> tuple[Signal, ...]:
        return self._catalog.list_signals_without_inbox_entries(limit=limit)

    def put_inbox_entry(self, entry: InboxEntry) -> None:
        self._catalog.put_inbox_entry(entry)

    def get_inbox_entry(self, local_id: str) -> InboxEntry | None:
        return self._catalog.get_inbox_entry(local_id)

    def list_inbox_entries(self, state: InboxState | None, *, limit: int) -> tuple[InboxEntry, ...]:
        return self._catalog.list_inbox_entries(state, limit=limit)

    def export_data(
        self,
        destination: Path,
        *,
        replace: bool = False,
        exported_at: datetime | None = None,
    ) -> ExportResult:
        return export_catalog(
            self._catalog,
            destination,
            replace=replace,
            exported_at=exported_at,
        )

    def import_data(
        self,
        source: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> ImportResult:
        return import_catalog(
            self._catalog,
            source,
            max_bytes=max_bytes,
            max_records=max_records,
        )

    def import_spotify_history(
        self, source: Path, *, dry_run: bool = False
    ) -> SpotifyHistoryImportResult:
        return import_spotify_history(self._catalog, source, dry_run=dry_run)

    def summarize_history(
        self, *, since: str | None = None, until: str | None = None, limit: int = 10
    ) -> HistorySummary:
        return summarize_history(self._catalog, since=since, until=until, limit=limit)

    def purge_source(self, source: str) -> PurgeResult:
        return purge_source(self._catalog, source)

    def delete_data(self) -> None:
        delete_catalog(self._catalog)


__all__ = ["MusicFriendApplication"]
