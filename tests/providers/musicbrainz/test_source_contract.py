"""MusicBrainzSource against the shared source-contract assertion vocabulary.

The full ``MusicSourceContract`` pytest mixin in ``tests/contracts/source_contract.py``
assumes an adapter that can support and be granted any subset of the seven
``Capability`` values (as Spotify does) and that maps an authentication or quota
failure distinctly from a rate limit. MusicBrainzSource is architecturally not that
shape: it is a single-capability (``RECENT_RELEASES`` only), keyless, no-auth,
no-quota adapter by design (see the release-source design doc, "MusicBrainzSource"
section) -- ``capabilities()`` always reports ``supported == granted ==
{RECENT_RELEASES}`` regardless of what a caller might request, and there is no
``AuthenticationRequiredError``/``QuotaExhaustedError`` concept for a keyless service.
Applying the generic mixin's ``OPERATION_CASES`` parametrization verbatim would
fabricate authentication/quota failure scenarios MusicBrainz cannot actually produce.

This file instead reuses the same shared assertion functions
(``assert_operation_contract``, ``assert_records_have_provenance``,
``assert_source_text_is_sanitized``, ``assert_adversarial_text_remains_data``,
``assert_malformed_maps_exactly``, ``assert_error_maps_exactly``,
``assert_retry_is_clamped``) against synthetic fixtures, scoped to the one
operation MusicBrainzSource actually supports: ``recent_releases``. Coverage for
"unsupported capability rejects before any transport call" (every other
``MusicSource`` method) and the batch/name-search mapping endpoints (not part of
the generic ``MusicSource`` contract at all) lives in ``test_source.py`` alongside
this file.
"""

from __future__ import annotations

from music_friend.domain import SourceReference
from music_friend.errors import RateLimitedError, SourceUnavailableError
from music_friend.providers import Capability
from music_friend.providers.musicbrainz.source import MusicBrainzSource
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
_MBID = "11111111-1111-1111-1111-111111111111"
_RGID = "22222222-2222-2222-2222-222222222222"


class _FakeTransport:
    """Records calls and returns scripted responses per the shared scenario names."""

    def __init__(self) -> None:
        self.calls: list[TransportCall] = []
        self.scenario = "happy_path"
        self.text = DEFAULT_PROVIDER_TEXT

    def get(self, path: str, query: object = None) -> object:
        self.calls.append(TransportCall("recent_releases", (("artist_count", 1),)))
        if self.scenario == "timeout":
            raise SourceUnavailableError()
        if self.scenario == "rate_limited":
            raise RateLimitedError(retry_after_seconds=999_999)
        if self.scenario == "malformed_record":
            # An unparseable response shape (not simply one invalid release-group,
            # which the normalizer tolerates by dropping it): the source itself must
            # reject this before it ever reaches normalize_release_group().
            return {"release-groups": "not-a-list"}
        release_group = {
            "id": _RGID,
            "title": self.text,
            "primary-type": "Album",
            "first-release-date": "2026-01-01",
            "score": 100,
            "artist-credit": [{"artist": {"id": _MBID}}],
        }
        return {"release-groups": [release_group], "release-group-count": 1}

    def close(self) -> None:
        pass


class MusicBrainzSourceFactory:
    """The ``SourceFactory`` surface, scoped to ``recent_releases``."""

    def __init__(self) -> None:
        self._transport = _FakeTransport()

    def create(
        self,
        *,
        capabilities: frozenset[Capability],
        scenario: str = "happy_path",
        raw_text: str | None = None,
    ) -> MusicBrainzSource:
        assert capabilities == frozenset({Capability.RECENT_RELEASES}), (
            "this factory is scoped to the RECENT_RELEASES-only contract"
        )
        self._transport = _FakeTransport()
        self._transport.scenario = scenario
        if raw_text is not None:
            self._transport.text = raw_text
        return MusicBrainzSource(transport=self._transport, clock=lambda: FIXED_OBSERVED_AT)  # type: ignore[arg-type]

    def transport_calls(self) -> tuple[TransportCall, ...]:
        return tuple(self._transport.calls)

    def example_artist_reference(self) -> SourceReference:
        return SourceReference(
            source="musicbrainz",
            native_id=_MBID,
            canonical_url=f"https://musicbrainz.org/artist/{_MBID}",
            observed_at=FIXED_OBSERVED_AT,
        )


def test_recent_releases_returns_canonical_result_and_exact_trace() -> None:
    assert_operation_contract(MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE)


def test_recent_releases_records_have_source_and_observation_time() -> None:
    assert_records_have_provenance(MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE)


def test_recent_releases_title_is_sanitized() -> None:
    assert_source_text_is_sanitized(
        MusicBrainzSourceFactory(), DEFAULT_PROVIDER_TEXT, _RECENT_RELEASES_CASE
    )


def test_adversarial_title_text_remains_data() -> None:
    assert_adversarial_text_remains_data(
        MusicBrainzSourceFactory(),
        "\x1b]8;;https://evil.example\x07click here\x1b]8;;\x07",
        ("\x1b", "\x07"),
        _RECENT_RELEASES_CASE,
    )


def test_malformed_recent_releases_response_maps_to_invalid_source_response() -> None:
    assert_malformed_maps_exactly(MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE)


def test_timeout_maps_to_source_unavailable_and_stays_redacted() -> None:
    assert_error_maps_exactly(
        MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE, "timeout", SourceUnavailableError
    )


def test_rate_limited_maps_exactly_and_stays_redacted() -> None:
    assert_error_maps_exactly(
        MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE, "rate_limited", RateLimitedError
    )


def test_retry_timing_clamps_to_nine_hundred_seconds() -> None:
    assert_retry_is_clamped(MusicBrainzSourceFactory(), _RECENT_RELEASES_CASE)
