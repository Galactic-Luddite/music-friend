"""Meta-tests proving each reusable assertion catches its intended defect."""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn, cast

import pytest

from music_friend.domain import Artist, CatalogItem, IdentityConfidence, SourceReference
from music_friend.errors import InvalidSourceResponseError, RateLimitedError
from music_friend.providers import Page

from .source_contract import (
    DEFAULT_PROVIDER_TEXT,
    FIXED_OBSERVED_AT,
    OPERATION_CASES,
    RAW_BODY_CANARY,
    SourceFactory,
    TransportCall,
    assert_error_text_is_redacted,
    assert_operation_contract,
    assert_pagination_is_bounded_and_stops,
    assert_records_have_provenance,
    assert_retry_is_clamped,
    assert_source_text_is_sanitized,
    assert_unsupported_before_transport,
)
from .synthetic_source import SyntheticMusicSource, SyntheticSourceFactory


class MissingProvenanceSource(SyntheticMusicSource):
    def _artist(self, native_id: str) -> Artist:
        artist = super()._artist(native_id)
        object.__setattr__(artist, "source_refs", ())
        return artist


class UnsanitizedTextSource(SyntheticMusicSource):
    def _clean_text(self) -> str:
        return self._provider_text


class UnsupportedTransportSource(SyntheticMusicSource):
    def top_items(self, time_range: str, limit: int) -> Page[CatalogItem]:
        if not self._capabilities.supported:
            self._record_transport("top_items", (("limit", limit),))
        return super().top_items(time_range, limit)


class UnboundedCursorSource(SyntheticMusicSource):
    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        if cursor is not None:
            return super().followed_artists(cursor)
        page = super().followed_artists(cursor)
        object.__setattr__(page, "next_cursor", "x" * 2049)
        return page


class LeakyInvalidSourceResponseError(InvalidSourceResponseError):
    def __str__(self) -> str:
        return "invalid response: " + RAW_BODY_CANARY


class RawBodyErrorSource(SyntheticMusicSource):
    def _malformed_response(self, record_kind: str) -> NoReturn:
        raise LeakyInvalidSourceResponseError()


class RetryClampSource(SyntheticMusicSource):
    def _rate_limited_error(self) -> RateLimitedError:
        error = RateLimitedError(900)
        error.retry_after_seconds = 901
        return error


class WrongTypeSecondItemSource(SyntheticMusicSource):
    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        page = super().search_artists(query, limit)
        wrong = self._catalog_item("track", "wrong-type-2")
        return cast(Page[Artist], Page(page.items + (wrong,), page.next_cursor))


class UnsanitizedSecondItemSource(SyntheticMusicSource):
    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        page = super().search_artists(query, limit)
        unsanitized = Artist(
            local_id="artist:unsanitized-2",
            display_name=self._provider_text,
            source_refs=(self._source_reference("unsanitized-2"),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=FIXED_OBSERVED_AT,
        )
        return Page(page.items + (unsanitized,), page.next_cursor)


class MissingProvenanceSecondItemSource(SyntheticMusicSource):
    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        page = super().search_artists(query, limit)
        missing = self._artist("missing-provenance-2")
        object.__setattr__(missing, "source_refs", ())
        return Page(page.items + (missing,), page.next_cursor)


class SecondPageDefectSource(SyntheticMusicSource):
    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        page = super().followed_artists(cursor)
        if cursor is None:
            return page
        missing = self._artist("continuation-missing-provenance-2")
        object.__setattr__(missing, "source_refs", ())
        return Page(page.items + (missing,), page.next_cursor)


class RawContinuationParametersSource(SyntheticMusicSource):
    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        page = super().followed_artists(cursor)
        if cursor is not None:
            self._calls[-1] = TransportCall(
                "followed_artists",
                (("cursor", "raw-continuation"), ("extra", "unexpected")),
            )
        return page


class ReferenceRecordingSource(SyntheticMusicSource):
    received_refs: tuple[SourceReference, ...] = ()

    def recent_releases(self, artist_refs, since):  # type: ignore[no-untyped-def]
        type(self).received_refs = tuple(artist_refs)
        return super().recent_releases(artist_refs, since)


class ReferenceRecordingFactory(SyntheticSourceFactory):
    def __init__(self) -> None:
        super().__init__(source_type=ReferenceRecordingSource)

    def example_artist_reference(self) -> SourceReference:
        return SourceReference(
            source="factory-specific",
            native_id="factoryartist1",
            canonical_url=None,
            observed_at=FIXED_OBSERVED_AT,
        )


def _factory(source_type: type[SyntheticMusicSource]) -> SyntheticSourceFactory:
    return SyntheticSourceFactory(
        provider_text=DEFAULT_PROVIDER_TEXT,
        source_type=source_type,
    )


ContractAssertion = Callable[[SourceFactory], None]
SEARCH_CASE = next(case for case in OPERATION_CASES if case.operation == "search_artists")


def _sanitization_assertion(factory: SourceFactory) -> None:
    assert_source_text_is_sanitized(factory, DEFAULT_PROVIDER_TEXT)


def _operation_assertion(factory: SourceFactory) -> None:
    assert_operation_contract(factory, SEARCH_CASE)


@pytest.mark.parametrize(
    ("source_type", "assertion", "expected_reason"),
    [
        (
            MissingProvenanceSource,
            assert_records_have_provenance,
            "search_artists first page item 1 record missing source provenance",
        ),
        (
            UnsanitizedTextSource,
            _sanitization_assertion,
            "search_artists first page item 1 display_name provider text was not sanitized",
        ),
        (
            UnsupportedTransportSource,
            assert_unsupported_before_transport,
            "unsupported top_items performed transport before rejection",
        ),
        (
            UnboundedCursorSource,
            assert_pagination_is_bounded_and_stops,
            "source cursor exceeds 2048 characters",
        ),
        (
            RawBodyErrorSource,
            assert_error_text_is_redacted,
            "public error leaked raw provider body",
        ),
        (
            RetryClampSource,
            assert_retry_is_clamped,
            "retry delay must clamp to 900 seconds",
        ),
        (
            WrongTypeSecondItemSource,
            _operation_assertion,
            "search_artists first page item 2 returned a noncanonical record",
        ),
        (
            UnsanitizedSecondItemSource,
            _sanitization_assertion,
            "search_artists first page item 2 display_name provider text was not sanitized",
        ),
        (
            MissingProvenanceSecondItemSource,
            assert_records_have_provenance,
            "search_artists first page item 2 record missing source provenance",
        ),
        (
            SecondPageDefectSource,
            assert_pagination_is_bounded_and_stops,
            "followed_artists continuation page item 2 record missing source provenance",
        ),
        (
            RawContinuationParametersSource,
            assert_pagination_is_bounded_and_stops,
            "pagination transport trace did not match exact bounded continuation calls",
        ),
    ],
)
def test_each_deliberate_defect_fails_exactly_its_targeted_contract_assertion(
    source_type: type[SyntheticMusicSource],
    assertion: ContractAssertion,
    expected_reason: str,
) -> None:
    captured: AssertionError | None = None
    try:
        assertion(_factory(source_type))
    except AssertionError as error:
        captured = error

    assert captured is not None
    assert str(captured) == expected_reason


def test_recent_release_contract_invokes_created_source_with_factory_reference() -> None:
    case = next(case for case in OPERATION_CASES if case.operation == "recent_releases")
    factory = ReferenceRecordingFactory()
    ReferenceRecordingSource.received_refs = ()

    assert_operation_contract(factory, case)

    assert ReferenceRecordingSource.received_refs == (factory.example_artist_reference(),)
