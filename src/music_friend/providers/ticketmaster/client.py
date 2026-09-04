"""Normalized Ticketmaster event discovery contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from music_friend.configuration import LocalConfig
from music_friend.errors import InvalidSourceResponseError
from music_friend.providers.credentials import CredentialKey, CredentialStore
from music_friend.providers.ticketmaster.transport import (
    TicketmasterOperation,
    TicketmasterTransport,
)
from music_friend.providers.ticketmaster.urls import sanitize_ticketmaster_url

TICKETMASTER_CREDENTIAL_KEY = CredentialKey("ticketmaster", "discovery")
_MINIMUM_INTERVAL_SECONDS = 0.5
_EVENT_WINDOW = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class TicketmasterAttraction:
    """One exact-name music attraction resolved by the Ticketmaster adapter."""

    native_id: str
    name: str

    def __post_init__(self) -> None:
        if type(self.native_id) is not str or not self.native_id or len(self.native_id) > 256:
            raise ValueError("native_id must contain 1..256 characters")
        if type(self.name) is not str or not self.name.strip() or len(self.name) > 256:
            raise ValueError("name must contain 1..256 characters")


@dataclass(frozen=True, slots=True)
class TicketmasterEvent:
    """The limited, normalized event fields consumed by Music Friend v1."""

    native_id: str
    title: str
    starts_at: datetime | None
    time_precision: str | None
    venue_name: str | None
    locality: str | None
    source_url: str | None
    purchase_url: str | None
    attribution: str | None

    def __post_init__(self) -> None:
        if type(self.native_id) is not str or not self.native_id or len(self.native_id) > 256:
            raise ValueError("native_id must contain 1..256 characters")
        if type(self.title) is not str or not self.title.strip() or len(self.title) > 4096:
            raise ValueError("title must contain 1..4096 characters")
        if self.starts_at is None:
            if self.time_precision is not None:
                raise ValueError("time_precision requires starts_at")
        elif (
            not isinstance(self.starts_at, datetime)
            or self.starts_at.tzinfo is None
            or self.starts_at.utcoffset() is None
            or self.time_precision not in {"date", "hour", "minute", "second"}
        ):
            raise ValueError("starts_at must be timezone-aware with a valid precision")
        for name in ("venue_name", "locality", "source_url", "purchase_url", "attribution"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not str or not value.strip() or len(value) > 4096
            ):
                raise ValueError(f"{name} must be bounded text or None")


@runtime_checkable
class TicketmasterClient(Protocol):
    """Optional event-only provider boundary that owns protected credential access."""

    def is_configured(self) -> bool: ...

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]: ...

    def events_for_attraction(
        self, attraction: TicketmasterAttraction, config: LocalConfig
    ) -> tuple[TicketmasterEvent, ...]: ...


class TicketmasterDiscoveryClient:
    """Read Ticketmaster events with protected-key access and conservative pacing."""

    def __init__(
        self,
        transport: TicketmasterTransport,
        credential_store: CredentialStore,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(transport, TicketmasterTransport):
            raise ValueError("transport must be a TicketmasterTransport")
        if not isinstance(credential_store, CredentialStore):
            raise ValueError("credential_store must implement CredentialStore")
        if not callable(now):
            raise ValueError("now must be callable")
        import time

        self._transport = transport
        self._credentials = credential_store
        self._now = now
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._last_request_started: float | None = None

    def is_configured(self) -> bool:
        """Report whether a protected nonempty Ticketmaster key is available locally."""
        try:
            value = self._credentials.load(TICKETMASTER_CREDENTIAL_KEY)
        except Exception:
            return False
        return type(value) is str and bool(value.strip()) and len(value) <= 4096

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]:
        """Resolve only music attractions; later discovery requires one exact local-name match."""
        if type(artist_name) is not str or not artist_name.strip() or len(artist_name) > 256:
            raise ValueError("artist_name must contain 1..256 characters")
        data = self._request(
            TicketmasterOperation.ATTRACTIONS,
            (
                ("keyword", artist_name),
                ("segmentName", "Music"),
                ("size", "10"),
            ),
        )
        attractions: list[TicketmasterAttraction] = []
        for item in _embedded_items(data, "attractions"):
            native_id = item.get("id")
            name = item.get("name")
            if _is_music_attraction(item) and type(native_id) is str and type(name) is str:
                attractions.append(TicketmasterAttraction(native_id=native_id, name=name))
        return tuple(attractions)

    def events_for_attraction(
        self, attraction: TicketmasterAttraction, config: LocalConfig
    ) -> tuple[TicketmasterEvent, ...]:
        """Read the configured home-area events within the fixed 365-day discovery window."""
        if not isinstance(attraction, TicketmasterAttraction):
            raise ValueError("attraction must be a TicketmasterAttraction")
        if type(config) is not LocalConfig:
            raise ValueError("config must be LocalConfig")
        if (
            config.event_country_code is None
            or config.event_postal_code is None
            or config.event_radius is None
            or config.event_radius_unit is None
        ):
            raise ValueError("event search area must be configured")
        now = self._aware_now()
        data = self._request(
            TicketmasterOperation.EVENTS,
            (
                ("attractionId", attraction.native_id),
                ("countryCode", config.event_country_code),
                ("postalCode", config.event_postal_code),
                ("radius", _radius_text(config.event_radius)),
                ("unit", config.event_radius_unit),
                ("startDateTime", _api_time(now)),
                ("endDateTime", _api_time(now + _EVENT_WINDOW)),
                ("size", "200"),
            ),
        )
        return tuple(_normalize_event(item) for item in _embedded_items(data, "events"))

    def _request(
        self, operation: TicketmasterOperation, query: tuple[tuple[str, str], ...]
    ) -> dict[str, object]:
        key = self._load_key()
        self._pace()
        return self._transport.execute(operation, query=(("apikey", key), *query))

    def _load_key(self) -> str:
        try:
            value = self._credentials.load(TICKETMASTER_CREDENTIAL_KEY)
        except Exception as error:
            raise ValueError("Ticketmaster credentials are unavailable") from error
        if type(value) is not str or not value.strip() or len(value) > 4096:
            raise ValueError("Ticketmaster credentials are unavailable")
        return value

    def _pace(self) -> None:
        now = self._monotonic()
        if type(now) not in {int, float}:
            raise ValueError("monotonic clock is invalid")
        current = float(now)
        if self._last_request_started is not None:
            wait = _MINIMUM_INTERVAL_SECONDS - (current - self._last_request_started)
            if wait > 0:
                self._sleeper(wait)
                current = float(self._monotonic())
        self._last_request_started = current

    def _aware_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


def _embedded_items(data: dict[str, object], key: str) -> tuple[dict[str, object], ...]:
    embedded = data.get("_embedded")
    if embedded is None:
        return ()
    if type(embedded) is not dict:
        raise InvalidSourceResponseError()
    items = embedded.get(key)
    if items is None:
        return ()
    if type(items) is not list or len(items) > 200:
        raise InvalidSourceResponseError()
    if not all(type(item) is dict for item in items):
        raise InvalidSourceResponseError()
    return tuple(items)


def _is_music_attraction(item: dict[str, object]) -> bool:
    classifications = item.get("classifications")
    if type(classifications) is not list:
        return False
    for classification in classifications:
        if type(classification) is not dict:
            continue
        segment = classification.get("segment")
        if type(segment) is dict and segment.get("name") == "Music":
            return True
    return False


def _normalize_event(item: dict[str, object]) -> TicketmasterEvent:
    native_id = item.get("id")
    title = item.get("name")
    if type(native_id) is not str or type(title) is not str:
        raise InvalidSourceResponseError()
    starts_at, time_precision = _start_time(item)
    venue_name, locality = _venue(item)
    source_url = sanitize_ticketmaster_url(item.get("url"))
    attribution = item.get("attribution")
    if attribution is not None and type(attribution) is not str:
        raise InvalidSourceResponseError()
    return TicketmasterEvent(
        native_id=native_id,
        title=title,
        starts_at=starts_at,
        time_precision=time_precision,
        venue_name=venue_name,
        locality=locality,
        source_url=source_url,
        purchase_url=source_url,
        attribution=attribution,
    )


def _start_time(item: dict[str, object]) -> tuple[datetime | None, str | None]:
    dates = item.get("dates")
    if type(dates) is not dict or type(dates.get("start")) is not dict:
        return None, None
    value = dates["start"].get("dateTime")
    if type(value) is not str:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None
    return parsed, "second"


def _venue(item: dict[str, object]) -> tuple[str | None, str | None]:
    embedded = item.get("_embedded")
    if type(embedded) is not dict or type(embedded.get("venues")) is not list:
        return None, None
    venues = embedded["venues"]
    if not venues or type(venues[0]) is not dict:
        return None, None
    venue = venues[0]
    name = venue.get("name")
    city = venue.get("city")
    locality = city.get("name") if type(city) is dict else None
    return (name if type(name) is str else None, locality if type(locality) is str else None)


def _radius_text(value: int | float) -> str:
    return str(int(value)) if type(value) is int or value.is_integer() else str(value)


def _api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "TicketmasterAttraction",
    "TicketmasterClient",
    "TicketmasterDiscoveryClient",
    "TicketmasterEvent",
    "TICKETMASTER_CREDENTIAL_KEY",
]
