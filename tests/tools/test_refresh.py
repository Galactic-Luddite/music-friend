from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxState,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
)
from music_friend.errors import QuotaExhaustedError, RateLimitedError
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)
from music_friend.providers.ticketmaster import TicketmasterAttraction, TicketmasterEvent
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import (
    RefreshInvocation,
    _acquire_lock,
    _PacedSource,
    _read_lock,
    _release_lock,
    refresh_once,
    update_inbox_state,
)
from music_friend.tools.release_discovery import _SourceCallStopped

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _artist(native_id: str, name: str = "Artist") -> Artist:
    return Artist(
        f"artist:{native_id}",
        name,
        (SourceReference("spotify", native_id, None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _release(native_id: str, artist: Artist, *, title: str = "Release") -> Release:
    return Release(
        f"release:{native_id}",
        title,
        "album",
        date(2026, 8, 31),
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (SourceReference("spotify", native_id, None, NOW),),
        NOW,
    )


def _event(native_id: str, *, title: str = "Show", venue_name: str = "Venue") -> TicketmasterEvent:
    return TicketmasterEvent(
        native_id=native_id,
        title=title,
        starts_at=NOW + timedelta(days=10),
        time_precision="minute",
        venue_name=venue_name,
        locality="City",
        source_url=f"https://example.test/event/{native_id}",
        purchase_url=f"https://example.test/event/{native_id}/tickets",
        attribution="Ticketmaster",
    )


class FakeMusicSource:
    def __init__(self) -> None:
        self.followed: Page[Artist] | Exception = Page((), None)
        self.saved: object = Page((), None)
        self.top: dict[str, Page[Artist] | Exception] = {
            interval: Page((), None) for interval in ("short_term", "medium_term", "long_term")
        }
        self.releases: dict[tuple[str, str | None], Page[Release] | Exception] = {}
        self.calls: list[str] = []
        self.release_calls: list[str] = []

    def capabilities(self) -> ProviderCapabilities:
        allowed = frozenset(Capability)
        return ProviderCapabilities(allowed, allowed)

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self.capabilities())

    def search_artists(self, _query: str, _limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def followed_artists(self, _cursor: str | None = None) -> Page[Artist]:
        self.calls.append("catalog:followed")
        if isinstance(self.followed, Exception):
            raise self.followed
        return self.followed

    def saved_items(self, _cursor: str | None = None) -> object:
        self.calls.append("catalog:saved")
        return self.saved

    def top_items(self, _time_range: str, _limit: int) -> object:
        raise AssertionError("unused")

    def top_artists(self, time_range: str, _limit: int) -> Page[Artist]:
        self.calls.append(f"catalog:{time_range}")
        response = self.top[time_range]
        if isinstance(response, Exception):
            raise response
        return response

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        _since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        self.calls.append("releases")
        self.release_calls.append(artist_refs[0].native_id)
        response = self.releases[(artist_refs[0].native_id, cursor)]
        if isinstance(response, list):
            response = response.pop(0)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeEventClient:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.events: dict[str, tuple[TicketmasterEvent, ...] | Exception] = {}
        self.calls: list[str] = []

    def is_configured(self) -> bool:
        self.calls.append("configured")
        return self.configured

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]:
        self.calls.append("attractions")
        return (TicketmasterAttraction(f"attraction:{artist_name}", artist_name),)

    def events_for_attraction(
        self, attraction: TicketmasterAttraction, _config: LocalConfig
    ) -> tuple[TicketmasterEvent, ...]:
        self.calls.append("events")
        response = self.events[attraction.native_id]
        if isinstance(response, Exception):
            raise response
        return response


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
    return LocalConfig(event_country_code="US", event_postal_code="94103")


def _refresh(
    application: MusicFriendApplication,
    source: FakeMusicSource,
    *,
    kind: str,
    lock_path: Path,
    events: FakeEventClient | None = None,
    monotonic: object | None = None,
    lock_clock: object | None = None,
    sleeper: object | None = None,
    checked_at: datetime = NOW,
) -> RefreshInvocation:
    return refresh_once(
        application,
        kind=kind,
        source_name="spotify",
        source=source,
        config=_config(),
        event_client=events,
        checked_at=checked_at,
        lock_path=lock_path,
        monotonic=monotonic,
        lock_clock=lock_clock,
        sleeper=sleeper,
    )


def test_release_refresh_creates_an_unread_item_then_preserves_saved_and_dismissed_history(
    tmp_path: Path,
) -> None:
    """Catches a rerun that loses an inbox decision or treats an unchanged release as new."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)

        first = _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        assert first.already_running is False
        assert first.run is not None
        entries = application.list_inbox_entries(None, limit=10)
        assert len(entries) == 1
        assert entries[0].state is InboxState.UNREAD
        saved = update_inbox_state(
            application, entries[0].local_id, InboxState.SAVED, updated_at=NOW
        )
        dismissed = update_inbox_state(
            application, saved.local_id, InboxState.DISMISSED, updated_at=NOW + timedelta(minutes=1)
        )

        second = _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        assert second.run is not None
        assert application.list_inbox_entries(None, limit=10) == (dismissed,)


def test_materially_changed_release_creates_a_new_unread_item_without_reopening_dismissed_history(
    tmp_path: Path,
) -> None:
    """Catches a changed discovery that reuses the old dismissed signal or fails to surface it."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Original"),), None
        )
        _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")
        original = application.list_inbox_entries(None, limit=10)[0]
        update_inbox_state(application, original.local_id, InboxState.DISMISSED, updated_at=NOW)
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Changed"),), None
        )

        _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        entries = application.list_inbox_entries(None, limit=10)
        assert {entry.state for entry in entries} == {InboxState.UNREAD, InboxState.DISMISSED}
        assert len(application.list_signals(None, limit=10)) == 2


def test_later_release_reversion_creates_a_fresh_unread_signal(tmp_path: Path) -> None:
    """Catches a release changing back to prior material being deduplicated against stale inbox history."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Original"),), None
        )
        _refresh(application, source, kind="releases", lock_path=tmp_path / "lock", checked_at=NOW)
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Changed"),), None
        )
        _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(days=1),
        )
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Original"),), None
        )

        _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(days=2),
        )

        assert len(application.list_signals(None, limit=10)) == 3
        assert len(application.list_inbox_entries(InboxState.UNREAD, limit=10)) == 3


def test_material_event_change_creates_a_fresh_unread_signal(tmp_path: Path) -> None:
    """Catches a rescheduled or relocated event being hidden when its title is unchanged."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        events = FakeEventClient()
        events.events["attraction:One"] = (_event("event-1", venue_name="Original Venue"),)
        _refresh(application, source, kind="events", lock_path=tmp_path / "lock", events=events)
        events.events["attraction:One"] = (_event("event-1", venue_name="Changed Venue"),)

        _refresh(
            application,
            source,
            kind="events",
            lock_path=tmp_path / "lock",
            events=events,
            checked_at=NOW + timedelta(hours=6),
        )

        assert len(application.list_signals(None, limit=10)) == 2
        assert len(application.list_inbox_entries(InboxState.UNREAD, limit=10)) == 2


def test_event_refresh_creates_an_unread_event_item_and_skips_unconfigured_events(
    tmp_path: Path,
) -> None:
    """Catches optional event setup being treated as a failure or failing to create inbox state."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        events = FakeEventClient()
        events.events["attraction:One"] = (_event("event-1"),)

        first = _refresh(
            application, source, kind="events", lock_path=tmp_path / "lock", events=events
        )
        skipped = _refresh(
            application,
            source,
            kind="events",
            lock_path=tmp_path / "lock",
            events=FakeEventClient(configured=False),
        )

        assert first.run is not None
        assert first.run.status.value == "succeeded"
        assert application.list_inbox_entries(InboxState.UNREAD, limit=10)
        assert skipped.run is not None
        assert skipped.run.status.value == "succeeded"


def test_event_refresh_does_not_require_a_music_source_when_a_watchlist_is_already_local(
    tmp_path: Path,
) -> None:
    """Catches event-only refresh depending on an unrelated music-provider connection."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        events = FakeEventClient()
        events.events["attraction:One"] = (_event("event-1"),)

        result = refresh_once(
            application,
            kind="events",
            source_name="spotify",
            source=None,  # type: ignore[arg-type]
            config=_config(),
            event_client=events,
            checked_at=NOW,
            lock_path=tmp_path / "lock",
        )

        assert result.run is not None
        assert application.list_inbox_entries(InboxState.UNREAD, limit=10)


def test_all_refresh_runs_catalog_before_release_and_event_checks(tmp_path: Path) -> None:
    """Catches all refresh using a stale watchlist or invoking discovery before catalog synchronization."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)
        events = FakeEventClient()
        events.events["attraction:One"] = (_event("event-1"),)

        result = _refresh(
            application, source, kind="all", lock_path=tmp_path / "lock", events=events
        )

        assert result.run is not None
        assert source.calls[:5] == [
            "catalog:followed",
            "catalog:saved",
            "catalog:short_term",
            "catalog:medium_term",
            "catalog:long_term",
        ]
        assert source.calls[5] == "releases"
        assert events.calls[-1] == "events"
        assert len(application.list_inbox_entries(InboxState.UNREAD, limit=10)) == 2


def test_refresh_records_partial_success_and_a_redacted_deterministic_summary(
    tmp_path: Path,
) -> None:
    """Catches raw provider failures in durable summaries or discarding an earlier successful component."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)
        events = FakeEventClient()
        events.events["attraction:One"] = RuntimeError("private-provider-error-canary")

        result = _refresh(
            application, source, kind="all", lock_path=tmp_path / "lock", events=events
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert application.list_inbox_entries(InboxState.UNREAD, limit=10)
        summary = tuple((metric.kind.value, metric.count) for metric in result.run.summary.metrics)
        assert summary == tuple(sorted(summary))
        assert "private-provider-error-canary" not in repr(result.run)


def test_refresh_lock_rejects_an_active_invocation_and_recovers_a_stale_lock(
    tmp_path: Path,
) -> None:
    """Catches overlapping refreshes or a dead process permanently blocking later refreshes."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeMusicSource()
        lock_path = tmp_path / "lock"
        lock_path.write_text("running", encoding="utf-8")

        blocked = _refresh(application, source, kind="catalog", lock_path=lock_path)

        assert blocked.already_running is True
        assert blocked.run is None
        lock_path.write_text(
            '{"created_at":1788263340.0,"token":"stale-lock-token"}', encoding="utf-8"
        )
        recovered = _refresh(application, source, kind="catalog", lock_path=lock_path)

        assert recovered.already_running is False
        assert recovered.run is not None


def test_old_lock_owner_cannot_remove_a_stale_lock_replacement(tmp_path: Path) -> None:
    """Catches a completed old refresh unlinking the replacement lock owned by a later invocation."""
    path = tmp_path / "refresh.lock"

    first = _acquire_lock(path, lock_clock=lambda: 1_000.0)
    replacement = _acquire_lock(path, lock_clock=lambda: 1_601.0)
    _release_lock(first)

    assert first is not None
    assert replacement is not None
    assert path.exists()
    _release_lock(replacement)
    assert not path.exists()


def test_lock_staleness_uses_acquisition_time_not_refresh_timestamp(tmp_path: Path) -> None:
    """Catches stale or future refresh timestamps changing another invocation's lock lifetime."""
    path = tmp_path / "refresh.lock"

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeMusicSource()
        first = _acquire_lock(path, lock_clock=lambda: 1_000.0)
        stale_timestamp = _refresh(
            application,
            source,
            kind="catalog",
            lock_path=path,
            checked_at=NOW - timedelta(days=365),
            lock_clock=lambda: 1_001.0,
        )
        future_timestamp = _refresh(
            application,
            source,
            kind="catalog",
            lock_path=path,
            checked_at=NOW + timedelta(days=365),
            lock_clock=lambda: 1_001.0,
        )
        recovered = _refresh(
            application,
            source,
            kind="catalog",
            lock_path=path,
            checked_at=NOW - timedelta(days=365),
            lock_clock=lambda: 1_601.0,
        )

        assert first is not None
        assert stale_timestamp.already_running is True
        assert future_timestamp.already_running is True
        assert recovered.already_running is False


def test_refresh_deadline_stops_between_components_after_preserving_completed_work(
    tmp_path: Path,
) -> None:
    """Catches a deadline check that starts another provider operation after its budget expires."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)
        values = iter((0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 601.0))

        result = _refresh(
            application,
            source,
            kind="all",
            lock_path=tmp_path / "lock",
            monotonic=lambda: next(values),
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert source.calls == [
            "catalog:followed",
            "catalog:saved",
            "catalog:short_term",
            "catalog:medium_term",
            "catalog:long_term",
        ]
        assert application.list_watchlist(limit=10)[0].artist == artist


def test_refresh_deadline_does_not_egress_again_after_expiring_inside_catalog_sync(
    tmp_path: Path,
) -> None:
    """Catches catalog synchronization continuing provider calls after the shared deadline expires."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        values = iter((0.0, 0.0, 1.0, 601.0, 601.0, 601.0, 601.0, 601.0))

        result = _refresh(
            application,
            source,
            kind="catalog",
            lock_path=tmp_path / "lock",
            monotonic=lambda: next(values),
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert source.calls == ["catalog:followed"]
        assert application.list_watchlist(limit=10)[0].artist == artist


def test_refresh_deadline_prevents_later_ticketmaster_calls_after_expiry(tmp_path: Path) -> None:
    """Catches event refresh querying another artist after the shared deadline has expired."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _watch(application, _artist("one", "One"))
        _watch(application, _artist("two", "Two"))
        source = FakeMusicSource()
        events = FakeEventClient()
        events.events["attraction:One"] = (_event("event-1"),)
        events.events["attraction:Two"] = (_event("event-2"),)
        values = iter((0.0, 0.0, 1.0, 1.0, 1.0, 601.0))

        result = _refresh(
            application,
            source,
            kind="events",
            lock_path=tmp_path / "lock",
            events=events,
            monotonic=lambda: next(values),
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert events.calls == ["configured", "attractions", "events"]


def test_ninth_source_request_waits_for_the_rolling_window(tmp_path: Path) -> None:
    """Catches a ninth provider call escaping before the 30-second rolling window opens."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeMusicSource()
        for number in range(9):
            artist = _artist(f"artist-{number}", f"Artist {number}")
            _watch(application, artist)
            source.releases[(f"artist-{number}", None)] = Page((), None)
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert clock.sleeps == [30.0]
        assert ("source_requests", 9) in {
            (metric.kind.value, metric.count) for metric in result.run.summary.metrics
        }


def test_exact_retry_after_is_paused_and_the_same_request_is_retried(tmp_path: Path) -> None:
    """Catches a valid short Retry-After being replaced or advancing to another artist."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = [RateLimitedError(60), Page((), None)]  # type: ignore[assignment]
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "succeeded"
        assert source.release_calls == ["one", "one"]
        assert clock.sleeps == [60]
        assert {(item.kind.value, item.count) for item in result.run.summary.metrics} >= {
            ("source_requests", 2),
            ("limit_pauses", 1),
        }
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify", SourceLimitState.AVAILABLE, NOW + timedelta(seconds=60), None, False, 0
        )


def test_estimated_retry_uses_exponential_delay_and_stops_above_sixty_seconds(
    tmp_path: Path,
) -> None:
    """Catches malformed Retry-After values bypassing 60/120-second estimated backoff."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = [  # type: ignore[assignment]
            RateLimitedError(None),
            RateLimitedError("malformed"),
        ]
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert clock.sleeps == [60]
        assert source.release_calls == ["one", "one"]
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify",
            SourceLimitState.COOLING_DOWN,
            NOW + timedelta(seconds=60),
            NOW + timedelta(seconds=180),
            False,
            2,
        )
        assert catalog.get_source_cursor(
            "spotify", SourceCapability.RECENT_RELEASES
        ) == SourceCursor(
            "spotify",
            SourceCapability.RECENT_RELEASES,
            artist.local_id,
            NOW + timedelta(seconds=60),
        )


def test_estimated_retry_delay_caps_at_nine_hundred_seconds(tmp_path: Path) -> None:
    """Catches repeated estimated limits producing an unbounded cooldown."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        catalog.put_source_limit(
            SourceLimitObservation(
                "spotify",
                SourceLimitState.COOLING_DOWN,
                NOW - timedelta(seconds=1),
                NOW - timedelta(seconds=1),
                False,
                4,
            )
        )
        source = FakeMusicSource()
        source.releases[("one", None)] = RateLimitedError(None)
        clock = FakeClock()

        _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        limit = catalog.get_source_limit("spotify")
        assert limit is not None
        assert limit.retry_at == NOW + timedelta(seconds=900)
        assert limit.consecutive_limits == 5


def test_two_limit_pause_cap_checkpoints_the_third_short_limit(tmp_path: Path) -> None:
    """Catches an invocation sleeping more than twice after repeated short limits."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = [  # type: ignore[assignment]
            RateLimitedError(1),
            RateLimitedError(1),
            RateLimitedError(1),
        ]
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert clock.sleeps == [1, 1]
        assert source.release_calls == ["one", "one", "one"]
        assert ("limit_pauses", 2) in {
            (metric.kind.value, metric.count) for metric in result.run.summary.metrics
        }


def test_short_limit_does_not_pause_past_the_refresh_deadline(tmp_path: Path) -> None:
    """Catches a Retry-After sleep starting when it cannot finish inside the refresh budget."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        clock = FakeClock()

        def near_deadline() -> Page[Release]:
            clock.value = 550
            raise RateLimitedError(60)

        source.releases[("one", None)] = near_deadline  # type: ignore[assignment]

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert clock.sleeps == []
        assert source.release_calls == ["one"]


def test_unexpired_source_cooldown_refuses_provider_requests(tmp_path: Path) -> None:
    """Catches a persisted cooldown being ignored by a new process."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        catalog.put_source_limit(
            SourceLimitObservation(
                "spotify",
                SourceLimitState.COOLING_DOWN,
                NOW,
                NOW + timedelta(minutes=2),
                False,
                2,
            )
        )
        source = FakeMusicSource()

        result = _refresh(
            application, source, kind="releases", lock_path=tmp_path / "lock", checked_at=NOW
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert source.calls == []


def test_quota_exhaustion_checkpoints_current_artist_without_calling_later_artists(
    tmp_path: Path,
) -> None:
    """Catches quota exhaustion being treated as an ordinary per-artist failure."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artists = tuple(
            _artist(value, name) for value, name in (("one", "A"), ("two", "B"), ("three", "C"))
        )
        for artist in artists:
            _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page((), None)
        source.releases[("two", None)] = QuotaExhaustedError()
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert source.release_calls == ["one", "two"]
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify", SourceLimitState.QUOTA_EXHAUSTED, NOW, None, False, 1
        )
        assert catalog.get_source_cursor(
            "spotify", SourceCapability.RECENT_RELEASES
        ) == SourceCursor("spotify", SourceCapability.RECENT_RELEASES, artists[1].local_id, NOW)


def test_all_refresh_stops_before_events_after_release_quota_exhaustion(tmp_path: Path) -> None:
    """Catches refresh-all starting another provider after release quota exhaustion."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = QuotaExhaustedError()
        events = FakeEventClient()
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="all",
            lock_path=tmp_path / "lock",
            events=events,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert events.calls == []
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify", SourceLimitState.QUOTA_EXHAUSTED, NOW, None, False, 1
        )
        assert catalog.get_source_cursor(
            "spotify", SourceCapability.RECENT_RELEASES
        ) == SourceCursor("spotify", SourceCapability.RECENT_RELEASES, artist.local_id, NOW)


def test_quota_checkpoint_is_resumable_on_a_later_invocation(tmp_path: Path) -> None:
    """Catches quota state without a retry timestamp permanently blocking the saved checkpoint."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        catalog.put_source_limit(
            SourceLimitObservation("spotify", SourceLimitState.QUOTA_EXHAUSTED, NOW, None, False, 1)
        )
        catalog.put_source_cursor(
            SourceCursor("spotify", SourceCapability.RECENT_RELEASES, artist.local_id, NOW)
        )
        source = FakeMusicSource()
        source.releases[("one", None)] = Page((), None)
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(days=1),
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "succeeded"
        assert source.release_calls == ["one"]
        assert catalog.get_source_cursor("spotify", SourceCapability.RECENT_RELEASES) is None


def test_long_delay_checkpoints_and_resume_starts_at_stopped_artist(tmp_path: Path) -> None:
    """Catches long rate limits losing progress or skipping the interrupted artist on resume."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artists = tuple(
            _artist(value, name) for value, name in (("one", "A"), ("two", "B"), ("three", "C"))
        )
        for artist in artists:
            _watch(application, artist)
        limited = FakeMusicSource()
        limited.releases[("one", None)] = Page((), None)
        limited.releases[("two", None)] = RateLimitedError(120)
        first_clock = FakeClock()

        first = _refresh(
            application,
            limited,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW,
            monotonic=first_clock.monotonic,
            sleeper=first_clock.sleep,
        )

        assert first.run is not None
        assert first.run.status.value == "partial"
        assert limited.release_calls == ["one", "two"]
        resumed = FakeMusicSource()
        resumed.releases[("two", None)] = Page((), None)
        resumed.releases[("three", None)] = Page((), None)
        second_clock = FakeClock()
        second = _refresh(
            application,
            resumed,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(seconds=121),
            monotonic=second_clock.monotonic,
            sleeper=second_clock.sleep,
        )

        assert second.run is not None
        assert second.run.status.value == "succeeded"
        assert resumed.release_calls == ["two", "three"]
        assert catalog.get_source_cursor("spotify", SourceCapability.RECENT_RELEASES) is None
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify",
            SourceLimitState.AVAILABLE,
            NOW + timedelta(seconds=121),
            None,
            False,
            0,
        )


def test_all_refresh_stops_before_events_after_long_release_cooldown(tmp_path: Path) -> None:
    """Catches refresh-all starting another provider after a terminal release cooldown."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.followed = Page((artist,), None)
        source.releases[("one", None)] = RateLimitedError(120)
        events = FakeEventClient()
        clock = FakeClock()

        result = _refresh(
            application,
            source,
            kind="all",
            lock_path=tmp_path / "lock",
            events=events,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert events.calls == []
        assert catalog.get_source_limit("spotify") == SourceLimitObservation(
            "spotify",
            SourceLimitState.COOLING_DOWN,
            NOW,
            NOW + timedelta(seconds=120),
            True,
            1,
        )
        assert catalog.get_source_cursor(
            "spotify", SourceCapability.RECENT_RELEASES
        ) == SourceCursor("spotify", SourceCapability.RECENT_RELEASES, artist.local_id, NOW)


def test_resumed_non_limit_failure_preserves_checkpoint_and_limit_state(tmp_path: Path) -> None:
    """Catches an incomplete resumed pass discarding its source-level recovery state."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artists = tuple(_artist(value, name) for value, name in (("two", "B"), ("three", "C")))
        for artist in artists:
            _watch(application, artist)
        saved_limit = SourceLimitObservation(
            "spotify",
            SourceLimitState.COOLING_DOWN,
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
            True,
            1,
        )
        saved_cursor = SourceCursor(
            "spotify",
            SourceCapability.RECENT_RELEASES,
            artists[0].local_id,
            NOW - timedelta(minutes=2),
        )
        catalog.put_source_limit(saved_limit)
        catalog.put_source_cursor(saved_cursor)
        source = FakeMusicSource()
        source.releases[("two", None)] = RuntimeError("source request failed")
        source.releases[("three", None)] = Page((_release("release-3", artists[1]),), None)

        result = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW,
        )

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert source.release_calls == ["two", "three"]
        assert application.get_release("release:release-3") is not None
        assert application.list_inbox_entries(InboxState.UNREAD, limit=10)
        assert (
            catalog.get_source_cursor("spotify", SourceCapability.RECENT_RELEASES) == saved_cursor
        )
        assert catalog.get_source_limit("spotify") == saved_limit


def test_missing_checkpoint_artist_falls_back_to_first_current_watchlist_artist(
    tmp_path: Path,
) -> None:
    """Catches a stale release checkpoint preventing every current artist from being checked."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artists = tuple(_artist(value, value.title()) for value in ("one", "two"))
        for artist in artists:
            _watch(application, artist)
        catalog.put_source_limit(
            SourceLimitObservation(
                "spotify",
                SourceLimitState.COOLING_DOWN,
                NOW - timedelta(minutes=2),
                NOW - timedelta(minutes=1),
                True,
                1,
            )
        )
        catalog.put_source_cursor(
            SourceCursor("spotify", SourceCapability.RECENT_RELEASES, "artist:missing", NOW)
        )
        source = FakeMusicSource()
        source.releases[("one", None)] = Page((), None)
        source.releases[("two", None)] = Page((), None)

        _refresh(application, source, kind="releases", lock_path=tmp_path / "lock", checked_at=NOW)

        assert source.release_calls == ["one", "two"]
        assert catalog.get_source_cursor("spotify", SourceCapability.RECENT_RELEASES) is None


def test_each_completed_refresh_keeps_a_distinct_redacted_history_record(tmp_path: Path) -> None:
    """Catches a repeated invocation overwriting the prior completed refresh record."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeMusicSource()

        first = _refresh(application, source, kind="catalog", lock_path=tmp_path / "lock")
        second = _refresh(application, source, kind="catalog", lock_path=tmp_path / "lock")

        assert first.run is not None
        assert second.run is not None
        assert first.run.local_id != second.run.local_id
        assert len(application.list_refresh_runs(limit=10)) == 2


def test_refresh_persists_a_partial_status_when_one_inbox_candidate_cannot_be_recorded(
    tmp_path: Path,
) -> None:
    """Catches one bad candidate aborting the completed refresh record after discovery succeeded."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        release = _release("release-1", artist)
        source.releases[("one", None)] = Page(
            (
                Release(
                    release.local_id,
                    release.title,
                    release.release_type,
                    release.release_date,
                    release.date_precision,
                    release.artist_refs,
                    release.source_refs + (SourceReference("other", "release-1", None, NOW),),
                    release.observed_at,
                ),
            ),
            None,
        )

        result = _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        assert result.run is not None
        assert result.run.status.value == "partial"
        assert application.list_inbox_entries(None, limit=10) == ()


def test_refresh_repairs_a_signal_left_without_an_inbox_entry_after_a_transient_write_failure(
    tmp_path: Path,
) -> None:
    """Catches a transient inbox write failure permanently orphaning a persisted signal on retry."""

    class FailOnceInboxApplication(MusicFriendApplication):
        def __init__(self, catalog: Catalog) -> None:
            super().__init__(catalog)
            self.fail_next_inbox_write = True

        def put_inbox_entry(self, entry: object) -> None:
            if self.fail_next_inbox_write:
                self.fail_next_inbox_write = False
                raise RuntimeError("transient inbox failure")
            super().put_inbox_entry(entry)  # type: ignore[arg-type]

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = FailOnceInboxApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)

        first = _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        assert first.run is not None
        assert first.run.status.value == "partial"
        assert application.list_inbox_entries(None, limit=10) == ()

        second = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(hours=1),
        )

        assert second.run is not None
        assert application.list_inbox_entries(InboxState.UNREAD, limit=10)


def test_refresh_recovers_a_signal_after_discovery_committed_but_signal_write_failed(
    tmp_path: Path,
) -> None:
    """Catches a committed discovery being unable to re-emit a signal after its write fails."""

    class FailOnceSignalApplication(MusicFriendApplication):
        def __init__(self, catalog: Catalog) -> None:
            super().__init__(catalog)
            self.fail_next_signal_write = True

        def put_signal(self, signal: Signal) -> None:
            if self.fail_next_signal_write:
                self.fail_next_signal_write = False
                raise RuntimeError("transient signal failure")
            super().put_signal(signal)

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = FailOnceSignalApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page((_release("release-1", artist),), None)

        first = _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")

        assert first.run is not None
        assert first.run.status.value == "partial"
        assert application.list_signals(None, limit=10) == ()
        assert application.list_inbox_entries(None, limit=10) == ()

        second = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(hours=1),
        )

        assert second.run is not None
        assert len(application.list_signals(None, limit=10)) == 1
        assert len(application.list_inbox_entries(InboxState.UNREAD, limit=10)) == 1


def test_refresh_recovers_a_changed_release_signal_when_an_earlier_version_exists(
    tmp_path: Path,
) -> None:
    """Catches an updated discovery being skipped because an earlier signal already exists."""

    class FailSelectedSignalApplication(MusicFriendApplication):
        def __init__(self, catalog: Catalog) -> None:
            super().__init__(catalog)
            self.fail_next_signal_write = False

        def put_signal(self, signal: Signal) -> None:
            if self.fail_next_signal_write:
                self.fail_next_signal_write = False
                raise RuntimeError("transient signal failure")
            super().put_signal(signal)

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = FailSelectedSignalApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeMusicSource()
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Original"),), None
        )
        _refresh(application, source, kind="releases", lock_path=tmp_path / "lock")
        application.fail_next_signal_write = True
        source.releases[("one", None)] = Page(
            (_release("release-1", artist, title="Changed"),), None
        )

        failed_update = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(hours=1),
        )
        recovered = _refresh(
            application,
            source,
            kind="releases",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(hours=2),
        )

        assert failed_update.run is not None
        assert failed_update.run.status.value == "partial"
        assert recovered.run is not None
        assert len(application.list_signals(None, limit=10)) == 2
        assert len(application.list_inbox_entries(InboxState.UNREAD, limit=10)) == 2


def test_inbox_repair_progresses_past_more_than_five_hundred_orphaned_signals(
    tmp_path: Path,
) -> None:
    """Catches a newest-first signal scan starving older orphaned inbox entries forever."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        application.put_artist(artist)
        release = _release("release-1", artist)
        application.put_release(release)
        for index in range(501):
            application.put_signal(
                Signal(
                    f"signal:{index}",
                    SignalKind.RELEASE,
                    release.local_id,
                    "spotify",
                    f"release-{index}",
                    f"fingerprint-{index}",
                    f"material-{index}",
                    Explanation(
                        (ExplanationReason(ExplanationReasonKind.NEW_RELEASE, release.title),)
                    ),
                    NOW + timedelta(seconds=index),
                )
            )

        _refresh(application, FakeMusicSource(), kind="catalog", lock_path=tmp_path / "lock")

        remaining = application.list_signals_without_inbox_entries(limit=500)
        assert tuple(signal.local_id for signal in remaining) == ("signal:500",)

        _refresh(
            application,
            FakeMusicSource(),
            kind="catalog",
            lock_path=tmp_path / "lock",
            checked_at=NOW + timedelta(hours=1),
        )

        assert application.list_signals_without_inbox_entries(limit=500) == ()


def test_refresh_rejects_invalid_invocation_contract_before_lock_or_provider_io(
    tmp_path: Path,
) -> None:
    """Catches malformed refresh orchestration inputs before local mutation or provider access."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        valid = {
            "application": application,
            "kind": "catalog",
            "source_name": "spotify",
            "source": FakeMusicSource(),
            "config": _config(),
            "event_client": None,
            "checked_at": NOW,
            "lock_path": tmp_path / "lock",
            "monotonic": lambda: 0.0,
            "lock_clock": lambda: 0.0,
            "sleeper": lambda _seconds: None,
        }
        invalid = (
            ("application", object(), "application"),
            ("kind", "unknown", "refresh kind"),
            ("source_name", "", "source_name"),
            ("source", object(), "source"),
            ("config", object(), "config"),
            ("checked_at", datetime(2026, 1, 1), "timezone-aware"),
            ("lock_path", "lock", "lock_path"),
            ("monotonic", object(), "monotonic"),
            ("lock_clock", object(), "lock_clock"),
            ("sleeper", object(), "sleeper"),
        )
        for field, value, message in invalid:
            arguments = dict(valid)
            arguments[field] = value
            with pytest.raises(ValueError, match=message):
                refresh_once(**arguments)  # type: ignore[arg-type]

        arguments = dict(valid)
        arguments.update(kind="releases", source=None)
        with pytest.raises(ValueError, match="source is required"):
            refresh_once(**arguments)  # type: ignore[arg-type]
        assert not (tmp_path / "lock").exists()


def test_inbox_update_rejects_invalid_application_state_and_missing_entry(tmp_path: Path) -> None:
    """Catches an explicit inbox decision being applied to an invalid or absent local record."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        with pytest.raises(ValueError, match="application"):
            update_inbox_state(object(), "missing", InboxState.SAVED, updated_at=NOW)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="state"):
            update_inbox_state(application, "missing", "saved", updated_at=NOW)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="does not exist"):
            update_inbox_state(application, "missing", InboxState.SAVED, updated_at=NOW)


def test_paced_source_preserves_read_contracts_and_stops_after_deadline() -> None:
    """Catches the refresh pacing wrapper changing source results or egressing past its deadline."""

    class RecordingReadSource(FakeMusicSource):
        def __init__(self) -> None:
            super().__init__()
            self.search_result = Page((_artist("search", "Search Result"),), None)
            self.top_result = Page((_artist("top", "Top Result"),), None)
            self.read_calls: list[tuple[object, ...]] = []

        def search_artists(self, query: str, limit: int) -> Page[Artist]:
            self.read_calls.append(("search", query, limit))
            return self.search_result

        def top_items(self, time_range: str, limit: int) -> object:
            self.read_calls.append(("top", time_range, limit))
            return self.top_result

    source = RecordingReadSource()
    clock = FakeClock()
    paced = _PacedSource(
        source,
        source_name="spotify",
        started_at=clock.monotonic(),
        checked_at=NOW,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        saved_limit=None,
    )
    assert paced.capabilities() == source.capabilities()
    assert paced.health() == source.health()
    assert paced.search_artists("One", 3) is source.search_result
    assert paced.top_items("short_term", 5) is source.top_result
    assert source.read_calls == [("search", "One", 3), ("top", "short_term", 5)]

    clock.value = 600.0
    calls_at_deadline = tuple(source.read_calls)
    with pytest.raises(_SourceCallStopped):
        paced.search_artists("Too Late", 1)
    assert paced.stopped is True
    assert tuple(source.read_calls) == calls_at_deadline

    with pytest.raises(_SourceCallStopped):
        paced.top_items("long_term", 1)
    assert tuple(source.read_calls) == calls_at_deadline


@pytest.mark.parametrize(
    "contents",
    (
        "not-json",
        "[]",
        '{"created_at":"bad","token":"value"}',
        '{"created_at":0,"token":""}',
        '{"created_at":0,"token":"value","extra":1}',
    ),
)
def test_refresh_lock_rejects_malformed_or_ambiguous_existing_state(
    tmp_path: Path, contents: str
) -> None:
    """Catches malformed lock state being treated as a safely replaceable stale owner."""
    path = tmp_path / "lock"
    path.write_text(contents, encoding="utf-8")
    assert _read_lock(path) is None
    assert _acquire_lock(path, lock_clock=lambda: 1_000.0) is None
    assert path.read_text(encoding="utf-8") == contents


def test_refresh_lock_rejects_invalid_clock_value_before_filesystem_mutation(
    tmp_path: Path,
) -> None:
    """Catches a nonnumeric lease timestamp creating an unusable lock file."""
    path = tmp_path / "lock"
    with pytest.raises(ValueError, match="lock_clock"):
        _acquire_lock(path, lock_clock=lambda: "bad")  # type: ignore[arg-type]
    assert not path.exists()
