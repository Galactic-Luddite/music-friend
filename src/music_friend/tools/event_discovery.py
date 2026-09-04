"""Bounded local Ticketmaster event discovery for the existing watchlist."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    Artist,
    ArtistEventDiscoveryResult,
    Event,
    EventCandidate,
    EventCandidateKind,
    EventDiscovery,
    EventDiscoveryResult,
    EventDiscoveryStatus,
    SourceReference,
)
from music_friend.domain.text import sanitize_source_text
from music_friend.providers.ticketmaster import (
    TicketmasterAttraction,
    TicketmasterClient,
    TicketmasterEvent,
)
from music_friend.providers.ticketmaster.urls import sanitize_ticketmaster_url
from music_friend.store import Catalog

_SOURCE = "ticketmaster"
_CACHE_DURATION = timedelta(hours=6)


def discover_ticketmaster_events(
    catalog: Catalog,
    *,
    config: LocalConfig,
    client: TicketmasterClient,
    checked_at: datetime,
) -> EventDiscoveryResult:
    """Discover each monitored artist independently without writing inbox state."""
    if not isinstance(catalog, Catalog):
        raise ValueError("catalog must be a Catalog")
    if type(config) is not LocalConfig:
        raise ValueError("config must be LocalConfig")
    if not isinstance(client, TicketmasterClient):
        raise ValueError("client must implement TicketmasterClient")
    _require_aware(checked_at, "checked_at")
    search_area = _search_area(config)
    if search_area is None or not client.is_configured():
        return EventDiscoveryResult(EventDiscoveryStatus.SKIPPED, ())

    artist_results = tuple(
        _discover_artist(catalog, client, entry.artist, search_area, checked_at)
        for entry in catalog._watchlist_entries()
    )
    statuses = {result.status for result in artist_results}
    if EventDiscoveryStatus.FAILED in statuses and len(statuses) > 1:
        status = EventDiscoveryStatus.PARTIAL
    elif EventDiscoveryStatus.FAILED in statuses:
        status = EventDiscoveryStatus.FAILED
    else:
        status = EventDiscoveryStatus.SUCCESS
    return EventDiscoveryResult(status, artist_results)


def _search_area(config: LocalConfig) -> LocalConfig | None:
    if config.event_country_code is None or config.event_postal_code is None:
        return None
    return LocalConfig(
        spotify_client_id=config.spotify_client_id,
        event_country_code=config.event_country_code,
        event_postal_code=config.event_postal_code,
        event_radius=(
            (80 if config.event_radius_unit == "kilometers" else 50)
            if config.event_radius is None
            else config.event_radius
        ),
        event_radius_unit="miles" if config.event_radius_unit is None else config.event_radius_unit,
    )


def _discover_artist(
    catalog: Catalog,
    client: TicketmasterClient,
    artist: Artist,
    config: LocalConfig,
    checked_at: datetime,
) -> ArtistEventDiscoveryResult:
    expiry = catalog.get_event_check_expiry(_SOURCE, artist.local_id)
    if expiry is not None and expiry > checked_at:
        return ArtistEventDiscoveryResult(artist.local_id, EventDiscoveryStatus.CACHED, 0, ())
    try:
        attractions = client.resolve_music_attractions(artist.display_name)
        attraction = _unique_exact_attraction(artist, attractions)
        if attraction is None:
            with catalog.transaction():
                catalog.put_event_check_expiry(
                    _SOURCE, artist.local_id, checked_at + _CACHE_DURATION
                )
            return ArtistEventDiscoveryResult(
                artist.local_id, EventDiscoveryStatus.UNMATCHED, 0, ()
            )
        events = client.events_for_attraction(attraction, config)
        with catalog.transaction():
            candidates = _persist_events(catalog, artist, events, checked_at)
            catalog.put_event_check_expiry(_SOURCE, artist.local_id, checked_at + _CACHE_DURATION)
        return ArtistEventDiscoveryResult(
            artist.local_id,
            EventDiscoveryStatus.SUCCESS,
            len(events),
            candidates,
        )
    except Exception:
        return ArtistEventDiscoveryResult(artist.local_id, EventDiscoveryStatus.FAILED, 0, ())


def _unique_exact_attraction(
    artist: Artist, attractions: tuple[TicketmasterAttraction, ...]
) -> TicketmasterAttraction | None:
    if not isinstance(attractions, tuple):
        raise ValueError("client returned invalid attractions")
    exact_by_native_id = {
        attraction.native_id: attraction
        for attraction in attractions
        if isinstance(attraction, TicketmasterAttraction)
        and _normalized_text(attraction.name) == _normalized_text(artist.display_name)
    }
    exact = tuple(exact_by_native_id.values())
    return exact[0] if len(exact) == 1 else None


def _persist_events(
    catalog: Catalog,
    artist: Artist,
    events: tuple[TicketmasterEvent, ...],
    checked_at: datetime,
) -> tuple[EventCandidate, ...]:
    if not isinstance(events, tuple):
        raise ValueError("client returned invalid events")
    candidates: list[EventCandidate] = []
    seen_native_ids: set[str] = set()
    seen_variant_ids: set[str] = set()
    for item in events:
        if not isinstance(item, TicketmasterEvent):
            raise ValueError("client returned an invalid event")
        if item.native_id in seen_native_ids:
            continue
        seen_native_ids.add(item.native_id)
        variant_identity = _variant_identity(item)
        if variant_identity in seen_variant_ids:
            continue
        seen_variant_ids.add(variant_identity)
        existing = catalog.get_event_discovery_by_provider(_SOURCE, item.native_id)
        if existing is None:
            existing = catalog.find_event_discovery_variant(
                _SOURCE, artist.local_id, variant_identity
            )
            if existing is not None:
                catalog.put_event_discovery(
                    replace(
                        existing,
                        last_seen_at=checked_at,
                        fetched_at=checked_at,
                        expires_at=checked_at + _CACHE_DURATION,
                    )
                )
                continue
        event = _canonical_event(item, artist, checked_at)
        material_identity = _material_identity(event, item.attribution)
        if existing is None:
            catalog.put_event(event)
            catalog.put_event_discovery(
                EventDiscovery(
                    event.local_id,
                    _SOURCE,
                    item.native_id,
                    artist.local_id,
                    variant_identity,
                    material_identity,
                    _safe_optional_text(item.attribution),
                    checked_at,
                    checked_at,
                    checked_at,
                    checked_at + _CACHE_DURATION,
                )
            )
            candidates.append(EventCandidate(event, artist.local_id, EventCandidateKind.NEW))
            continue
        if existing.event_local_id != event.local_id:
            raise ValueError("provider event identity conflicts with stored state")
        catalog.put_event(event)
        catalog.put_event_discovery(
            EventDiscovery(
                event.local_id,
                _SOURCE,
                item.native_id,
                artist.local_id,
                variant_identity,
                material_identity,
                _safe_optional_text(item.attribution),
                existing.first_seen_at,
                checked_at,
                checked_at,
                checked_at + _CACHE_DURATION,
            )
        )
        if existing.material_identity != material_identity:
            candidates.append(EventCandidate(event, artist.local_id, EventCandidateKind.UPDATED))
    return tuple(candidates)


def _canonical_event(item: TicketmasterEvent, artist: Artist, checked_at: datetime) -> Event:
    source_url = sanitize_ticketmaster_url(item.source_url)
    purchase_url = sanitize_ticketmaster_url(item.purchase_url)
    source_links = tuple(dict.fromkeys(link for link in (source_url, purchase_url) if link))
    canonical_url = source_url if source_url is not None else purchase_url
    return Event(
        _local_id(item.native_id),
        sanitize_source_text(item.title),
        (artist.local_id,),
        _safe_optional_text(item.venue_name),
        _safe_optional_text(item.locality),
        item.starts_at,
        item.time_precision,
        source_links,
        (SourceReference(_SOURCE, item.native_id, canonical_url, checked_at),),
        checked_at,
    )


def _local_id(native_id: str) -> str:
    return f"mf:{sha256(f'{_SOURCE}:{native_id}'.encode('utf-8')).hexdigest()}"


def _variant_identity(item: TicketmasterEvent) -> str:
    value = {
        "artist_event": _normalized_text(item.title),
        "start": None if item.starts_at is None else item.starts_at.isoformat(),
        "venue": _normalized_text(item.venue_name or ""),
        "version": 1,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"event-variant:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _material_identity(event: Event, attribution: str | None) -> str:
    value = {
        "artist_refs": event.artist_refs,
        "attribution": _safe_optional_text(attribution),
        "locality": event.locality,
        "source_links": event.source_links,
        "starts_at": None if event.starts_at is None else event.starts_at.isoformat(),
        "time_precision": event.time_precision,
        "title": event.title,
        "venue_name": event.venue_name,
        "version": 1,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"event:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _safe_optional_text(value: str | None) -> str | None:
    return None if value is None else sanitize_source_text(value, limit=256)


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["discover_ticketmaster_events"]
