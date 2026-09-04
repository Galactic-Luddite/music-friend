"""Deterministic, I/O-free synthetic MusicSource used by contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import NoReturn

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.domain.text import sanitize_source_text
from music_friend.errors import (
    AuthenticationRequiredError,
    InvalidSourceResponseError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
    require_capability,
)

from .source_contract import (
    CREDENTIAL_CANARY,
    DEFAULT_PROVIDER_TEXT,
    FIXED_OBSERVED_AT,
    RAW_BODY_CANARY,
    SOURCE_TEXT_LIMIT,
    TransportCall,
)

SCENARIOS = frozenset(
    {
        "authentication_required",
        "happy_path",
        "malformed_record",
        "missing_scope",
        "quota_exhausted",
        "rate_limited",
        "timeout",
    }
)
_FOLLOWED_CURSOR = "synthetic-followed-page-2"


class SyntheticMusicSource:
    """Stable in-memory source with explicit synthetic failure scenarios."""

    def __init__(
        self,
        *,
        capabilities: ProviderCapabilities,
        scenario: str,
        provider_text: str,
        calls: list[TransportCall],
    ) -> None:
        self._capabilities = capabilities
        self._scenario = scenario
        self._provider_text = provider_text
        self._calls = calls

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def health(self) -> ProviderHealth:
        self._begin("health", Capability.HEALTH)
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        bounded_limit = self._bounded_limit(limit)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        self._begin(
            "search_artists",
            Capability.SEARCH_ARTISTS,
            (("limit", bounded_limit),),
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("artist")
        return Page((self._artist("artist-search"),), None)

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise ValueError("cursor must be a bounded string or None")
        parameters: tuple[tuple[str, str | int | bool], ...]
        if cursor is None:
            parameters = (("cursor_state", "start"),)
        elif cursor == _FOLLOWED_CURSOR:
            parameters = (("cursor_state", "continued"),)
        else:
            parameters = (("cursor_state", "unknown"),)
        self._begin(
            "followed_artists",
            Capability.FOLLOWED_ARTISTS,
            parameters,
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("artist")
        if cursor is None:
            return Page((self._artist("artist-followed-1"),), _FOLLOWED_CURSOR)
        if cursor == _FOLLOWED_CURSOR:
            return Page((self._artist("artist-followed-2"),), None)
        raise InvalidSourceResponseError(CREDENTIAL_CANARY, RAW_BODY_CANARY)

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
            raise ValueError("cursor must be a bounded string or None")
        self._begin(
            "saved_items",
            Capability.SAVED_ITEMS,
            (("cursor_state", "start" if cursor is None else "continued"),),
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("catalog_item")
        return CatalogItemBatch(
            (self._catalog_item("saved", "saved-1"),), (self._artist("synthetic"),), None
        )

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        bounded_limit = self._bounded_limit(limit)
        if not isinstance(time_range, str) or len(time_range) > 64:
            raise ValueError("time_range must be a bounded string")
        self._begin(
            "top_items",
            Capability.TOP_ITEMS,
            (("limit", bounded_limit), ("range", time_range)),
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("catalog_item")
        return CatalogItemBatch(
            (self._catalog_item("track", "top-1"),), (self._artist("synthetic"),), None
        )

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        bounded_limit = self._bounded_limit(limit)
        if not isinstance(time_range, str) or len(time_range) > 64:
            raise ValueError("time_range must be a bounded string")
        self._begin(
            "top_artists",
            Capability.TOP_ARTISTS,
            (("limit", bounded_limit), ("range", time_range)),
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("artist")
        return Page((self._artist("artist-top"),), None)

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
    ) -> Page[Release]:
        if not isinstance(since, datetime) or since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone-aware")
        reference_count = min(len(artist_refs), 500)
        self._begin(
            "recent_releases",
            Capability.RECENT_RELEASES,
            (("artist_count", reference_count),),
        )
        if self._scenario == "malformed_record":
            return self._malformed_response("release")
        return Page((self._release("release-1"),), None)

    def _begin(
        self,
        operation: str,
        capability: Capability,
        parameters: tuple[tuple[str, str | int | bool], ...] = (),
    ) -> None:
        require_capability(self._capabilities, capability)
        self._record_transport(operation, parameters)
        if self._scenario == "timeout":
            raise SourceUnavailableError(CREDENTIAL_CANARY, RAW_BODY_CANARY)
        if self._scenario == "authentication_required":
            raise AuthenticationRequiredError(CREDENTIAL_CANARY, RAW_BODY_CANARY)
        if self._scenario == "quota_exhausted":
            raise QuotaExhaustedError(CREDENTIAL_CANARY, RAW_BODY_CANARY)
        if self._scenario == "rate_limited":
            raise self._rate_limited_error()

    def _record_transport(
        self,
        operation: str,
        parameters: tuple[tuple[str, str | int | bool], ...] = (),
    ) -> None:
        self._calls.append(TransportCall(operation, parameters))

    def _clean_text(self) -> str:
        return sanitize_source_text(self._provider_text, limit=SOURCE_TEXT_LIMIT)

    def _source_reference(self, native_id: str) -> SourceReference:
        return SourceReference(
            source="synthetic",
            native_id=native_id,
            canonical_url=None,
            observed_at=FIXED_OBSERVED_AT,
        )

    def _artist(self, native_id: str) -> Artist:
        return Artist(
            local_id=f"artist:{native_id}",
            display_name=self._clean_text(),
            source_refs=(self._source_reference(native_id),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=FIXED_OBSERVED_AT,
        )

    def _catalog_item(self, kind: str, native_id: str) -> CatalogItem:
        return CatalogItem(
            kind=kind,
            local_id=f"item:{native_id}",
            title=self._clean_text(),
            artist_refs=("artist:synthetic",),
            source_refs=(self._source_reference(native_id),),
            observed_at=FIXED_OBSERVED_AT,
        )

    def _release(self, native_id: str) -> Release:
        return Release(
            local_id=f"release:{native_id}",
            title=self._clean_text(),
            release_type="album",
            release_date=date(2026, 1, 1),
            date_precision=ReleaseDatePrecision.DAY,
            artist_refs=("artist:synthetic",),
            source_refs=(self._source_reference(native_id),),
            observed_at=FIXED_OBSERVED_AT,
        )

    def _malformed_response(self, record_kind: str) -> NoReturn:
        try:
            if record_kind == "artist":
                Artist(
                    local_id="artist:malformed",
                    display_name="",
                    source_refs=(self._source_reference("malformed"),),
                    identity_confidence=IdentityConfidence.SOURCE_ONLY,
                    observed_at=FIXED_OBSERVED_AT,
                )
            elif record_kind == "catalog_item":
                CatalogItem(
                    kind="track",
                    local_id="item:malformed",
                    title="",
                    artist_refs=("artist:synthetic",),
                    source_refs=(self._source_reference("malformed"),),
                    observed_at=FIXED_OBSERVED_AT,
                )
            elif record_kind == "release":
                Release(
                    local_id="release:malformed",
                    title="",
                    release_type="album",
                    release_date=date(2026, 1, 1),
                    date_precision=ReleaseDatePrecision.DAY,
                    artist_refs=("artist:synthetic",),
                    source_refs=(self._source_reference("malformed"),),
                    observed_at=FIXED_OBSERVED_AT,
                )
            else:
                raise ValueError("unknown synthetic record kind")
        except (TypeError, ValueError) as error:
            raise InvalidSourceResponseError(CREDENTIAL_CANARY, RAW_BODY_CANARY) from error
        raise AssertionError("malformed record unexpectedly passed validation")

    def _rate_limited_error(self) -> RateLimitedError:
        return RateLimitedError("901")

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        return limit


class SyntheticSourceFactory:
    """Create isolated synthetic sources and expose their safe call trace."""

    def __init__(
        self,
        *,
        provider_text: str = DEFAULT_PROVIDER_TEXT,
        source_type: type[SyntheticMusicSource] = SyntheticMusicSource,
    ) -> None:
        if not isinstance(provider_text, str):
            raise ValueError("provider_text must be a string")
        if len(provider_text) > 4096:
            raise ValueError("provider_text must contain at most 4096 characters")
        self._provider_text = provider_text
        self._source_type = source_type
        self._calls: list[TransportCall] = []

    def create(
        self,
        *,
        capabilities: frozenset[Capability],
        scenario: str = "happy_path",
        raw_text: str | None = None,
    ) -> SyntheticMusicSource:
        if type(capabilities) is not frozenset or not all(
            isinstance(capability, Capability) for capability in capabilities
        ):
            raise ValueError("capabilities must be a frozenset of Capability values")
        if scenario not in SCENARIOS:
            raise ValueError("unknown synthetic scenario")
        if raw_text is not None:
            if not isinstance(raw_text, str):
                raise ValueError("raw_text must be a string or None")
            if len(raw_text) > 4096:
                raise ValueError("raw_text must contain at most 4096 characters")
        payload = self._provider_text if raw_text is None else raw_text
        granted = frozenset() if scenario == "missing_scope" else capabilities
        resolved = ProviderCapabilities(supported=capabilities, granted=granted)
        self._calls.clear()
        return self._source_type(
            capabilities=resolved,
            scenario=scenario,
            provider_text=payload,
            calls=self._calls,
        )

    def transport_calls(self) -> tuple[TransportCall, ...]:
        return tuple(self._calls)

    def example_artist_reference(self) -> SourceReference:
        return SourceReference(
            source="synthetic",
            native_id="artist-1",
            canonical_url=None,
            observed_at=FIXED_OBSERVED_AT,
        )
