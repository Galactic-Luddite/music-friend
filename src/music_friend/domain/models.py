"""Immutable, provider-neutral records used by Music Friend."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from music_friend.domain.text import sanitize_source_text

RecordId = str


class IdentityConfidence(str, Enum):
    SOURCE_ONLY = "source_only"
    EXTERNAL_ID = "external_id"
    USER_CONFIRMED = "user_confirmed"


class InterestKind(str, Enum):
    ARTIST = "artist"
    RELEASE = "release"
    EVENT = "event"


class InterestStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class ReleaseDatePrecision(str, Enum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"


class AffinityEvidenceKind(str, Enum):
    FOLLOWED = "followed"
    SAVED_TRACK = "saved_track"
    TOP_SHORT_TERM = "top_short_term"
    TOP_MEDIUM_TERM = "top_medium_term"
    TOP_LONG_TERM = "top_long_term"


class WatchlistAction(str, Enum):
    ADD = "add"
    PIN = "pin"
    MUTE = "mute"


class WatchlistInclusionReason(str, Enum):
    PINNED = "pinned"
    MANUALLY_ADDED = "manually_added"
    AUTOMATIC = "automatic"


class LocalPreferenceKey(str, Enum):
    EVENT_COUNTRY_CODE = "event_country_code"
    EVENT_POSTAL_CODE = "event_postal_code"
    EVENT_RADIUS = "event_radius"
    EVENT_RADIUS_UNIT = "event_radius_unit"


class RefreshKind(str, Enum):
    CATALOG = "catalog"
    RELEASES = "releases"
    EVENTS = "events"
    ALL = "all"


class RefreshStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class RefreshMetricKind(str, Enum):
    PAGES = "pages"
    RECORDS_SEEN = "records_seen"
    RECORDS_CREATED = "records_created"
    RECORDS_UPDATED = "records_updated"
    RECORDS_SKIPPED = "records_skipped"
    SIGNALS_CREATED = "signals_created"
    FAILURES = "failures"
    CATALOG_SKIPPED_FRESH = "catalog_skipped_fresh"
    LIMIT_PAUSES = "limit_pauses"
    SOURCE_REQUESTS = "source_requests"


class SyncCapabilityStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_FRESH = "skipped_fresh"


class SourceCapability(str, Enum):
    FOLLOWED_ARTISTS = "followed_artists"
    SAVED_ITEMS = "saved_items"
    TOP_ARTISTS_SHORT_TERM = "top_artists_short_term"
    TOP_ARTISTS_MEDIUM_TERM = "top_artists_medium_term"
    TOP_ARTISTS_LONG_TERM = "top_artists_long_term"
    RECENT_RELEASES = "recent_releases"
    EVENTS = "events"


class SourceLimitState(str, Enum):
    AVAILABLE = "available"
    COOLING_DOWN = "cooling_down"
    QUOTA_EXHAUSTED = "quota_exhausted"


class SignalKind(str, Enum):
    RELEASE = "release"
    EVENT = "event"


class ReleaseCandidateKind(str, Enum):
    NEW = "new"
    UPDATED = "updated"


class ReleaseDiscoveryStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class EventCandidateKind(str, Enum):
    NEW = "new"
    UPDATED = "updated"


class EventDiscoveryStatus(str, Enum):
    SKIPPED = "skipped"
    CACHED = "cached"
    UNMATCHED = "unmatched"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class InboxState(str, Enum):
    UNREAD = "unread"
    SAVED = "saved"
    DISMISSED = "dismissed"


class ExplanationReasonKind(str, Enum):
    FOLLOWED_ARTIST = "followed_artist"
    SAVED_TRACKS = "saved_tracks"
    TOP_SHORT_TERM = "top_short_term"
    TOP_MEDIUM_TERM = "top_medium_term"
    TOP_LONG_TERM = "top_long_term"
    MONITORED_ARTIST = "monitored_artist"
    NEW_RELEASE = "new_release"
    UPDATED_RELEASE = "updated_release"
    UPCOMING_EVENT = "upcoming_event"
    EXACT_ARTIST_MATCH = "exact_artist_match"


_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_COUNTRY_CODE = re.compile(r"[A-Z]{2}\Z")
_POSTAL_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{0,15}\Z")
_OPAQUE_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
_TOP_EVIDENCE_KINDS = frozenset(
    {
        AffinityEvidenceKind.TOP_SHORT_TERM,
        AffinityEvidenceKind.TOP_MEDIUM_TERM,
        AffinityEvidenceKind.TOP_LONG_TERM,
    }
)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > 4096:
        raise ValueError(f"{name} must be at most 4096 characters")
    return value


def _require_optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, name)


def _require_aware_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _require_https_url(value: Any, name: str) -> str:
    url = _require_text(value, name)
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise ValueError(f"{name} must be a safe HTTPS URL") from error
    if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc or parsed.fragment:
        raise ValueError(f"{name} must be a safe HTTPS URL")
    return url


def _require_record_id(value: Any, name: str) -> RecordId:
    return _require_text(value, name)


def _require_record_ids(value: Any, name: str, *, nonempty: bool = False) -> tuple[RecordId, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    if nonempty and not value:
        raise ValueError(f"{name} must not be empty")
    for record_id in value:
        _require_record_id(record_id, name)
    return value


def _require_source_refs(value: Any, *, nonempty: bool = False) -> tuple[SourceReference, ...]:
    if not isinstance(value, tuple):
        raise ValueError("source_refs must be a tuple")
    if nonempty and not value:
        raise ValueError("source_refs must be a non-empty tuple")
    source_id_pairs: set[tuple[str, str]] = set()
    for reference in value:
        if not isinstance(reference, SourceReference):
            raise ValueError("source_refs must contain source references")
        pair = (reference.source, reference.native_id)
        if pair in source_id_pairs:
            raise ValueError("source_refs must not contain duplicate source identities")
        source_id_pairs.add(pair)
    return value


def _require_source_links(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("source_links must be a tuple")
    for link in value:
        _require_https_url(link, "source_links")
    return value


def _require_release_date(value: Any) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError("release_date must be a date")
    return value


def _require_enum(value: Any, enum_type: type[Enum], name: str) -> Enum:
    if not isinstance(value, enum_type):
        raise ValueError(f"{name} must be a {enum_type.__name__}")
    return value


def _require_nonnegative_int(value: Any, name: str, *, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a bounded non-negative integer")
    return value


def _require_safe_token(value: Any, name: str) -> str:
    token = _require_text(value, name)
    if _SAFE_TOKEN.fullmatch(token) is None:
        raise ValueError(f"{name} must be a bounded safe token")
    return token


def _require_inert_text(value: Any, name: str, *, limit: int = 256) -> str:
    text = _require_text(value, name)
    if len(text) > limit or sanitize_source_text(text, limit=limit) != text:
        raise ValueError(f"{name} must be bounded safe inert text")
    return text


class _CanonicalRecord:
    __slots__ = ()

    local_id: RecordId

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.local_id == other.local_id

    def __hash__(self) -> int:
        return hash((type(self), self.local_id))


@dataclass(frozen=True, slots=True)
class SourceReference:
    source: str
    native_id: str
    canonical_url: str | None
    observed_at: datetime
    confidence: IdentityConfidence = IdentityConfidence.SOURCE_ONLY

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_text(self.native_id, "native_id")
        if self.canonical_url is not None:
            _require_https_url(self.canonical_url, "canonical_url")
        _require_aware_datetime(self.observed_at, "observed_at")
        _require_enum(self.confidence, IdentityConfidence, "confidence")


@dataclass(frozen=True, slots=True, eq=False)
class Artist(_CanonicalRecord):
    local_id: RecordId
    display_name: str
    source_refs: tuple[SourceReference, ...]
    identity_confidence: IdentityConfidence
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_text(self.display_name, "display_name")
        _require_source_refs(self.source_refs)
        _require_enum(self.identity_confidence, IdentityConfidence, "identity_confidence")
        _require_aware_datetime(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True, eq=False)
class Release(_CanonicalRecord):
    local_id: RecordId
    title: str
    release_type: str
    release_date: date
    date_precision: ReleaseDatePrecision
    artist_refs: tuple[RecordId, ...]
    source_refs: tuple[SourceReference, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_text(self.title, "title")
        _require_text(self.release_type, "release_type")
        _require_release_date(self.release_date)
        _require_enum(self.date_precision, ReleaseDatePrecision, "date_precision")
        _require_record_ids(self.artist_refs, "artist_refs", nonempty=True)
        _require_source_refs(self.source_refs)
        _require_aware_datetime(self.observed_at, "observed_at")
        if self.date_precision is ReleaseDatePrecision.YEAR and (
            self.release_date.month != 1 or self.release_date.day != 1
        ):
            raise ValueError("year precision requires January 1")
        if self.date_precision is ReleaseDatePrecision.MONTH and self.release_date.day != 1:
            raise ValueError("month precision requires the first day of the month")


@dataclass(frozen=True, slots=True, eq=False)
class Event(_CanonicalRecord):
    local_id: RecordId
    title: str
    artist_refs: tuple[RecordId, ...]
    venue_name: str | None
    locality: str | None
    starts_at: datetime | None
    time_precision: str | None
    source_links: tuple[str, ...]
    source_refs: tuple[SourceReference, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_text(self.title, "title")
        _require_record_ids(self.artist_refs, "artist_refs")
        _require_optional_text(self.venue_name, "venue_name")
        _require_optional_text(self.locality, "locality")
        _require_source_links(self.source_links)
        _require_source_refs(self.source_refs)
        _require_aware_datetime(self.observed_at, "observed_at")
        if self.starts_at is None:
            if self.time_precision is not None:
                raise ValueError("time_precision requires starts_at")
            return
        _require_aware_datetime(self.starts_at, "starts_at")
        precision = _require_text(self.time_precision, "time_precision")
        if precision not in ("date", "hour", "minute", "second"):
            raise ValueError("time_precision must describe the supplied start time")


@dataclass(frozen=True, slots=True, eq=False)
class Interest(_CanonicalRecord):
    local_id: RecordId
    kind: InterestKind
    target_local_id: RecordId
    status: InterestStatus
    created_by: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_enum(self.kind, InterestKind, "kind")
        _require_record_id(self.target_local_id, "target_local_id")
        _require_enum(self.status, InterestStatus, "status")
        _require_text(self.created_by, "created_by")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, eq=False)
class Observation(_CanonicalRecord):
    local_id: RecordId
    source: str
    native_id: str
    record_kind: str
    record_local_id: RecordId
    fact_name: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_text(self.source, "source")
        _require_text(self.native_id, "native_id")
        _require_text(self.record_kind, "record_kind")
        _require_record_id(self.record_local_id, "record_local_id")
        _require_text(self.fact_name, "fact_name")
        if len(self.fact_name) > 128:
            raise ValueError("fact_name must be at most 128 characters")
        _require_aware_datetime(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True, eq=False)
class CatalogItem(_CanonicalRecord):
    kind: str
    local_id: RecordId
    title: str
    artist_refs: tuple[RecordId, ...]
    source_refs: tuple[SourceReference, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.kind, "kind")
        _require_record_id(self.local_id, "local_id")
        _require_text(self.title, "title")
        _require_record_ids(self.artist_refs, "artist_refs")
        _require_source_refs(self.source_refs, nonempty=True)
        _require_aware_datetime(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class CatalogItemBatch:
    items: tuple[CatalogItem, ...]
    artists: tuple[Artist, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, CatalogItem) for item in self.items
        ):
            raise ValueError("items must be a tuple of catalog items")
        if not isinstance(self.artists, tuple) or not all(
            isinstance(artist, Artist) for artist in self.artists
        ):
            raise ValueError("artists must be a tuple of artists")
        if len(self.items) > 500 or len(self.artists) > 500:
            raise ValueError("catalog item batches must contain at most 500 entries")
        artist_ids = tuple(artist.local_id for artist in self.artists)
        if len(set(artist_ids)) != len(artist_ids):
            raise ValueError("artists must not contain duplicate artist identities")
        credited_ids = {artist_id for item in self.items for artist_id in item.artist_refs}
        if credited_ids != set(artist_ids):
            raise ValueError("batch must contain every credited artist exactly once")
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, str):
                raise ValueError("next_cursor must be a string or None")
            if len(self.next_cursor) > 2048:
                raise ValueError("next_cursor must be at most 2048 characters")


@dataclass(frozen=True, slots=True, eq=False)
class AffinityEvidence(_CanonicalRecord):
    local_id: RecordId
    artist_local_id: RecordId
    source: str
    kind: AffinityEvidenceKind
    evidence_key: str
    rank: int | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_text(self.source, "source")
        _require_enum(self.kind, AffinityEvidenceKind, "kind")
        _require_text(self.evidence_key, "evidence_key")
        _require_aware_datetime(self.observed_at, "observed_at")
        if self.kind in _TOP_EVIDENCE_KINDS:
            if type(self.rank) is not int or not 1 <= self.rank <= 50:
                raise ValueError("top affinity evidence rank must be between 1 and 50")
        elif self.rank is not None:
            raise ValueError("non-top affinity evidence must not have a rank")


@dataclass(frozen=True, slots=True)
class AffinityScore:
    total_points: int
    followed_count: int
    followed_points: int
    saved_track_count: int
    saved_track_points: int
    top_short_term_rank: int | None
    top_short_term_points: int
    top_medium_term_rank: int | None
    top_medium_term_points: int
    top_long_term_rank: int | None
    top_long_term_points: int

    def __post_init__(self) -> None:
        for name in (
            "total_points",
            "followed_count",
            "followed_points",
            "saved_track_count",
            "saved_track_points",
            "top_short_term_points",
            "top_medium_term_points",
            "top_long_term_points",
        ):
            _require_nonnegative_int(getattr(self, name), name)
        expected_followed = 100 if self.followed_count else 0
        expected_saved = min(self.saved_track_count * 2, 40)
        expected_short = _top_component_points(self.top_short_term_rank, 3)
        expected_medium = _top_component_points(self.top_medium_term_rank, 2)
        expected_long = _top_component_points(self.top_long_term_rank, 1)
        if self.followed_points != expected_followed:
            raise ValueError("followed_points does not match affinity-v1")
        if self.saved_track_points != expected_saved:
            raise ValueError("saved_track_points does not match affinity-v1")
        if self.top_short_term_points != expected_short:
            raise ValueError("top_short_term_points does not match affinity-v1")
        if self.top_medium_term_points != expected_medium:
            raise ValueError("top_medium_term_points does not match affinity-v1")
        if self.top_long_term_points != expected_long:
            raise ValueError("top_long_term_points does not match affinity-v1")
        expected_total = (
            expected_followed + expected_saved + expected_short + expected_medium + expected_long
        )
        if self.total_points != expected_total:
            raise ValueError("total_points does not match affinity-v1 components")


def _top_component_points(rank: object, weight: int) -> int:
    if rank is None:
        return 0
    if type(rank) is not int or not 1 <= rank <= 50:
        raise ValueError("top affinity rank must be between 1 and 50")
    return (51 - rank) * weight


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    artist: Artist
    inclusion_reason: WatchlistInclusionReason
    affinity: AffinityScore

    def __post_init__(self) -> None:
        if not isinstance(self.artist, Artist):
            raise ValueError("artist must be an Artist")
        _require_enum(
            self.inclusion_reason,
            WatchlistInclusionReason,
            "inclusion_reason",
        )
        if not isinstance(self.affinity, AffinityScore):
            raise ValueError("affinity must be an AffinityScore")


@dataclass(frozen=True, slots=True)
class SyncCapabilityResult:
    capability: SourceCapability
    status: SyncCapabilityStatus
    pages_seen: int
    artists_seen: int
    evidence_count: int

    def __post_init__(self) -> None:
        _require_enum(self.capability, SourceCapability, "capability")
        _require_enum(self.status, SyncCapabilityStatus, "status")
        _require_nonnegative_int(self.pages_seen, "pages_seen", maximum=100_000)
        _require_nonnegative_int(self.artists_seen, "artists_seen", maximum=100_000)
        _require_nonnegative_int(self.evidence_count, "evidence_count", maximum=100_000)


@dataclass(frozen=True, slots=True)
class CatalogSyncResult:
    capabilities: tuple[SyncCapabilityResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(item, SyncCapabilityResult) for item in self.capabilities
        ):
            raise ValueError("capabilities must be a tuple of sync results")
        capability_keys = tuple(item.capability for item in self.capabilities)
        if len(set(capability_keys)) != len(capability_keys):
            raise ValueError("capabilities must not contain duplicate results")


@dataclass(frozen=True, slots=True)
class WatchlistOverride:
    artist_local_id: RecordId
    action: WatchlistAction
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_enum(self.action, WatchlistAction, "action")
        _require_aware_datetime(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class LocalPreference:
    key: LocalPreferenceKey
    value: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_enum(self.key, LocalPreferenceKey, "key")
        _require_aware_datetime(self.updated_at, "updated_at")
        value = _require_text(self.value, "value")
        if self.key is LocalPreferenceKey.EVENT_COUNTRY_CODE:
            valid = _COUNTRY_CODE.fullmatch(value) is not None
        elif self.key is LocalPreferenceKey.EVENT_POSTAL_CODE:
            valid = _POSTAL_CODE.fullmatch(value) is not None
        elif self.key is LocalPreferenceKey.EVENT_RADIUS:
            valid = value.isascii() and value.isdigit() and 1 <= int(value) <= 100
        else:
            valid = value in {"miles", "kilometers"}
        if not valid:
            label = self.key.value.replace("_", " ")
            raise ValueError(f"{label} preference is invalid")


@dataclass(frozen=True, slots=True)
class RefreshMetric:
    kind: RefreshMetricKind
    count: int

    def __post_init__(self) -> None:
        _require_enum(self.kind, RefreshMetricKind, "kind")
        _require_nonnegative_int(self.count, "count")


@dataclass(frozen=True, slots=True)
class RefreshSummary:
    metrics: tuple[RefreshMetric, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, tuple) or not all(
            isinstance(metric, RefreshMetric) for metric in self.metrics
        ):
            raise ValueError("metrics must be a tuple of refresh metrics")
        if len(self.metrics) > len(RefreshMetricKind):
            raise ValueError("refresh summary contains too many metrics")
        kinds = tuple(metric.kind for metric in self.metrics)
        if len(set(kinds)) != len(kinds):
            raise ValueError("refresh summary must not contain duplicate metrics")


@dataclass(frozen=True, slots=True, eq=False)
class RefreshRun(_CanonicalRecord):
    local_id: RecordId
    source: str
    kind: RefreshKind
    status: RefreshStatus
    started_at: datetime
    finished_at: datetime | None
    summary: RefreshSummary

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_text(self.source, "source")
        _require_enum(self.kind, RefreshKind, "kind")
        _require_enum(self.status, RefreshStatus, "status")
        _require_aware_datetime(self.started_at, "started_at")
        if not isinstance(self.summary, RefreshSummary):
            raise ValueError("summary must be a RefreshSummary")
        if self.status is RefreshStatus.RUNNING:
            if self.finished_at is not None:
                raise ValueError("running refresh finished_at must be None")
            return
        if self.finished_at is None:
            raise ValueError("completed refresh finished_at must be supplied")
        _require_aware_datetime(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")


@dataclass(frozen=True, slots=True)
class SourceCursor:
    source: str
    capability: SourceCapability
    cursor: str
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_enum(self.capability, SourceCapability, "capability")
        cursor = _require_text(self.cursor, "cursor")
        if len(cursor) > 2048:
            raise ValueError("cursor must be at most 2048 characters")
        _require_aware_datetime(self.updated_at, "updated_at")


#: Ceiling and floor for the learned request-pacing window, in calls per window.
#: Persisted per source and adjusted with AIMD: halved on a 429, grown by one
#: call after a streak of successes, never leaving this inclusive range.
MAX_SOURCE_WINDOW_CALLS = 8
MIN_SOURCE_WINDOW_CALLS = 1

#: Cadence of the scheduled full refresh, in minutes. Shared by the CLI's schedule
#: installer and the release-discovery freshness TTL so the TTL always stays well
#: below the schedule interval, even when a scheduled run starts slightly early.
DAILY_REFRESH_MINUTES = 1440


@dataclass(frozen=True, slots=True)
class SourceLimitObservation:
    source: str
    state: SourceLimitState
    observed_at: datetime
    retry_at: datetime | None
    retry_is_exact: bool
    consecutive_limits: int
    window_calls: int = MAX_SOURCE_WINDOW_CALLS

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_enum(self.state, SourceLimitState, "state")
        _require_aware_datetime(self.observed_at, "observed_at")
        if type(self.retry_is_exact) is not bool:
            raise ValueError("retry_is_exact must be a boolean")
        _require_nonnegative_int(self.consecutive_limits, "consecutive_limits")
        if self.consecutive_limits > 1_000_000:
            raise ValueError("consecutive_limits must be at most 1000000")
        if type(self.window_calls) is not int or not (
            MIN_SOURCE_WINDOW_CALLS <= self.window_calls <= MAX_SOURCE_WINDOW_CALLS
        ):
            raise ValueError("window_calls must be within the supported pacing range")
        if self.retry_at is not None:
            _require_aware_datetime(self.retry_at, "retry_at")
        if self.state is SourceLimitState.AVAILABLE:
            valid = (
                self.retry_at is None
                and self.retry_is_exact is False
                and self.consecutive_limits == 0
            )
        elif self.state is SourceLimitState.COOLING_DOWN:
            valid = self.retry_at is not None and self.consecutive_limits >= 1
        else:
            valid = (
                self.retry_at is None
                and self.retry_is_exact is False
                and self.consecutive_limits >= 1
            )
        if not valid:
            raise ValueError("source limit state is inconsistent")


@dataclass(frozen=True, slots=True)
class ReleaseCheckCursor:
    source: str
    artist_local_id: RecordId
    last_successful_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_aware_datetime(self.last_successful_at, "last_successful_at")


@dataclass(frozen=True, slots=True)
class CatalogSyncCursor:
    source: str
    capability: str
    last_successful_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_text(self.capability, "capability")
        _require_aware_datetime(self.last_successful_at, "last_successful_at")


@dataclass(frozen=True, slots=True)
class ReleaseCheckContinuation:
    source: str
    artist_local_id: RecordId
    cursor: str
    since: datetime

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_record_id(self.artist_local_id, "artist_local_id")
        if type(self.cursor) is not str or _OPAQUE_CURSOR.fullmatch(self.cursor) is None:
            raise ValueError("cursor must be an opaque bounded token")
        _require_aware_datetime(self.since, "since")


@dataclass(frozen=True, slots=True)
class ReleaseDiscovery:
    release_local_id: RecordId
    source: str
    provider_native_id: str
    normalized_title: str
    release_date: date
    material_identity: str
    first_seen_at: datetime
    last_seen_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.release_local_id, "release_local_id")
        _require_text(self.source, "source")
        _require_text(self.provider_native_id, "provider_native_id")
        _require_inert_text(self.normalized_title, "normalized_title")
        _require_release_date(self.release_date)
        _require_safe_token(self.material_identity, "material_identity")
        _require_aware_datetime(self.first_seen_at, "first_seen_at")
        _require_aware_datetime(self.last_seen_at, "last_seen_at")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at must not precede first_seen_at")


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    release: Release
    artist_local_id: RecordId
    kind: ReleaseCandidateKind

    def __post_init__(self) -> None:
        if not isinstance(self.release, Release):
            raise ValueError("release must be a Release")
        _require_record_id(self.artist_local_id, "artist_local_id")
        if self.artist_local_id not in self.release.artist_refs:
            raise ValueError("artist_local_id must be a release artist")
        _require_enum(self.kind, ReleaseCandidateKind, "kind")


@dataclass(frozen=True, slots=True)
class ArtistReleaseDiscoveryResult:
    artist_local_id: RecordId
    status: ReleaseDiscoveryStatus
    records_seen: int
    candidates: tuple[ReleaseCandidate, ...]
    continuation: str | None

    def __post_init__(self) -> None:
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_enum(self.status, ReleaseDiscoveryStatus, "status")
        _require_nonnegative_int(self.records_seen, "records_seen", maximum=100)
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, ReleaseCandidate) for candidate in self.candidates
        ):
            raise ValueError("candidates must be a tuple of release candidates")
        if len(self.candidates) > self.records_seen:
            raise ValueError("candidates must not exceed records_seen")
        if any(candidate.artist_local_id != self.artist_local_id for candidate in self.candidates):
            raise ValueError("candidates must belong to the result artist")
        if self.continuation is not None and (
            type(self.continuation) is not str
            or _OPAQUE_CURSOR.fullmatch(self.continuation) is None
        ):
            raise ValueError("continuation must be an opaque bounded token or None")
        if self.status is ReleaseDiscoveryStatus.PARTIAL:
            if self.continuation is None or self.records_seen != 100:
                raise ValueError("partial discovery must stop at the record bound")
        elif self.continuation is not None:
            raise ValueError("only partial discovery may have a continuation")


@dataclass(frozen=True, slots=True)
class ReleaseDiscoveryResult:
    artists: tuple[ArtistReleaseDiscoveryResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.artists, tuple) or not all(
            isinstance(result, ArtistReleaseDiscoveryResult) for result in self.artists
        ):
            raise ValueError("artists must be a tuple of artist discovery results")
        artist_ids = tuple(result.artist_local_id for result in self.artists)
        if len(set(artist_ids)) != len(artist_ids):
            raise ValueError("artist discovery results must be unique")


@dataclass(frozen=True, slots=True)
class EventDiscovery:
    event_local_id: RecordId
    source: str
    provider_native_id: str
    artist_local_id: RecordId
    variant_identity: str
    material_identity: str
    attribution: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    fetched_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.event_local_id, "event_local_id")
        _require_text(self.source, "source")
        _require_text(self.provider_native_id, "provider_native_id")
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_safe_token(self.variant_identity, "variant_identity")
        _require_safe_token(self.material_identity, "material_identity")
        if self.attribution is not None:
            _require_inert_text(self.attribution, "attribution")
        _require_aware_datetime(self.first_seen_at, "first_seen_at")
        _require_aware_datetime(self.last_seen_at, "last_seen_at")
        _require_aware_datetime(self.fetched_at, "fetched_at")
        _require_aware_datetime(self.expires_at, "expires_at")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last_seen_at must not precede first_seen_at")
        if self.expires_at < self.fetched_at:
            raise ValueError("expires_at must not precede fetched_at")


@dataclass(frozen=True, slots=True)
class EventCandidate:
    event: Event
    artist_local_id: RecordId
    kind: EventCandidateKind

    def __post_init__(self) -> None:
        if not isinstance(self.event, Event):
            raise ValueError("event must be an Event")
        _require_record_id(self.artist_local_id, "artist_local_id")
        if self.artist_local_id not in self.event.artist_refs:
            raise ValueError("artist_local_id must be an event artist")
        _require_enum(self.kind, EventCandidateKind, "kind")


@dataclass(frozen=True, slots=True)
class ArtistEventDiscoveryResult:
    artist_local_id: RecordId
    status: EventDiscoveryStatus
    records_seen: int
    candidates: tuple[EventCandidate, ...]

    def __post_init__(self) -> None:
        _require_record_id(self.artist_local_id, "artist_local_id")
        _require_enum(self.status, EventDiscoveryStatus, "status")
        _require_nonnegative_int(self.records_seen, "records_seen", maximum=500)
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, EventCandidate) for candidate in self.candidates
        ):
            raise ValueError("candidates must be a tuple of event candidates")
        if len(self.candidates) > self.records_seen:
            raise ValueError("candidates must not exceed records_seen")
        if any(candidate.artist_local_id != self.artist_local_id for candidate in self.candidates):
            raise ValueError("candidates must belong to the result artist")
        if self.status in {EventDiscoveryStatus.CACHED, EventDiscoveryStatus.UNMATCHED} and (
            self.records_seen or self.candidates
        ):
            raise ValueError("cached or unmatched event results cannot contain candidates")


@dataclass(frozen=True, slots=True)
class EventDiscoveryResult:
    status: EventDiscoveryStatus
    artists: tuple[ArtistEventDiscoveryResult, ...]

    def __post_init__(self) -> None:
        _require_enum(self.status, EventDiscoveryStatus, "status")
        if not isinstance(self.artists, tuple) or not all(
            isinstance(result, ArtistEventDiscoveryResult) for result in self.artists
        ):
            raise ValueError("artists must be a tuple of artist discovery results")
        artist_ids = tuple(result.artist_local_id for result in self.artists)
        if len(set(artist_ids)) != len(artist_ids):
            raise ValueError("artist discovery results must be unique")
        if self.status is EventDiscoveryStatus.SKIPPED and self.artists:
            raise ValueError("skipped event discovery cannot contain artist results")


@dataclass(frozen=True, slots=True)
class ExplanationReason:
    kind: ExplanationReasonKind
    detail: str | None

    def __post_init__(self) -> None:
        _require_enum(self.kind, ExplanationReasonKind, "kind")
        if self.detail is not None:
            _require_inert_text(self.detail, "detail")


@dataclass(frozen=True, slots=True)
class Explanation:
    reasons: tuple[ExplanationReason, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reasons, tuple) or not all(
            isinstance(reason, ExplanationReason) for reason in self.reasons
        ):
            raise ValueError("reasons must be a tuple of explanation reasons")
        if not 1 <= len(self.reasons) <= 16:
            raise ValueError("explanation must contain 1..16 reasons")
        kinds = tuple(reason.kind for reason in self.reasons)
        if len(set(kinds)) != len(kinds):
            raise ValueError("explanation must not contain duplicate explanation reasons")


@dataclass(frozen=True, slots=True, eq=False)
class Signal(_CanonicalRecord):
    local_id: RecordId
    kind: SignalKind
    record_local_id: RecordId
    provider: str
    provider_native_id: str
    fingerprint: str
    material_version: str
    explanation: Explanation
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_enum(self.kind, SignalKind, "kind")
        _require_record_id(self.record_local_id, "record_local_id")
        _require_text(self.provider, "provider")
        _require_text(self.provider_native_id, "provider_native_id")
        _require_safe_token(self.fingerprint, "fingerprint")
        _require_safe_token(self.material_version, "material_version")
        if not isinstance(self.explanation, Explanation):
            raise ValueError("explanation must be an Explanation")
        _require_aware_datetime(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True, eq=False)
class InboxEntry(_CanonicalRecord):
    local_id: RecordId
    signal_local_id: RecordId
    state: InboxState
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_record_id(self.local_id, "local_id")
        _require_record_id(self.signal_local_id, "signal_local_id")
        _require_enum(self.state, InboxState, "state")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


__all__ = [
    "AffinityEvidence",
    "AffinityEvidenceKind",
    "AffinityScore",
    "Artist",
    "ArtistReleaseDiscoveryResult",
    "CatalogSyncResult",
    "CatalogItem",
    "CatalogItemBatch",
    "Event",
    "Explanation",
    "ExplanationReason",
    "ExplanationReasonKind",
    "IdentityConfidence",
    "InboxEntry",
    "InboxState",
    "Interest",
    "InterestKind",
    "InterestStatus",
    "LocalPreference",
    "LocalPreferenceKey",
    "Observation",
    "RecordId",
    "ReleaseCandidate",
    "ReleaseCandidateKind",
    "ReleaseCheckCursor",
    "ReleaseCheckContinuation",
    "ReleaseDiscovery",
    "ReleaseDiscoveryResult",
    "ReleaseDiscoveryStatus",
    "RefreshKind",
    "RefreshMetric",
    "RefreshMetricKind",
    "RefreshRun",
    "RefreshStatus",
    "RefreshSummary",
    "Release",
    "ReleaseDatePrecision",
    "Signal",
    "SignalKind",
    "SourceCapability",
    "SourceCursor",
    "MAX_SOURCE_WINDOW_CALLS",
    "MIN_SOURCE_WINDOW_CALLS",
    "SourceLimitObservation",
    "SourceLimitState",
    "SourceReference",
    "SyncCapabilityResult",
    "SyncCapabilityStatus",
    "WatchlistAction",
    "WatchlistEntry",
    "WatchlistInclusionReason",
    "WatchlistOverride",
]
