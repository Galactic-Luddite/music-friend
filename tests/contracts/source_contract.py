"""Reusable behavioral assertions for Music Friend source adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, TypeVar

import pytest

from music_friend.domain import Artist, CatalogItem, CatalogItemBatch, Release, SourceReference
from music_friend.domain.text import sanitize_source_text
from music_friend.errors import (
    AdditionalScopeRequiredError,
    AuthenticationRequiredError,
    CapabilityUnsupportedError,
    InvalidSourceResponseError,
    MusicFriendError,
    QuotaExhaustedError,
    RateLimitedError,
    SourceUnavailableError,
)
from music_friend.providers import (
    Capability,
    MusicSource,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)

FIXED_OBSERVED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEFAULT_PROVIDER_TEXT = "Synthetic <provider> [record]"
DEFAULT_SANITIZED_TEXT = "Synthetic ＜provider＞ ［record］"
SOURCE_TEXT_LIMIT = 96
CREDENTIAL_CANARY = "credential" + "-canary"
RAW_BODY_CANARY = "raw" + "-body-canary"


@dataclass(frozen=True, slots=True)
class TransportCall:
    """Safe, bounded description of one synthetic transport operation."""

    operation: str
    parameters: tuple[tuple[str, str | int | bool], ...] = ()

    def __post_init__(self) -> None:
        if not self.operation or len(self.operation) > 64:
            raise ValueError("transport operation must contain 1..64 characters")
        if len(self.parameters) > 8:
            raise ValueError("transport parameters must contain at most 8 entries")
        for name, value in self.parameters:
            if not name or len(name) > 64:
                raise ValueError("transport parameter names must contain 1..64 characters")
            if isinstance(value, str) and len(value) > 128:
                raise ValueError("transport parameter values must contain at most 128 characters")
            if type(value) is int and not -1_000_000 <= value <= 1_000_000:
                raise ValueError("transport integer parameters must be bounded")
            if not isinstance(value, (str, int, bool)):
                raise ValueError("transport parameter values must be bounded scalars")


class SourceFactory(Protocol):
    """Factory surface an adapter contract suite supplies."""

    def create(
        self,
        *,
        capabilities: frozenset[Capability],
        scenario: str = "happy_path",
        raw_text: str | None = None,
    ) -> MusicSource: ...

    def transport_calls(self) -> tuple[TransportCall, ...]: ...

    def example_artist_reference(self) -> SourceReference: ...


OperationResult = ProviderHealth | Page[Artist] | CatalogItemBatch | Page[Release]


@dataclass(frozen=True, slots=True)
class OperationCase:
    """One protocol operation and its observable contract."""

    operation: str
    capability: Capability
    invoke: Callable[[MusicSource, SourceFactory], OperationResult]
    expected_call: TransportCall
    record_type: type[Artist] | type[CatalogItem] | type[Release] | None = None
    text_field: str | None = None


def _health(source: MusicSource, _factory: SourceFactory) -> ProviderHealth:
    return source.health()


def _search_artists(source: MusicSource, _factory: SourceFactory) -> Page[Artist]:
    return source.search_artists("synthetic query", 10)


def _followed_artists(source: MusicSource, _factory: SourceFactory) -> Page[Artist]:
    return source.followed_artists()


def _saved_items(source: MusicSource, _factory: SourceFactory) -> CatalogItemBatch:
    return source.saved_items()


def _top_items(source: MusicSource, _factory: SourceFactory) -> CatalogItemBatch:
    return source.top_items("medium_term", 10)


def _top_artists(source: MusicSource, _factory: SourceFactory) -> Page[Artist]:
    return source.top_artists("medium_term", 10)


def _recent_releases(source: MusicSource, factory: SourceFactory) -> Page[Release]:
    return source.recent_releases((factory.example_artist_reference(),), FIXED_OBSERVED_AT)


OPERATION_CASES = (
    OperationCase("health", Capability.HEALTH, _health, TransportCall("health")),
    OperationCase(
        "search_artists",
        Capability.SEARCH_ARTISTS,
        _search_artists,
        TransportCall("search_artists", (("limit", 10),)),
        Artist,
        "display_name",
    ),
    OperationCase(
        "followed_artists",
        Capability.FOLLOWED_ARTISTS,
        _followed_artists,
        TransportCall("followed_artists", (("cursor_state", "start"),)),
        Artist,
        "display_name",
    ),
    OperationCase(
        "saved_items",
        Capability.SAVED_ITEMS,
        _saved_items,
        TransportCall("saved_items", (("cursor_state", "start"),)),
        CatalogItem,
        "title",
    ),
    OperationCase(
        "top_items",
        Capability.TOP_ITEMS,
        _top_items,
        TransportCall("top_items", (("limit", 10), ("range", "medium_term"))),
        CatalogItem,
        "title",
    ),
    OperationCase(
        "top_artists",
        Capability.TOP_ARTISTS,
        _top_artists,
        TransportCall("top_artists", (("limit", 10), ("range", "medium_term"))),
        Artist,
        "display_name",
    ),
    OperationCase(
        "recent_releases",
        Capability.RECENT_RELEASES,
        _recent_releases,
        TransportCall("recent_releases", (("artist_count", 1),)),
        Release,
        "title",
    ),
)
RECORD_OPERATION_CASES = tuple(case for case in OPERATION_CASES if case.record_type is not None)
FAILURE_SCENARIOS: tuple[tuple[str, type[MusicFriendError]], ...] = (
    ("timeout", SourceUnavailableError),
    ("authentication_required", AuthenticationRequiredError),
    ("quota_exhausted", QuotaExhaustedError),
    ("rate_limited", RateLimitedError),
)

E = TypeVar("E", bound=MusicFriendError)


def _capture_error(action: Callable[[], object], expected: type[E]) -> E:
    try:
        action()
    except expected as error:
        return error
    except Exception as error:  # pragma: no cover - assertion reports the adapter defect
        raise AssertionError(
            f"expected {expected.__name__}, received {type(error).__name__}"
        ) from error
    raise AssertionError(f"expected {expected.__name__}, no error was raised")


def _cases(operation_case: OperationCase | None) -> tuple[OperationCase, ...]:
    return OPERATION_CASES if operation_case is None else (operation_case,)


def _record_cases(operation_case: OperationCase | None) -> tuple[OperationCase, ...]:
    return RECORD_OPERATION_CASES if operation_case is None else (operation_case,)


def _create_and_assert_snapshot(
    factory: SourceFactory,
    operation_case: OperationCase,
    *,
    scenario: str = "happy_path",
    raw_text: str | None = None,
    granted: bool = True,
) -> MusicSource:
    capabilities = frozenset({operation_case.capability})
    source = factory.create(
        capabilities=capabilities,
        scenario=scenario,
        raw_text=raw_text,
    )
    assert factory.transport_calls() == (), "factory creation must not perform transport"
    snapshot = source.capabilities()
    assert type(snapshot) is ProviderCapabilities, "capabilities must be canonical"
    assert snapshot.supported == capabilities, "supported capability snapshot changed"
    expected_granted = capabilities if granted else frozenset()
    assert snapshot.granted == expected_granted, "granted capability snapshot changed"
    assert factory.transport_calls() == (), "capabilities must not perform transport"
    return source


def _assert_provenance(record: object, context: str) -> None:
    assert getattr(record, "observed_at", None) == FIXED_OBSERVED_AT, (
        f"{context} record missing fixed observation time"
    )
    source_refs = getattr(record, "source_refs", ())
    assert source_refs, f"{context} record missing source provenance"
    for reference in source_refs:
        assert reference.source, f"{context} record missing source provenance"
        assert reference.observed_at == FIXED_OBSERVED_AT, (
            f"{context} source provenance missing fixed observation time"
        )


def _validate_page_records(
    result: OperationResult,
    operation_case: OperationCase,
    *,
    page_label: str,
    expected_text: str,
    empty_expected: bool,
) -> tuple[object, ...]:
    if operation_case.record_type is CatalogItem:
        assert type(result) is CatalogItemBatch, (
            f"{operation_case.operation} must return a canonical CatalogItemBatch"
        )
        for artist_number, artist in enumerate(result.artists, start=1):
            context = f"{operation_case.operation} {page_label} artist {artist_number}"
            assert artist.display_name == expected_text, (
                f"{context} display_name provider text was not sanitized"
            )
            _assert_provenance(artist, context)
    else:
        assert type(result) is Page, f"{operation_case.operation} must return a canonical Page"
    assert len(result.items) <= 500, f"{operation_case.operation} page exceeds 500 records"
    if empty_expected:
        assert result.items == (), f"{operation_case.operation} {page_label} must be empty"
        return ()
    assert result.items, f"{operation_case.operation} {page_label} unexpectedly empty"
    assert operation_case.record_type is not None
    assert operation_case.text_field is not None
    records: list[object] = []
    for item_number, record in enumerate(result.items, start=1):
        context = f"{operation_case.operation} {page_label} item {item_number}"
        assert type(record) is operation_case.record_type, (
            f"{context} returned a noncanonical record"
        )
        assert getattr(record, operation_case.text_field) == expected_text, (
            f"{context} {operation_case.text_field} provider text was not sanitized"
        )
        _assert_provenance(record, context)
        records.append(record)
    return tuple(records)


def _assert_safe_error(error: MusicFriendError) -> None:
    representations = (str(error), repr(error), str(error.to_public_dict()))
    for canary in (CREDENTIAL_CANARY, RAW_BODY_CANARY):
        assert all(canary not in value for value in representations), (
            "public error leaked raw provider body"
        )


def assert_operation_contract(
    factory: SourceFactory,
    operation_case: OperationCase,
) -> None:
    source = _create_and_assert_snapshot(factory, operation_case)
    result = operation_case.invoke(source, factory)
    if operation_case.record_type is None:
        assert type(result) is ProviderHealth, "health must be canonical"
    else:
        _validate_page_records(
            result,
            operation_case,
            page_label="first page",
            expected_text=DEFAULT_SANITIZED_TEXT,
            empty_expected=False,
        )
    assert factory.transport_calls() == (operation_case.expected_call,), (
        f"{operation_case.operation} must record exactly its expected transport call"
    )


def assert_records_have_provenance(
    factory: SourceFactory,
    operation_case: OperationCase | None = None,
) -> None:
    for case in _record_cases(operation_case):
        source = _create_and_assert_snapshot(factory, case)
        _validate_page_records(
            case.invoke(source, factory),
            case,
            page_label="first page",
            expected_text=DEFAULT_SANITIZED_TEXT,
            empty_expected=False,
        )
        assert factory.transport_calls() == (case.expected_call,), (
            f"{case.operation} must record exactly its expected transport call"
        )


def assert_source_text_is_sanitized(
    factory: SourceFactory,
    raw_text: str,
    operation_case: OperationCase | None = None,
) -> None:
    expected = sanitize_source_text(raw_text, limit=SOURCE_TEXT_LIMIT)
    for case in _record_cases(operation_case):
        assert case.text_field is not None
        source = _create_and_assert_snapshot(factory, case, raw_text=raw_text)
        _validate_page_records(
            case.invoke(source, factory),
            case,
            page_label="first page",
            expected_text=expected,
            empty_expected=False,
        )
        assert factory.transport_calls() == (case.expected_call,), (
            f"{case.operation} source text caused unexpected transport"
        )


def assert_adversarial_text_remains_data(
    factory: SourceFactory,
    raw_text: str,
    forbidden_fragments: tuple[str, ...],
    operation_case: OperationCase,
) -> None:
    assert operation_case.text_field is not None
    source = _create_and_assert_snapshot(factory, operation_case, raw_text=raw_text)
    expected = sanitize_source_text(raw_text, limit=SOURCE_TEXT_LIMIT)
    records = _validate_page_records(
        operation_case.invoke(source, factory),
        operation_case,
        page_label="first page",
        expected_text=expected,
        empty_expected=False,
    )
    for record in records:
        text = getattr(record, operation_case.text_field)
        assert all(fragment not in text for fragment in forbidden_fragments), (
            "adversarial framing survived source sanitization"
        )
    assert factory.transport_calls() == (operation_case.expected_call,), (
        "adversarial text caused an unexpected transport call"
    )


def assert_pagination_is_bounded_and_stops(factory: SourceFactory) -> None:
    case = next(case for case in OPERATION_CASES if case.operation == "followed_artists")
    source = _create_and_assert_snapshot(factory, case)
    first = source.followed_artists()

    _validate_page_records(
        first,
        case,
        page_label="first page",
        expected_text=DEFAULT_SANITIZED_TEXT,
        empty_expected=False,
    )
    assert first.next_cursor is not None, "first synthetic page must expose continuation"
    assert len(first.next_cursor) <= 2048, "source cursor exceeds 2048 characters"
    assert factory.transport_calls() == (case.expected_call,)
    second = source.followed_artists(first.next_cursor)
    _validate_page_records(
        second,
        case,
        page_label="continuation page",
        expected_text=DEFAULT_SANITIZED_TEXT,
        empty_expected=False,
    )
    assert second.next_cursor is None, "pagination did not stop at the source end"
    expected_calls = (
        case.expected_call,
        TransportCall(
            "followed_artists",
            (("cursor_state", "continued"),),
        ),
    )
    assert factory.transport_calls() == expected_calls, (
        "pagination transport trace did not match exact bounded continuation calls"
    )


def assert_unsupported_before_transport(
    factory: SourceFactory,
    operation_case: OperationCase | None = None,
) -> None:
    for case in _cases(operation_case):
        source = factory.create(capabilities=frozenset())
        assert factory.transport_calls() == (), "factory creation must not perform transport"
        snapshot = source.capabilities()
        assert snapshot.supported == frozenset()
        assert snapshot.granted == frozenset()
        assert factory.transport_calls() == (), "capabilities must not perform transport"
        error = _capture_error(lambda: case.invoke(source, factory), CapabilityUnsupportedError)
        assert type(error) is CapabilityUnsupportedError
        assert factory.transport_calls() == (), (
            f"unsupported {case.operation} performed transport before rejection"
        )


def assert_missing_scope_before_transport(
    factory: SourceFactory,
    operation_case: OperationCase | None = None,
) -> None:
    for case in _cases(operation_case):
        source = _create_and_assert_snapshot(
            factory,
            case,
            scenario="missing_scope",
            granted=False,
        )
        error = _capture_error(lambda: case.invoke(source, factory), AdditionalScopeRequiredError)
        assert type(error) is AdditionalScopeRequiredError
        _assert_safe_error(error)
        assert factory.transport_calls() == (), f"ungranted {case.operation} performed transport"


def assert_error_maps_exactly(
    factory: SourceFactory,
    operation_case: OperationCase,
    scenario: str,
    expected: type[E],
) -> E:
    source = _create_and_assert_snapshot(factory, operation_case, scenario=scenario)
    error = _capture_error(lambda: operation_case.invoke(source, factory), expected)
    assert type(error) is expected, f"scenario must map to exact {expected.__name__}"
    _assert_safe_error(error)
    assert factory.transport_calls() == (operation_case.expected_call,), (
        f"failed {operation_case.operation} must record exactly its expected transport call"
    )
    return error


def assert_malformed_maps_exactly(
    factory: SourceFactory,
    operation_case: OperationCase,
) -> None:
    assert_error_maps_exactly(
        factory,
        operation_case,
        "malformed_record",
        InvalidSourceResponseError,
    )


def assert_error_text_is_redacted(factory: SourceFactory) -> None:
    for case in OPERATION_CASES:
        source = _create_and_assert_snapshot(
            factory,
            case,
            scenario="missing_scope",
            granted=False,
        )
        scope_error = _capture_error(lambda: case.invoke(source, factory), MusicFriendError)
        _assert_safe_error(scope_error)
        for scenario, expected in FAILURE_SCENARIOS:
            source = _create_and_assert_snapshot(factory, case, scenario=scenario)
            error = _capture_error(lambda: case.invoke(source, factory), MusicFriendError)
            _assert_safe_error(error)
    for case in RECORD_OPERATION_CASES:
        source = _create_and_assert_snapshot(factory, case, scenario="malformed_record")
        error = _capture_error(lambda: case.invoke(source, factory), MusicFriendError)
        _assert_safe_error(error)


def assert_retry_is_clamped(
    factory: SourceFactory,
    operation_case: OperationCase | None = None,
) -> None:
    for case in _cases(operation_case):
        error = assert_error_maps_exactly(factory, case, "rate_limited", RateLimitedError)
        assert error.retry_after_seconds == 900, "retry delay must clamp to 900 seconds"
        assert error.to_public_dict()["retry_after_seconds"] == 900, (
            "retry delay must clamp to 900 seconds"
        )


class MusicSourceContract:
    """Pytest mixin inherited by every concrete Music Friend adapter suite."""

    source_factory: SourceFactory

    @pytest.mark.parametrize("operation_case", OPERATION_CASES, ids=lambda case: case.operation)
    def test_operation_returns_canonical_result_and_exact_trace(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_operation_contract(self.source_factory, operation_case)

    @pytest.mark.parametrize(
        "operation_case",
        RECORD_OPERATION_CASES,
        ids=lambda case: case.operation,
    )
    def test_every_provider_text_field_is_sanitized(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_source_text_is_sanitized(
            self.source_factory,
            DEFAULT_PROVIDER_TEXT,
            operation_case,
        )

    @pytest.mark.parametrize(
        "operation_case",
        RECORD_OPERATION_CASES,
        ids=lambda case: case.operation,
    )
    def test_every_record_has_source_and_observation_time(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_records_have_provenance(self.source_factory, operation_case)

    def test_pagination_is_bounded_and_stops_at_source_end(self) -> None:
        assert_pagination_is_bounded_and_stops(self.source_factory)

    @pytest.mark.parametrize("operation_case", OPERATION_CASES, ids=lambda case: case.operation)
    def test_unsupported_capability_rejects_before_transport(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_unsupported_before_transport(self.source_factory, operation_case)

    @pytest.mark.parametrize("operation_case", OPERATION_CASES, ids=lambda case: case.operation)
    def test_missing_scope_rejects_before_transport(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_missing_scope_before_transport(self.source_factory, operation_case)

    @pytest.mark.parametrize("operation_case", OPERATION_CASES, ids=lambda case: case.operation)
    @pytest.mark.parametrize(
        ("scenario", "expected"),
        FAILURE_SCENARIOS,
        ids=lambda value: value if isinstance(value, str) else value.CATEGORY,
    )
    def test_public_failures_map_exactly_and_remain_redacted(
        self,
        operation_case: OperationCase,
        scenario: str,
        expected: type[MusicFriendError],
    ) -> None:
        assert_error_maps_exactly(self.source_factory, operation_case, scenario, expected)

    @pytest.mark.parametrize(
        "operation_case",
        RECORD_OPERATION_CASES,
        ids=lambda case: case.operation,
    )
    def test_malformed_record_maps_to_invalid_source_response(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_malformed_maps_exactly(self.source_factory, operation_case)

    @pytest.mark.parametrize("operation_case", OPERATION_CASES, ids=lambda case: case.operation)
    def test_retry_timing_clamps_to_nine_hundred_seconds(
        self,
        operation_case: OperationCase,
    ) -> None:
        assert_retry_is_clamped(self.source_factory, operation_case)
