"""DeezerSource against the shared source-contract assertion vocabulary.

Mirrors ``tests/providers/musicbrainz/test_source_contract.py``: DeezerSource is
architecturally the same shape as MusicBrainzSource -- a single-capability
(``RECENT_RELEASES`` only), keyless, no-auth, no-quota adapter -- so this reuses
the shared assertion functions scoped to ``recent_releases`` rather than the
generic ``OPERATION_CASES`` parametrization, which assumes an adapter that can
support any subset of the seven ``Capability`` values.

Fixture album shape is taken from a real, live, unauthenticated GET against
Deezer's public API (recon for issue #42, 2026-09-26): ``GET
https://api.deezer.com/artist/27/albums?limit=5`` (artist id 27, a real public
artist). The synthetic fixtures below use the same keys, types, and structure
observed live, with artist/album identity replaced by synthetic values.
"""

from __future__ import annotations

import pytest

from music_friend.domain import SourceReference
from music_friend.errors import InvalidSourceResponseError, RateLimitedError, SourceUnavailableError
from music_friend.providers import Capability
from music_friend.providers.deezer.source import DeezerSource
from tests.contracts.source_contract import (
    DEFAULT_PROVIDER_TEXT,
    FIXED_OBSERVED_AT,
    OPERATION_CASES,
    TransportCall,
    assert_adversarial_text_remains_data,
    assert_error_maps_exactly,
    assert_malformed_maps_exactly,
    assert_operation_contract,
    assert_records_have_provenance,
    assert_retry_is_clamped,
    assert_source_text_is_sanitized,
)

_RECENT_RELEASES_CASE = next(
    case for case in OPERATION_CASES if case.operation == "recent_releases"
)
_DEEZER_ARTIST_ID = "1000001"
_DEEZER_ALBUM_ID = 2000002


class _FakeTransport:
    """Records calls and returns scripted responses per the shared scenario names."""

    def __init__(self) -> None:
        self.calls: list[TransportCall] = []
        self.scenario = "happy_path"
        self.text = DEFAULT_PROVIDER_TEXT
        self.pages: list[dict[str, object]] = []

    def get(self, path: str, query: object = None) -> object:
        self.calls.append(TransportCall("recent_releases", (("artist_count", 1),)))
        if self.scenario == "timeout":
            raise SourceUnavailableError()
        if self.scenario == "rate_limited":
            raise RateLimitedError(retry_after_seconds=999_999)
        if self.scenario == "malformed_record":
            # Real Deezer album objects are dicts; a fundamentally wrong shape
            # (not merely one invalid album, which the normalizer tolerates by
            # dropping it) must be rejected by the source itself.
            return {"data": "not-a-list"}
        if self.pages:
            return self.pages.pop(0)
        album = {
            "id": _DEEZER_ALBUM_ID,
            "title": self.text,
            "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID}",
            "cover": f"https://api.deezer.com/album/{_DEEZER_ALBUM_ID}/image",
            "cover_small": "https://cdn-images.dzcdn.net/images/cover/x/56x56.jpg",
            "cover_medium": "https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg",
            "cover_big": "https://cdn-images.dzcdn.net/images/cover/x/500x500.jpg",
            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/x/1000x1000.jpg",
            "md5_image": "0" * 32,
            "genre_id": 113,
            "fans": 42,
            "release_date": "2026-01-01",
            "record_type": "album",
            "tracklist": f"https://api.deezer.com/album/{_DEEZER_ALBUM_ID}/tracks",
            "explicit_lyrics": False,
            "type": "album",
        }
        return {"data": [album], "total": 1}

    def close(self) -> None:
        pass


class DeezerSourceFactory:
    """The ``SourceFactory`` surface, scoped to ``recent_releases``."""

    def __init__(self) -> None:
        self._transport = _FakeTransport()

    def create(
        self,
        *,
        capabilities: frozenset[Capability],
        scenario: str = "happy_path",
        raw_text: str | None = None,
    ) -> DeezerSource:
        assert capabilities == frozenset({Capability.RECENT_RELEASES}), (
            "this factory is scoped to the RECENT_RELEASES-only contract"
        )
        self._transport = _FakeTransport()
        self._transport.scenario = scenario
        if raw_text is not None:
            self._transport.text = raw_text
        return DeezerSource(transport=self._transport, clock=lambda: FIXED_OBSERVED_AT)  # type: ignore[arg-type]

    def transport_calls(self) -> tuple[TransportCall, ...]:
        return tuple(self._transport.calls)

    def example_artist_reference(self) -> SourceReference:
        return SourceReference(
            source="deezer",
            native_id=_DEEZER_ARTIST_ID,
            canonical_url=f"https://www.deezer.com/artist/{_DEEZER_ARTIST_ID}",
            observed_at=FIXED_OBSERVED_AT,
        )


def test_recent_releases_returns_canonical_result_and_exact_trace() -> None:
    assert_operation_contract(DeezerSourceFactory(), _RECENT_RELEASES_CASE)


def test_recent_releases_records_have_source_and_observation_time() -> None:
    assert_records_have_provenance(DeezerSourceFactory(), _RECENT_RELEASES_CASE)


def test_recent_releases_title_is_sanitized() -> None:
    assert_source_text_is_sanitized(
        DeezerSourceFactory(), DEFAULT_PROVIDER_TEXT, _RECENT_RELEASES_CASE
    )


def test_adversarial_title_text_remains_data() -> None:
    assert_adversarial_text_remains_data(
        DeezerSourceFactory(),
        "\x1b]8;;https://evil.example\x07click here\x1b]8;;\x07",
        ("\x1b", "\x07"),
        _RECENT_RELEASES_CASE,
    )


def test_malformed_recent_releases_response_maps_to_invalid_source_response() -> None:
    assert_malformed_maps_exactly(DeezerSourceFactory(), _RECENT_RELEASES_CASE)


def test_timeout_maps_to_source_unavailable_and_stays_redacted() -> None:
    assert_error_maps_exactly(
        DeezerSourceFactory(), _RECENT_RELEASES_CASE, "timeout", SourceUnavailableError
    )


def test_rate_limited_maps_exactly_and_stays_redacted() -> None:
    assert_error_maps_exactly(
        DeezerSourceFactory(), _RECENT_RELEASES_CASE, "rate_limited", RateLimitedError
    )


def test_retry_timing_clamps_to_nine_hundred_seconds() -> None:
    assert_retry_is_clamped(DeezerSourceFactory(), _RECENT_RELEASES_CASE)


def test_every_failure_scenario_stays_redacted() -> None:
    for scenario, expected in (
        ("timeout", SourceUnavailableError),
        ("rate_limited", RateLimitedError),
        ("malformed_record", InvalidSourceResponseError),
    ):
        error = assert_error_maps_exactly(
            DeezerSourceFactory(), _RECENT_RELEASES_CASE, scenario, expected
        )
        representations = (str(error), repr(error), str(error.to_public_dict()))
        assert all("raw-body-canary" not in value for value in representations)
        assert all("credential-canary" not in value for value in representations)


def test_recent_releases_pagination_is_bounded_and_stops_at_source_end() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    artist_ref = factory.example_artist_reference()
    factory._transport.pages = [
        {
            "data": [
                {
                    "id": _DEEZER_ALBUM_ID,
                    "title": DEFAULT_PROVIDER_TEXT,
                    "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID}",
                    "release_date": "2026-01-01",
                    "record_type": "album",
                }
            ],
            "total": 2,
            "next": "https://api.deezer.com/artist/1000001/albums?limit=1&index=1",
        },
        {
            "data": [
                {
                    "id": _DEEZER_ALBUM_ID + 1,
                    "title": DEFAULT_PROVIDER_TEXT,
                    "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID + 1}",
                    "release_date": "2025-01-01",
                    "record_type": "album",
                }
            ],
            "total": 2,
        },
    ]

    from datetime import datetime, timezone

    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    first = source.recent_releases((artist_ref,), since)
    assert len(first.items) == 1
    assert first.next_cursor == "1"

    second = source.recent_releases((artist_ref,), since, first.next_cursor)
    assert len(second.items) == 1
    assert second.next_cursor is None


def test_recent_releases_filters_by_since_client_side() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    artist_ref = factory.example_artist_reference()
    factory._transport.pages = [
        {
            "data": [
                {
                    "id": _DEEZER_ALBUM_ID,
                    "title": "Too Old",
                    "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID}",
                    "release_date": "2020-01-01",
                    "record_type": "album",
                },
                {
                    "id": _DEEZER_ALBUM_ID + 1,
                    "title": "Recent Enough",
                    "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID + 1}",
                    "release_date": "2026-06-01",
                    "record_type": "single",
                },
            ],
            "total": 2,
        }
    ]
    from datetime import datetime, timezone

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    page = source.recent_releases((artist_ref,), since)
    assert len(page.items) == 1
    assert page.items[0].title == "Recent Enough"
    assert page.items[0].release_type == "single"


def test_health_does_not_perform_a_transport_call() -> None:
    from music_friend.providers.deezer.source import DeezerSource

    class _NoCallTransport:
        def get(self, path: str, query: object = None) -> object:
            raise AssertionError("health must not perform a transport call")

        def close(self) -> None:
            pass

    source = DeezerSource(transport=_NoCallTransport())
    health = source.health()
    assert health.capabilities.granted == frozenset({Capability.RECENT_RELEASES})


def test_unsupported_methods_raise_before_any_transport_call() -> None:
    from music_friend.errors import CapabilityUnsupportedError
    from music_friend.providers.deezer.source import DeezerSource

    class _NoCallTransport:
        def get(self, path: str, query: object = None) -> object:
            raise AssertionError("unsupported methods must not perform a transport call")

        def close(self) -> None:
            pass

    source = DeezerSource(transport=_NoCallTransport())
    with pytest.raises(CapabilityUnsupportedError):
        source.search_artists("query", 10)
    with pytest.raises(CapabilityUnsupportedError):
        source.followed_artists()
    with pytest.raises(CapabilityUnsupportedError):
        source.saved_items()
    with pytest.raises(CapabilityUnsupportedError):
        source.top_items("medium_term", 10)
    with pytest.raises(CapabilityUnsupportedError):
        source.top_artists("medium_term", 10)


def test_recent_releases_rejects_zero_deezer_artist_references() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    with pytest.raises(ValueError):
        source.recent_releases((), FIXED_OBSERVED_AT)


def test_recent_releases_rejects_multiple_deezer_artist_references() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    with pytest.raises(ValueError):
        source.recent_releases((ref, ref), FIXED_OBSERVED_AT)


def test_recent_releases_rejects_a_non_integer_cursor() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((ref,), FIXED_OBSERVED_AT, "not-an-int")


def test_recent_releases_rejects_a_negative_cursor() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((ref,), FIXED_OBSERVED_AT, "-1")


def test_recent_releases_rejects_a_non_object_response() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    factory._transport.pages = ["not-an-object"]  # type: ignore[list-item]
    with pytest.raises(InvalidSourceResponseError):
        source.recent_releases((ref,), FIXED_OBSERVED_AT)


def test_recent_releases_skips_an_album_that_fails_normalization() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    factory._transport.pages = [{"data": [{"id": "not-an-int", "title": "x"}], "total": 1}]
    page = source.recent_releases((ref,), FIXED_OBSERVED_AT)
    assert page.items == ()


def test_recent_releases_skips_a_non_object_album_entry() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    factory._transport.pages = [{"data": ["not-an-object"], "total": 1}]
    page = source.recent_releases((ref,), FIXED_OBSERVED_AT)
    assert page.items == ()


def test_recent_releases_next_cursor_is_none_when_page_is_empty() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    ref = factory.example_artist_reference()
    factory._transport.pages = [{"data": [], "total": 0, "next": "https://api.deezer.com/x"}]
    page = source.recent_releases((ref,), FIXED_OBSERVED_AT)
    assert page.next_cursor is None


def test_record_type_maps_to_release_type() -> None:
    factory = DeezerSourceFactory()
    source = factory.create(capabilities=frozenset({Capability.RECENT_RELEASES}))
    artist_ref = factory.example_artist_reference()
    factory._transport.pages = [
        {
            "data": [
                {
                    "id": _DEEZER_ALBUM_ID,
                    "title": "EP Title",
                    "link": f"https://www.deezer.com/album/{_DEEZER_ALBUM_ID}",
                    "release_date": "2026-01-01",
                    "record_type": "ep",
                }
            ],
            "total": 1,
        }
    ]
    page = source.recent_releases((artist_ref,), FIXED_OBSERVED_AT)
    assert page.items[0].release_type == "ep"
