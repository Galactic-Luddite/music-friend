"""One-shot local refresh and inbox state transitions."""

from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    MAX_SOURCE_WINDOW_CALLS,
    MIN_SOURCE_WINDOW_CALLS,
    Artist,
    CatalogItemBatch,
    CatalogSyncResult,
    Event,
    EventCandidate,
    EventDiscoveryResult,
    EventDiscoveryStatus,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    InboxEntry,
    InboxState,
    RefreshKind,
    RefreshMetric,
    RefreshMetricKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    Release,
    ReleaseCandidate,
    ReleaseDiscoveryResult,
    ReleaseDiscoveryStatus,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
    SyncCapabilityStatus,
)
from music_friend.errors import QuotaExhaustedError, RateLimitedError
from music_friend.providers import MusicSource, Page, ProviderCapabilities, ProviderHealth
from music_friend.providers.musicbrainz.source import MusicBrainzSource
from music_friend.providers.ticketmaster import (
    TicketmasterAttraction,
    TicketmasterClient,
    TicketmasterEvent,
)
from music_friend.tools.application import MusicFriendApplication
from music_friend.tools.identity_mapping import run_identity_mapping
from music_friend.tools.release_discovery import (
    _ReleaseDiscoveryInterrupted,
    _SourceCallStopped,
)

_DEADLINE_SECONDS = 600
_LOCK_STALE_AFTER = timedelta(seconds=_DEADLINE_SECONDS)
_SOURCE_WINDOW_SECONDS = 30.0
_MAX_LIMIT_PAUSES = 2
#: AIMD recovery: this many consecutive successful requests earns back one call
#: per window, up to MAX_SOURCE_WINDOW_CALLS.
_RECOVERY_STREAK = 5
_FALLBACK_LADDER = (60, 120, 240, 480, 900)


@dataclass(frozen=True, slots=True)
class RefreshInvocation:
    """The terminal outcome of one local refresh invocation."""

    run: RefreshRun | None
    already_running: bool
    skip_reason: str | None = None
    #: Set together on a partial outcome: why the run stopped short, an ISO-8601
    #: timestamp of when the source is expected to be ready again, and how many
    #: units of work (artists, pages) remained undone.
    reason: str | None = None
    retry_after: str | None = None
    remaining: int | None = None


@dataclass(slots=True)
class _RefreshCounts:
    pages: int = 0
    records_seen: int = 0
    records_created: int = 0
    records_updated: int = 0
    records_skipped: int = 0
    signals_created: int = 0
    failures: int = 0
    successes: int = 0
    catalog_skipped_fresh: int = 0
    source_requests: int = 0
    limit_pauses: int = 0
    partial: bool = False
    events_skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _LockLease:
    """An ownership token and file identity for one acquired local refresh lock."""

    path: Path
    token: str
    created_at: float
    device: int
    inode: int


class _DeadlineExpired(RuntimeError):
    """Private control flow that prevents another provider operation after the budget expires."""


_T = TypeVar("_T")


class _PacedSource:
    """Pace source egress and stop the invocation at a durable limit boundary."""

    def __init__(
        self,
        source: MusicSource,
        *,
        source_name: str,
        started_at: float,
        checked_at: datetime,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        saved_limit: SourceLimitObservation | None,
        rng: random.Random,
    ) -> None:
        self.source = source
        self.source_name = source_name
        self.started_at = started_at
        self.checked_at = checked_at
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.rng = rng
        self.requests = 0
        self.pauses = 0
        self._request_times: deque[float] = deque()
        self._consecutive_limits = 0 if saved_limit is None else saved_limit.consecutive_limits
        self._estimated_limits = (
            self._consecutive_limits
            if saved_limit is not None and not saved_limit.retry_is_exact
            else 0
        )
        self.window_calls = (
            MAX_SOURCE_WINDOW_CALLS if saved_limit is None else saved_limit.window_calls
        )
        self._success_streak = 0
        self.limit_observation = saved_limit
        self._fresh_limit = False
        self.stopped = bool(
            saved_limit is not None
            and saved_limit.state is SourceLimitState.COOLING_DOWN
            and saved_limit.retry_at is not None
            and saved_limit.retry_at > checked_at
        )

    def capabilities(self) -> ProviderCapabilities:
        return self.source.capabilities()

    def health(self) -> ProviderHealth:
        return self._request(self.source.health)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        return self._request(lambda: self.source.search_artists(query, limit))

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        return self._request(lambda: self.source.followed_artists(cursor))

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        return self._request(lambda: self.source.saved_items(cursor))

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        return self._request(lambda: self.source.top_items(time_range, limit))

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        return self._request(lambda: self.source.top_artists(time_range, limit))

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        return self._request(lambda: self.source.recent_releases(artist_refs, since, cursor))

    def available_observation(self) -> SourceLimitObservation:
        return SourceLimitObservation(
            self.source_name,
            SourceLimitState.AVAILABLE,
            self._wall_now(),
            None,
            False,
            0,
            self.window_calls,
        )

    def current_observation(self) -> SourceLimitObservation:
        """The observation to persist for the next run: reflects the learned pacing rate.

        Only a limit observed during *this* invocation, or an unexpired cooldown carried
        in from a previous run (the constructor kept ``stopped`` true for it), is worth
        keeping; a stale, already-expired saved cooldown is replaced with the current
        learned rate instead of being persisted forever.
        """
        if self.limit_observation is not None and (self._fresh_limit or self.stopped):
            return self.limit_observation
        return self.available_observation()

    def _request(self, operation: Callable[[], _T]) -> _T:
        if self.stopped:
            raise _SourceCallStopped()
        while True:
            self._pace()
            self.requests += 1
            try:
                result = operation()
            except QuotaExhaustedError:
                observed_at = self._wall_now()
                self._consecutive_limits += 1
                self._on_limited()
                self.limit_observation = SourceLimitObservation(
                    self.source_name,
                    SourceLimitState.QUOTA_EXHAUSTED,
                    observed_at,
                    None,
                    False,
                    self._consecutive_limits,
                    self.window_calls,
                )
                self.stopped = True
                raise _SourceCallStopped() from None
            except RateLimitedError as error:
                observed_at = self._wall_now()
                self._consecutive_limits += 1
                self._on_limited()
                if error.retry_after_is_exact:
                    self._estimated_limits = 0
                    delay = float(error.retry_after_seconds)
                else:
                    self._estimated_limits += 1
                    base = _FALLBACK_LADDER[min(self._estimated_limits - 1, 4)]
                    delay = _jittered_delay(base, self.rng)
                self.limit_observation = SourceLimitObservation(
                    self.source_name,
                    SourceLimitState.COOLING_DOWN,
                    observed_at,
                    observed_at + timedelta(seconds=delay),
                    error.retry_after_is_exact,
                    self._consecutive_limits,
                    self.window_calls,
                )
                remaining = _DEADLINE_SECONDS - (self.monotonic() - self.started_at)
                if delay <= 60 and self.pauses < _MAX_LIMIT_PAUSES and delay < remaining:
                    self.pauses += 1
                    self.sleeper(float(delay))
                    continue
                self.stopped = True
                raise _SourceCallStopped() from None
            self._consecutive_limits = 0
            self._estimated_limits = 0
            self._on_success()
            return result

    def _on_limited(self) -> None:
        """AIMD multiplicative decrease: halve the learned per-window call budget."""
        self._success_streak = 0
        self._fresh_limit = True
        self.window_calls = max(MIN_SOURCE_WINDOW_CALLS, self.window_calls // 2)

    def _on_success(self) -> None:
        """AIMD additive increase: grow the budget by one call after a success streak."""
        self._fresh_limit = False
        self._success_streak += 1
        if self._success_streak >= _RECOVERY_STREAK and self.window_calls < MAX_SOURCE_WINDOW_CALLS:
            self._success_streak = 0
            self.window_calls += 1

    def _pace(self) -> None:
        while True:
            now = self.monotonic()
            if now - self.started_at >= _DEADLINE_SECONDS:
                self.stopped = True
                raise _SourceCallStopped()
            while self._request_times and now - self._request_times[0] >= _SOURCE_WINDOW_SECONDS:
                self._request_times.popleft()
            if len(self._request_times) < self.window_calls:
                self._request_times.append(now)
                return
            delay = self._request_times[0] + _SOURCE_WINDOW_SECONDS - now
            if now + delay - self.started_at >= _DEADLINE_SECONDS:
                self.stopped = True
                raise _SourceCallStopped()
            self.sleeper(delay)

    def _wall_now(self) -> datetime:
        elapsed = max(0.0, self.monotonic() - self.started_at)
        return self.checked_at + timedelta(seconds=elapsed)


def _jittered_delay(base_seconds: int, rng: random.Random) -> float:
    """Full-range jitter bounded to [base/2, base], so Spotify never sees a thundering herd."""
    floor = base_seconds / 2
    return floor + rng.random() * (base_seconds - floor)


@dataclass(slots=True)
class _DeadlineEventClient:
    """Check the shared deadline immediately before each event-provider operation."""

    client: TicketmasterClient
    started_at: float
    monotonic: Callable[[], float]

    def is_configured(self) -> bool:
        self._check()
        return self.client.is_configured()

    def resolve_music_attractions(self, artist_name: str) -> tuple[TicketmasterAttraction, ...]:
        self._check()
        return self.client.resolve_music_attractions(artist_name)

    def events_for_attraction(
        self,
        attraction: TicketmasterAttraction,
        config: LocalConfig,
    ) -> tuple[TicketmasterEvent, ...]:
        self._check()
        return self.client.events_for_attraction(attraction, config)

    def _check(self) -> None:
        if self.monotonic() - self.started_at >= _DEADLINE_SECONDS:
            raise _DeadlineExpired()


def refresh_once(
    application: MusicFriendApplication,
    *,
    kind: RefreshKind | str,
    source_name: str,
    source: MusicSource | None,
    config: LocalConfig,
    event_client: TicketmasterClient | None,
    checked_at: datetime,
    lock_path: Path,
    force: bool = False,
    release_source: MusicSource | None = None,
    release_source_name: str | None = None,
    monotonic: Callable[[], float] | object | None = None,
    lock_clock: Callable[[], float] | object | None = None,
    sleeper: Callable[[float], None] | object | None = None,
    now: Callable[[], datetime] | object | None = None,
    rng: random.Random | object | None = None,
) -> RefreshInvocation:
    """Run selected existing checks once, persist safe results, and always release the local lock."""
    if not isinstance(application, MusicFriendApplication):
        raise ValueError("application must be a MusicFriendApplication")
    selected_kind = _refresh_kind(kind)
    if type(source_name) is not str or not source_name:
        raise ValueError("source_name must be text")
    components = _components(selected_kind)
    if source is not None and not isinstance(source, MusicSource):
        raise ValueError("source must implement the music source contract")
    if source is None and any(component in {"catalog", "releases"} for component in components):
        raise ValueError("source is required for catalog and release refresh")
    if release_source is not None and not isinstance(release_source, MusicSource):
        raise ValueError("release_source must implement the music source contract")
    if release_source_name is not None and (
        type(release_source_name) is not str or not release_source_name
    ):
        raise ValueError("release_source_name must be text")
    if "releases" in components:
        if release_source is None:
            release_source = source
        if release_source_name is None:
            release_source_name = source_name
    if type(config) is not LocalConfig:
        raise ValueError("config must be LocalConfig")
    if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    if not isinstance(lock_path, Path):
        raise ValueError("lock_path must be a Path")
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    clock = time.monotonic if monotonic is None else monotonic
    if not callable(clock):
        raise ValueError("monotonic must be callable")
    acquisition_clock = time.time if lock_clock is None else lock_clock
    if not callable(acquisition_clock):
        raise ValueError("lock_clock must be callable")
    sleep = time.sleep if sleeper is None else sleeper
    if not callable(sleep):
        raise ValueError("sleeper must be callable")
    wall_clock = (lambda: checked_at) if now is None else now
    if not callable(wall_clock):
        raise ValueError("now must be callable")
    entropy = random.Random() if rng is None else rng
    if not isinstance(entropy, random.Random):
        raise ValueError("rng must be a random.Random")
    lease = _acquire_lock(lock_path, lock_clock=acquisition_clock)
    if lease is None:
        return RefreshInvocation(None, True)

    try:
        started_monotonic = clock()
        limited_source = (
            None
            if source is None
            else _PacedSource(
                source,
                source_name=source_name,
                started_at=started_monotonic,
                checked_at=checked_at,
                monotonic=clock,
                sleeper=sleep,
                saved_limit=application.get_source_limit(source_name),
                rng=entropy,
            )
        )
        shares_paced_source = (
            limited_source is not None
            and release_source is source
            and release_source_name == source_name
        )
        limited_release_source: _PacedSource | None
        if shares_paced_source:
            limited_release_source = limited_source
        elif release_source is None:
            limited_release_source = None
        else:
            assert release_source_name is not None  # set together with release_source above
            limited_release_source = _PacedSource(
                release_source,
                source_name=release_source_name,
                started_at=started_monotonic,
                checked_at=checked_at,
                monotonic=clock,
                sleeper=sleep,
                saved_limit=application.get_source_limit(release_source_name),
                rng=entropy,
            )
        limited_event_client = (
            None
            if event_client is None
            else _DeadlineEventClient(event_client, started_monotonic, clock)
        )
        counts = _RefreshCounts()
        _repair_missing_signals(application, counts)
        _repair_inbox_entries(application, checked_at, counts)
        run_id = _run_id()
        deadline_exceeded = False
        for component in components:
            if clock() - started_monotonic >= _DEADLINE_SECONDS:
                counts.failures += 1
                deadline_exceeded = True
                break
            if component == "catalog":
                if limited_source is None:
                    raise AssertionError("catalog refresh requires a source")
                _run_catalog(application, source_name, limited_source, checked_at, counts, force)
            elif component == "releases":
                if limited_release_source is None or release_source_name is None:
                    raise AssertionError("release refresh requires a release_source")
                _run_releases(
                    application, release_source_name, limited_release_source, checked_at, counts
                )
                if limited_release_source.stopped:
                    break
            else:
                _run_events(application, config, limited_event_client, checked_at, counts)
        if limited_source is not None and (
            "catalog" in components or limited_source is limited_release_source
        ):
            counts.source_requests = limited_source.requests
            counts.limit_pauses = limited_source.pauses
            # Persist the learned pacing rate (and any cooldown) so the next invocation
            # starts from where this one left off, whether it hit a limit or recovered.
            # Gated on the catalog component actually running (or the release source
            # sharing this same paced wrapper): a musicbrainz-only release refresh must
            # never touch Spotify's source_limits row just because a `source` argument
            # was supplied for it, since that argument may go entirely unused.
            application.put_source_limit(limited_source.current_observation())
        if limited_release_source is not None and limited_release_source is not limited_source:
            application.put_source_limit(limited_release_source.current_observation())
        if (
            selected_kind is RefreshKind.EVENTS
            and counts.events_skip_reason is not None
            and counts.successes == 0
            and counts.failures == 0
            and counts.signals_created == 0
        ):
            return RefreshInvocation(None, False, skip_reason=counts.events_skip_reason)
        status = _status(counts)
        finished_at = max(checked_at, _wall_clock_now(wall_clock))
        run = RefreshRun(
            run_id,
            source_name,
            selected_kind,
            status,
            checked_at,
            finished_at,
            _summary(counts),
        )
        application.put_refresh_run(run)
        reason, retry_after, remaining = _partial_details(
            status, limited_source, deadline_exceeded, counts
        )
        return RefreshInvocation(
            run,
            False,
            skip_reason=counts.events_skip_reason,
            reason=reason,
            retry_after=retry_after,
            remaining=remaining,
        )
    finally:
        _release_lock(lease)


def _partial_details(
    status: RefreshStatus,
    limited_source: _PacedSource | None,
    deadline_exceeded: bool,
    counts: _RefreshCounts,
) -> tuple[str | None, str | None, int | None]:
    """The reason/retry_after/remaining triple surfaced on a partial refresh result."""
    if status is not RefreshStatus.PARTIAL:
        return None, None, None
    observation = None if limited_source is None else limited_source.limit_observation
    if observation is not None and observation.state is SourceLimitState.QUOTA_EXHAUSTED:
        return "quota_exhausted", None, counts.records_skipped
    if observation is not None and observation.state is SourceLimitState.COOLING_DOWN:
        retry_after = None if observation.retry_at is None else observation.retry_at.isoformat()
        return "rate_limited", retry_after, counts.records_skipped
    if deadline_exceeded:
        return "deadline", None, counts.records_skipped
    return None, None, counts.records_skipped


def _wall_clock_now(wall_clock: Callable[[], datetime]) -> datetime:
    value = wall_clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must return a timezone-aware datetime")
    return value


def update_inbox_state(
    application: MusicFriendApplication,
    inbox_local_id: str,
    state: InboxState,
    *,
    updated_at: datetime,
) -> InboxEntry:
    """Apply one explicit local inbox decision while keeping its original creation time."""
    if not isinstance(application, MusicFriendApplication):
        raise ValueError("application must be a MusicFriendApplication")
    if not isinstance(state, InboxState):
        raise ValueError("state must be an InboxState")
    entry = application.get_inbox_entry(inbox_local_id)
    if entry is None:
        raise ValueError("inbox entry does not exist")
    updated = InboxEntry(entry.local_id, entry.signal_local_id, state, entry.created_at, updated_at)
    application.put_inbox_entry(updated)
    return updated


def _refresh_kind(value: RefreshKind | str) -> RefreshKind:
    if isinstance(value, RefreshKind):
        return value
    if type(value) is str:
        try:
            return RefreshKind(value)
        except ValueError:
            pass
    raise ValueError("kind must be a refresh kind")


def _components(kind: RefreshKind) -> tuple[str, ...]:
    if kind is RefreshKind.CATALOG:
        return ("catalog",)
    if kind is RefreshKind.RELEASES:
        return ("releases",)
    if kind is RefreshKind.EVENTS:
        return ("events",)
    return ("catalog", "releases", "events")


def _run_catalog(
    application: MusicFriendApplication,
    source_name: str,
    source: _PacedSource,
    checked_at: datetime,
    counts: _RefreshCounts,
    force: bool = False,
) -> None:
    try:
        result = application.synchronize_catalog(
            source_name, source, checked_at=checked_at, force=force
        )
    except Exception:
        counts.failures += 1
        return
    _count_catalog(result, counts)
    if source.stopped and source.limit_observation is not None:
        application.put_source_limit(source.limit_observation)
        counts.partial = True


def _count_catalog(result: CatalogSyncResult, counts: _RefreshCounts) -> None:
    for capability in result.capabilities:
        counts.pages += capability.pages_seen
        counts.records_seen += capability.artists_seen
        if capability.status is SyncCapabilityStatus.SUCCESS:
            counts.successes += 1
        elif capability.status is SyncCapabilityStatus.SKIPPED_FRESH:
            counts.catalog_skipped_fresh += 1
        else:
            counts.failures += 1


def _run_releases(
    application: MusicFriendApplication,
    source_name: str,
    source: _PacedSource,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    # Run identity mapping before release discovery if using MusicBrainz. Per-artist
    # network failures are handled inside run_identity_mapping itself (that artist is
    # recorded unmapped and the pass continues); only RateLimitedError propagates here,
    # so pacing/cooldown accounting for this source stays correct.
    if source_name == "musicbrainz" and isinstance(source.source, MusicBrainzSource):
        try:
            run_identity_mapping(
                application._catalog,
                source.source,
                source_name,
                checked_at,
            )
        except RateLimitedError:
            counts.failures += 1
            if source.limit_observation is not None:
                application.put_source_limit(source.available_observation())
            return

    checkpoint = application.get_source_cursor(source_name, SourceCapability.RECENT_RELEASES)
    try:
        result = application.discover_releases(
            source_name,
            source,
            checked_at=checked_at,
            start_artist_local_id=None if checkpoint is None else checkpoint.cursor,
        )
    except _ReleaseDiscoveryInterrupted as interrupted:
        _count_releases(
            application,
            ReleaseDiscoveryResult(interrupted.completed),
            checked_at,
            counts,
        )
        observed_at = (
            checked_at if source.limit_observation is None else source.limit_observation.observed_at
        )
        application.put_source_cursor(
            SourceCursor(
                source_name,
                SourceCapability.RECENT_RELEASES,
                interrupted.artist_local_id,
                observed_at,
            )
        )
        if source.limit_observation is not None:
            application.put_source_limit(source.limit_observation)
        counts.partial = True
        return
    except Exception:
        counts.failures += 1
        return
    _count_releases(application, result, checked_at, counts)
    if any(artist.status is ReleaseDiscoveryStatus.FAILED for artist in result.artists):
        return
    application.remove_source_cursor(source_name, SourceCapability.RECENT_RELEASES)
    application.put_source_limit(source.available_observation())


def _count_releases(
    application: MusicFriendApplication,
    result: ReleaseDiscoveryResult,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    for artist_result in result.artists:
        counts.records_seen += artist_result.records_seen
        if artist_result.status is ReleaseDiscoveryStatus.FAILED:
            counts.failures += 1
            continue
        counts.successes += 1
        if artist_result.status is ReleaseDiscoveryStatus.PARTIAL:
            counts.records_skipped += 1
        _record_release_candidates(application, artist_result.candidates, checked_at, counts)


def _run_events(
    application: MusicFriendApplication,
    config: LocalConfig,
    event_client: TicketmasterClient | None,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    if event_client is None:
        counts.records_skipped += 1
        counts.successes += 1
        return
    try:
        result = application.discover_ticketmaster_events(
            config=config,
            client=event_client,
            checked_at=checked_at,
        )
    except Exception:
        counts.failures += 1
        return
    if result.status is EventDiscoveryStatus.SKIPPED and not _event_area_configured(config):
        counts.events_skip_reason = "event_area_not_configured"
        counts.records_skipped += 1
        return
    _count_events(application, result, checked_at, counts)


def _event_area_configured(config: LocalConfig) -> bool:
    return config.event_country_code is not None and config.event_postal_code is not None


def _count_events(
    application: MusicFriendApplication,
    result: EventDiscoveryResult,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    if result.status is EventDiscoveryStatus.SKIPPED:
        counts.records_skipped += 1
        counts.successes += 1
        return
    for artist_result in result.artists:
        counts.records_seen += artist_result.records_seen
        if artist_result.status is EventDiscoveryStatus.FAILED:
            counts.failures += 1
            continue
        counts.successes += 1
        if artist_result.status in {EventDiscoveryStatus.CACHED, EventDiscoveryStatus.UNMATCHED}:
            counts.records_skipped += 1
        _record_event_candidates(application, artist_result.candidates, checked_at, counts)


def _record_release_candidates(
    application: MusicFriendApplication,
    candidates: tuple[ReleaseCandidate, ...],
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    for candidate in candidates:
        if candidate.kind.value == "new":
            counts.records_created += 1
            reason = ExplanationReasonKind.NEW_RELEASE
        else:
            counts.records_updated += 1
            reason = ExplanationReasonKind.UPDATED_RELEASE
        try:
            _record_candidate(
                application,
                kind=SignalKind.RELEASE,
                record_local_id=candidate.release.local_id,
                artist_local_id=candidate.artist_local_id,
                provider_reference=candidate.release.source_refs,
                reason=reason,
                detail=candidate.release.title,
                material=_release_material(candidate.release),
                checked_at=checked_at,
                counts=counts,
            )
        except Exception:
            counts.failures += 1


def _record_event_candidates(
    application: MusicFriendApplication,
    candidates: tuple[EventCandidate, ...],
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    for candidate in candidates:
        if candidate.kind.value == "new":
            counts.records_created += 1
        else:
            counts.records_updated += 1
        try:
            _record_candidate(
                application,
                kind=SignalKind.EVENT,
                record_local_id=candidate.event.local_id,
                artist_local_id=candidate.artist_local_id,
                provider_reference=candidate.event.source_refs,
                reason=ExplanationReasonKind.UPCOMING_EVENT,
                detail=candidate.event.title,
                material=_event_material(candidate.event),
                checked_at=checked_at,
                counts=counts,
            )
        except Exception:
            counts.failures += 1


def _record_candidate(
    application: MusicFriendApplication,
    *,
    kind: SignalKind,
    record_local_id: str,
    artist_local_id: str,
    provider_reference: tuple[SourceReference, ...],
    reason: ExplanationReasonKind,
    detail: str,
    material: object,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    reference = _single_reference(provider_reference)
    artist = application.get_artist(artist_local_id)
    if artist is None:
        raise ValueError("candidate artist does not exist")
    explanation = Explanation(
        (
            ExplanationReason(ExplanationReasonKind.MONITORED_ARTIST, artist.display_name),
            ExplanationReason(reason, detail),
        )
    )
    material_version = _material_version(kind, material, explanation, checked_at)
    existing = application.find_signal(
        reference.source,
        kind,
        reference.native_id,
        material_version,
    )
    if existing is not None:
        return
    signal = Signal(
        _signal_id(reference.source, kind, reference.native_id, material_version),
        kind,
        record_local_id,
        reference.source,
        reference.native_id,
        _fingerprint(reference.source, kind, reference.native_id),
        material_version,
        explanation,
        checked_at,
    )
    application.put_signal(signal)
    application.put_inbox_entry(
        InboxEntry(
            _inbox_id(signal.local_id), signal.local_id, InboxState.UNREAD, checked_at, checked_at
        )
    )
    counts.signals_created += 1


def _repair_inbox_entries(
    application: MusicFriendApplication,
    checked_at: datetime,
    counts: _RefreshCounts,
) -> None:
    """Restore unread inbox entries from the bounded set that is actually missing them."""
    for signal in application.list_signals_without_inbox_entries(limit=500):
        inbox_id = _inbox_id(signal.local_id)
        try:
            application.put_inbox_entry(
                InboxEntry(inbox_id, signal.local_id, InboxState.UNREAD, checked_at, checked_at)
            )
        except Exception:
            counts.failures += 1


def _repair_missing_signals(application: MusicFriendApplication, counts: _RefreshCounts) -> None:
    """Reconstruct bounded initial signals after discovery committed before their signal write."""
    for release_discovery in application.list_release_discoveries_without_current_signal(limit=500):
        release = application.get_release(release_discovery.release_local_id)
        if release is None or not release.artist_refs:
            counts.failures += 1
            continue
        try:
            _record_candidate(
                application,
                kind=SignalKind.RELEASE,
                record_local_id=release.local_id,
                artist_local_id=release.artist_refs[0],
                provider_reference=_source_references(
                    release.source_refs, release_discovery.source
                ),
                reason=(
                    ExplanationReasonKind.NEW_RELEASE
                    if release_discovery.first_seen_at == release_discovery.last_seen_at
                    else ExplanationReasonKind.UPDATED_RELEASE
                ),
                detail=release.title,
                material=_release_material(release),
                checked_at=release_discovery.last_seen_at,
                counts=counts,
            )
        except Exception:
            counts.failures += 1
    for event_discovery in application.list_event_discoveries_without_current_signal(limit=500):
        event = application.get_event(event_discovery.event_local_id)
        if event is None:
            counts.failures += 1
            continue
        try:
            _record_candidate(
                application,
                kind=SignalKind.EVENT,
                record_local_id=event.local_id,
                artist_local_id=event_discovery.artist_local_id,
                provider_reference=_source_references(event.source_refs, event_discovery.source),
                reason=ExplanationReasonKind.UPCOMING_EVENT,
                detail=event.title,
                material=_event_material(event),
                checked_at=event_discovery.last_seen_at,
                counts=counts,
            )
        except Exception:
            counts.failures += 1


def _single_reference(references: tuple[SourceReference, ...]) -> SourceReference:
    if len(references) != 1:
        raise ValueError("candidate has invalid provider provenance")
    reference = references[0]
    return reference


def _source_references(
    references: tuple[SourceReference, ...], source: str
) -> tuple[SourceReference, ...]:
    return tuple(reference for reference in references if reference.source == source)


def _material_version(
    kind: SignalKind,
    material: object,
    explanation: Explanation,
    observed_at: datetime,
) -> str:
    value = {
        "kind": kind.value,
        "material": material,
        "observed_at": observed_at.isoformat(),
        "reasons": tuple((reason.kind.value, reason.detail) for reason in explanation.reasons),
        "version": 1,
    }
    return f"material:{_digest(value)}"


def _release_material(release: Release) -> dict[str, object]:
    return {
        "artist_refs": release.artist_refs,
        "date_precision": release.date_precision.value,
        "release_date": release.release_date.isoformat(),
        "release_type": release.release_type,
        "source_refs": tuple(
            (reference.source, reference.native_id, reference.canonical_url)
            for reference in release.source_refs
        ),
        "title": release.title,
    }


def _event_material(event: Event) -> dict[str, object]:
    return {
        "artist_refs": event.artist_refs,
        "locality": event.locality,
        "source_links": event.source_links,
        "source_refs": tuple(
            (reference.source, reference.native_id, reference.canonical_url)
            for reference in event.source_refs
        ),
        "starts_at": None if event.starts_at is None else event.starts_at.isoformat(),
        "time_precision": event.time_precision,
        "title": event.title,
        "venue_name": event.venue_name,
    }


def _fingerprint(provider: str, kind: SignalKind, provider_native_id: str) -> str:
    return f"signal:{_digest((provider, kind.value, provider_native_id))}"


def _signal_id(
    provider: str, kind: SignalKind, provider_native_id: str, material_version: str
) -> str:
    return f"mf:{_digest((provider, kind.value, provider_native_id, material_version))}"


def _inbox_id(signal_local_id: str) -> str:
    return f"inbox:{_digest(signal_local_id)}"


def _run_id() -> str:
    return f"refresh:{uuid4()}"


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _status(counts: _RefreshCounts) -> RefreshStatus:
    if counts.partial:
        return RefreshStatus.PARTIAL
    if counts.failures == 0:
        return RefreshStatus.SUCCEEDED
    if counts.successes == 0:
        return RefreshStatus.FAILED
    return RefreshStatus.PARTIAL


def _summary(counts: _RefreshCounts) -> RefreshSummary:
    values = {
        RefreshMetricKind.PAGES: counts.pages,
        RefreshMetricKind.RECORDS_SEEN: counts.records_seen,
        RefreshMetricKind.RECORDS_CREATED: counts.records_created,
        RefreshMetricKind.RECORDS_UPDATED: counts.records_updated,
        RefreshMetricKind.RECORDS_SKIPPED: counts.records_skipped,
        RefreshMetricKind.SIGNALS_CREATED: counts.signals_created,
        RefreshMetricKind.FAILURES: counts.failures,
        RefreshMetricKind.CATALOG_SKIPPED_FRESH: counts.catalog_skipped_fresh,
        RefreshMetricKind.LIMIT_PAUSES: counts.limit_pauses,
        RefreshMetricKind.SOURCE_REQUESTS: counts.source_requests,
    }
    return RefreshSummary(
        tuple(
            RefreshMetric(kind, values[kind])
            for kind in sorted(values, key=lambda item: item.value)
            if values[kind]
            or kind in {RefreshMetricKind.LIMIT_PAUSES, RefreshMetricKind.SOURCE_REQUESTS}
        )
    )


def _acquire_lock(
    path: Path,
    *,
    lock_clock: Callable[[], float] = time.time,
) -> _LockLease | None:
    acquired_at = lock_clock()
    if type(acquired_at) not in {int, float}:
        raise ValueError("lock_clock must return seconds")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            existing = _read_lock(path)
            if (
                existing is None
                or acquired_at - existing.created_at < _LOCK_STALE_AFTER.total_seconds()
            ):
                return None
            _release_lock(existing)
            if path.exists():
                return None
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        token = uuid4().hex
        created_at = float(acquired_at)
        encoded = json.dumps(
            {"created_at": created_at, "token": token},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return _LockLease(path, token, created_at, metadata.st_dev, metadata.st_ino)
    except OSError:
        return None


def _release_lock(lease: _LockLease) -> None:
    try:
        if not isinstance(lease, _LockLease) or lease.path.is_symlink():
            return
        metadata = lease.path.stat()
        if (metadata.st_dev, metadata.st_ino) != (lease.device, lease.inode):
            return
        current = _read_lock(lease.path)
        if current is None or current.token != lease.token:
            return
        lease.path.unlink()
    except OSError:
        pass


def _read_lock(path: Path) -> _LockLease | None:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_size > 512:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            type(value) is not dict
            or set(value) != {"created_at", "token"}
            or type(value["created_at"]) not in {int, float}
            or type(value["token"]) is not str
            or not value["token"]
            or len(value["token"]) > 128
        ):
            return None
        return _LockLease(
            path,
            value["token"],
            float(value["created_at"]),
            metadata.st_dev,
            metadata.st_ino,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


__all__ = ["RefreshInvocation", "refresh_once", "update_inbox_state"]
