from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    EventCandidateKind,
    EventDiscoveryStatus,
    IdentityConfidence,
    SourceReference,
)
from music_friend.providers.ticketmaster import (
    TicketmasterAttraction,
    TicketmasterDiscoveryClient,
    TicketmasterEvent,
)
from music_friend.providers.ticketmaster.transport import TicketmasterTransport
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.event_discovery import discover_ticketmaster_events

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _artist(native_id: str, name: str | None = None) -> Artist:
    return Artist(
        f"artist:{native_id}",
        name or native_id,
        (SourceReference("spotify", native_id, None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _watch(application: MusicFriendApplication, artist: Artist) -> None:
    application.put_artist(artist)
    application.put_affinity_evidence(
        AffinityEvidence(
            f"evidence:{artist.local_id}",
            artist.local_id,
            "spotify",
            AffinityEvidenceKind.FOLLOWED,
            artist.source_refs[0].native_id,
            None,
            NOW,
        )
    )


def _config() -> LocalConfig:
    return LocalConfig(
        event_country_code="US",
        event_postal_code="94103",
        event_radius=50,
        event_radius_unit="miles",
    )


class FakeTicketmasterClient:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.attractions: dict[str, tuple[TicketmasterAttraction, ...] | Exception] = {}
        self.events: dict[str, tuple[TicketmasterEvent, ...] | Exception] = {}
        self.calls: list[tuple[str, object]] = []

    def is_configured(self) -> bool:
        self.calls.append(("is_configured", None))
        return self.available

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]:
        self.calls.append(("attractions", artist_name))
        response = self.attractions[artist_name]
        if isinstance(response, Exception):
            raise response
        return response

    def events_for_attraction(
        self,
        attraction: TicketmasterAttraction,
        config: LocalConfig,
    ) -> tuple[TicketmasterEvent, ...]:
        self.calls.append(("events", (attraction.native_id, config)))
        response = self.events[attraction.native_id]
        if isinstance(response, Exception):
            raise response
        return response


class MemoryCredentialStore:
    def __init__(self, value: str) -> None:
        self.value = value

    def save(self, _key: object, value: str) -> None:
        self.value = value

    def load(self, _key: object) -> str:
        return self.value

    def delete(self, _key: object) -> None:
        self.value = ""


def _attraction(native_id: str, name: str) -> TicketmasterAttraction:
    return TicketmasterAttraction(native_id=native_id, name=name)


def _event(
    native_id: str,
    *,
    title: str = "Show",
    starts_at: datetime = NOW + timedelta(days=10),
    venue_name: str = "Venue",
) -> TicketmasterEvent:
    return TicketmasterEvent(
        native_id=native_id,
        title=title,
        starts_at=starts_at,
        time_precision="minute",
        venue_name=venue_name,
        locality="San Francisco",
        source_url=f"https://www.ticketmaster.com/event/{native_id}",
        purchase_url=f"https://www.ticketmaster.com/event/{native_id}/buy",
        attribution="Ticketmaster",
    )


def test_event_discovery_skips_without_complete_optional_setup_and_never_egresses(
    tmp_path: Path,
) -> None:
    """Catches an optional event check that calls a provider before setup is complete."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))
        client = FakeTicketmasterClient()

        result = discover_ticketmaster_events(
            catalog,
            config=LocalConfig(event_country_code="US"),
            client=client,
            checked_at=NOW,
        )

        assert result.status is EventDiscoveryStatus.SKIPPED
        assert result.artists == ()
        assert client.calls == []


def test_event_discovery_skips_without_protected_key_and_never_queries_provider(
    tmp_path: Path,
) -> None:
    """Catches a missing protected key that still starts attraction or event discovery."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))
        client = FakeTicketmasterClient(available=False)

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        assert result.status is EventDiscoveryStatus.SKIPPED
        assert result.artists == ()
        assert client.calls == [("is_configured", None)]


def test_event_discovery_normalizes_new_events_and_uses_configured_search_area(
    tmp_path: Path,
) -> None:
    """Catches discovery that drops location, purchase, attribution, or event provenance."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (_event("event-1"),)

        result = discover_ticketmaster_events(
            catalog,
            config=_config(),
            client=client,
            checked_at=NOW,
        )

        artist_result = result.artists[0]
        assert result.status is EventDiscoveryStatus.SUCCESS
        assert artist_result.status is EventDiscoveryStatus.SUCCESS
        assert artist_result.candidates[0].kind is EventCandidateKind.NEW
        event = artist_result.candidates[0].event
        assert event.artist_refs == (artist.local_id,)
        assert event.starts_at == NOW + timedelta(days=10)
        assert event.venue_name == "Venue"
        assert event.locality == "San Francisco"
        assert event.source_links == (
            "https://www.ticketmaster.com/event/event-1",
            "https://www.ticketmaster.com/event/event-1/buy",
        )
        assert event.source_refs[0].source == "ticketmaster"
        assert event.source_refs[0].native_id == "event-1"
        assert client.calls[-1] == ("events", ("attraction-1", _config()))


def test_event_discovery_defaults_a_kilometer_search_area_to_eighty_kilometers(
    tmp_path: Path,
) -> None:
    """Catches a kilometer preference that silently receives the fifty-mile default."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (_event("event-1"),)
        config = LocalConfig(
            event_country_code="US",
            event_postal_code="94103",
            event_radius_unit="kilometers",
        )

        discover_ticketmaster_events(catalog, config=config, client=client, checked_at=NOW)

        call = client.calls[-1]
        assert call[0] == "events"
        assert isinstance(call[1], tuple)
        configured_area = call[1][1]
        assert isinstance(configured_area, LocalConfig)
        assert configured_area.event_radius == 80
        assert configured_area.event_radius_unit == "kilometers"


def test_event_discovery_uses_six_hour_cache_then_revalidates_after_expiry(tmp_path: Path) -> None:
    """Catches a cache that either refetches too soon or never revalidates stale results."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (_event("event-1"),)

        first = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )
        cached = discover_ticketmaster_events(
            catalog,
            config=_config(),
            client=client,
            checked_at=NOW + timedelta(hours=5, minutes=59),
        )
        refreshed = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW + timedelta(hours=6)
        )

        assert first.artists[0].candidates[0].kind is EventCandidateKind.NEW
        assert cached.artists[0].status is EventDiscoveryStatus.CACHED
        assert cached.artists[0].candidates == ()
        assert refreshed.artists[0].status is EventDiscoveryStatus.SUCCESS
        assert refreshed.artists[0].candidates == ()
        assert [name for name, _ in client.calls].count("attractions") == 2


def test_event_discovery_deduplicates_exact_and_obvious_event_variants(tmp_path: Path) -> None:
    """Catches duplicate candidates for one provider event or same artist/name/time/venue variant."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (
            _event("event-1", title="The  Show"),
            _event("event-1", title="The  Show"),
            _event("event-2", title="the show"),
        )

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        assert [candidate.event.title for candidate in result.artists[0].candidates] == [
            "The  Show"
        ]
        assert result.artists[0].records_seen == 3


def test_event_discovery_treats_repeated_same_attraction_as_one_exact_match(tmp_path: Path) -> None:
    """Catches duplicate provider rows being mistaken for ambiguous artist identity."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        attraction = _attraction("attraction-1", "One")
        client.attractions["One"] = (attraction, attraction)
        client.events["attraction-1"] = (_event("event-1"),)

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        assert result.artists[0].status is EventDiscoveryStatus.SUCCESS
        assert [candidate.event.title for candidate in result.artists[0].candidates] == ["Show"]


def test_event_discovery_emits_updated_only_after_material_change(tmp_path: Path) -> None:
    """Catches treating all rechecks as new or missing an event material change."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (_event("event-1", title="Original"),)
        first = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )
        client.events["attraction-1"] = (_event("event-1", title="Updated"),)
        second = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW + timedelta(hours=6)
        )
        third = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW + timedelta(hours=12)
        )

        assert first.artists[0].candidates[0].kind is EventCandidateKind.NEW
        assert second.artists[0].candidates[0].kind is EventCandidateKind.UPDATED
        assert third.artists[0].candidates == ()


def test_event_discovery_isolates_artist_failures_and_preserves_existing_events(
    tmp_path: Path,
) -> None:
    """Catches one artist's provider failure erasing state or blocking another artist."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        first_artist = _artist("one", "One")
        second_artist = _artist("two", "Two")
        _watch(application, first_artist)
        _watch(application, second_artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.attractions["Two"] = (_attraction("attraction-2", "Two"),)
        client.events["attraction-1"] = (_event("event-1"),)
        client.events["attraction-2"] = (_event("event-2"),)
        first = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )
        retained_event_id = first.artists[0].candidates[0].event.local_id

        client.events["attraction-1"] = RuntimeError("raw-provider-event-canary")
        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW + timedelta(hours=6)
        )

        by_artist = {item.artist_local_id: item for item in result.artists}
        assert by_artist[first_artist.local_id].status is EventDiscoveryStatus.FAILED
        assert by_artist[second_artist.local_id].status is EventDiscoveryStatus.SUCCESS
        assert "raw-provider-event-canary" not in repr(result)
        assert catalog.get_event(retained_event_id) is not None


def test_event_discovery_never_persists_raw_provider_fields_or_keyed_urls(tmp_path: Path) -> None:
    """Catches raw response data or key-bearing provider URLs entering durable event state."""
    raw_payload = "raw-ticketmaster-response-canary"
    protected_key = "protected-ticketmaster-key"
    responses = iter(
        (
            httpx.Response(
                200,
                json={
                    "_embedded": {
                        "attractions": [
                            {
                                "id": "attraction-1",
                                "name": "One",
                                "classifications": [{"segment": {"name": "Music"}}],
                            }
                        ]
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "provider_payload": raw_payload,
                    "_embedded": {
                        "events": [
                            {
                                "id": "event-1",
                                "name": "Show",
                                "url": (
                                    "https://www.ticketmaster.com/event/event-1"
                                    f"?apikey={protected_key}"
                                ),
                            }
                        ]
                    },
                },
            ),
        )
    )
    transport = TicketmasterTransport(httpx.MockTransport(lambda _request: next(responses)))
    client = TicketmasterDiscoveryClient(
        transport,
        MemoryCredentialStore(protected_key),
        now=lambda: NOW,
    )
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        dump = "\n".join(catalog._connection.iterdump())
        assert result.artists[0].candidates
        assert raw_payload not in dump
        assert protected_key not in dump
    transport.close()


def test_event_discovery_sanitizes_keyed_urls_from_any_ticketmaster_client(tmp_path: Path) -> None:
    """Catches a protocol-compatible client storing a credential-like event URL unchanged."""
    query_parameter = "api" + "key"
    query_value = "synthetic-query-value"
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (
            TicketmasterEvent(
                native_id="event-1",
                title="Show",
                starts_at=NOW + timedelta(days=10),
                time_precision="minute",
                venue_name="Venue",
                locality="San Francisco",
                source_url=(
                    f"https://www.ticketmaster.com/event/event-1?{query_parameter}={query_value}"
                ),
                purchase_url=None,
                attribution="Ticketmaster",
            ),
        )

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        event = result.artists[0].candidates[0].event
        dump = "\n".join(catalog._connection.iterdump())
        assert event.source_links == ("https://www.ticketmaster.com/event/event-1",)
        assert event.source_refs[0].canonical_url == "https://www.ticketmaster.com/event/event-1"
        assert query_value not in dump


def test_event_discovery_strips_bidi_and_control_characters_from_names(tmp_path: Path) -> None:
    """Issue #24: a bidi override in a provider-supplied event/venue name must not survive refresh."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (
            TicketmasterEvent(
                native_id="event-1",
                title="Example‮",
                starts_at=NOW + timedelta(days=10),
                time_precision="minute",
                venue_name="Venue⁦Name⁩",
                locality="City​Name",
                source_url="https://www.ticketmaster.com/event/event-1",
                purchase_url=None,
                attribution="Ticketmaster",
            ),
        )

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        event = result.artists[0].candidates[0].event
        assert event.title == "Example"
        assert event.venue_name == "VenueName"
        assert event.locality == "CityName"
        for forbidden in ("‮", "⁦", "⁩", "​"):
            assert forbidden not in event.title
            assert event.venue_name is not None and forbidden not in event.venue_name
            assert event.locality is not None and forbidden not in event.locality


def test_event_discovery_preserves_legitimate_scripts_and_emoji_in_names(tmp_path: Path) -> None:
    """Issue #24: non-Latin scripts and emoji ZWJ sequences must pass through unchanged."""
    japanese_title = "音楽フェス"  # music festival
    emoji_venue = "The \U0001f3a4‍\U0001f3b6 Room"  # microphone+musical-note ZWJ sequence
    arabic_locality = "القاهرة"  # Cairo
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))
        client = FakeTicketmasterClient()
        client.attractions["One"] = (_attraction("attraction-1", "One"),)
        client.events["attraction-1"] = (
            TicketmasterEvent(
                native_id="event-1",
                title=japanese_title,
                starts_at=NOW + timedelta(days=10),
                time_precision="minute",
                venue_name=emoji_venue,
                locality=arabic_locality,
                source_url="https://www.ticketmaster.com/event/event-1",
                purchase_url=None,
                attribution="Ticketmaster",
            ),
        )

        result = discover_ticketmaster_events(
            catalog, config=_config(), client=client, checked_at=NOW
        )

        event = result.artists[0].candidates[0].event
        assert event.title == japanese_title
        assert event.venue_name == emoji_venue
        assert event.locality == arabic_locality


def test_discover_ticketmaster_events_rejects_invalid_inputs(tmp_path: Path) -> None:
    """Catches a malformed catalog, config, or client reaching provider dispatch."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        client = FakeTicketmasterClient()
        with pytest.raises(ValueError, match="catalog"):
            discover_ticketmaster_events(
                object(),
                config=_config(),
                client=client,
                checked_at=NOW,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="config"):
            discover_ticketmaster_events(
                catalog,
                config=object(),
                client=client,
                checked_at=NOW,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="client"):
            discover_ticketmaster_events(
                catalog,
                config=_config(),
                client=object(),
                checked_at=NOW,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            discover_ticketmaster_events(
                catalog,
                config=_config(),
                client=client,
                checked_at=NOW.replace(tzinfo=None),
            )
