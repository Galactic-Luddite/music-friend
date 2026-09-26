from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    IdentityConfidence,
    Release,
    ReleaseCandidateKind,
    ReleaseDatePrecision,
    ReleaseDiscoveryStatus,
    SourceReference,
)
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
RAW_ERROR_CANARY = "raw-release-provider-canary"


def _artist(native_id: str, name: str | None = None) -> Artist:
    return Artist(
        f"artist:{native_id}",
        name or native_id,
        (SourceReference("spotify", native_id, None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _release(
    native_id: str,
    artist: Artist,
    *,
    title: str = "Release",
    release_date: date = date(2026, 8, 31),
    release_type: str = "album",
) -> Release:
    return Release(
        f"release:{native_id}",
        title,
        release_type,
        release_date,
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (
            SourceReference(
                "spotify", native_id, f"https://open.spotify.com/album/{native_id}", NOW
            ),
        ),
        NOW,
    )


class FakeReleaseSource:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, str | None], Page[Release] | Exception] = {}
        self.calls: list[tuple[str, datetime, str | None]] = []

    def capabilities(self) -> ProviderCapabilities:
        capabilities = frozenset(Capability)
        return ProviderCapabilities(capabilities, capabilities)

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self.capabilities())

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        raise AssertionError("unused")

    def saved_items(self, cursor: str | None = None) -> object:
        raise AssertionError("unused")

    def top_items(self, time_range: str, limit: int) -> object:
        raise AssertionError("unused")

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]:
        assert len(artist_refs) == 1
        native_id = artist_refs[0].native_id
        self.calls.append((native_id, since, cursor))
        response = self.pages[(native_id, cursor)]
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


def test_release_discovery_uses_first_and_overlapping_subsequent_windows(
    tmp_path: Path,
) -> None:
    """Catches a global cursor, wrong first window, or a missing overlap."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one")
        _watch(application, artist)
        source = FakeReleaseSource()
        source.pages[("one", None)] = Page((_release("one-release", artist),), None)

        first = application.discover_releases("spotify", source, checked_at=NOW)
        second_time = NOW + timedelta(days=1)
        second = application.discover_releases("spotify", source, checked_at=second_time)

        assert first.artists[0].status is ReleaseDiscoveryStatus.SUCCESS
        assert source.calls == [
            ("one", NOW - timedelta(days=30), None),
            ("one", NOW - timedelta(hours=48), None),
        ]
        assert second.artists[0].candidates == ()
        cursor = catalog.get_release_check_cursor("spotify", artist.local_id)
        assert cursor is not None
        assert cursor.last_successful_at == second_time


def test_release_discovery_resumes_after_one_hundred_and_reaches_record_one_hundred_one(
    tmp_path: Path,
) -> None:
    """Catches a partial release cursor being dropped and restarting the same first pages."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("prolific")
        _watch(application, artist)
        source = FakeReleaseSource()
        first_page = tuple(
            _release(f"first-{index}", artist, title=f"First {index}") for index in range(50)
        )
        second_page = tuple(
            _release(f"second-{index}", artist, title=f"Second {index}") for index in range(50)
        )
        final_release = _release("record-101", artist, title="Record 101")
        source.pages[("prolific", None)] = Page(first_page, "next-page")
        source.pages[("prolific", "next-page")] = Page(second_page, "must-not-fetch")
        source.pages[("prolific", "must-not-fetch")] = Page((final_release,), None)

        first = application.discover_releases("spotify", source, checked_at=NOW)

        first_result = first.artists[0]
        assert first_result.status is ReleaseDiscoveryStatus.PARTIAL
        assert first_result.records_seen == 100
        assert first_result.continuation == "must-not-fetch"
        assert len(first_result.candidates) == 100
        assert catalog.get_release_check_cursor("spotify", artist.local_id) is None
        continuation = catalog.get_release_check_continuation("spotify", artist.local_id)
        assert continuation is not None
        assert continuation.cursor == "must-not-fetch"
        source.pages[("prolific", "must-not-fetch")] = RuntimeError(RAW_ERROR_CANARY)
        failed = application.discover_releases(
            "spotify", source, checked_at=NOW + timedelta(hours=12)
        )
        assert failed.artists[0].status is ReleaseDiscoveryStatus.FAILED
        retained = catalog.get_release_check_continuation("spotify", artist.local_id)
        assert retained == continuation
        assert RAW_ERROR_CANARY not in repr(failed)
        source.pages[("prolific", "must-not-fetch")] = Page((final_release,), None)
        second = application.discover_releases(
            "spotify", source, checked_at=NOW + timedelta(days=1)
        )
        assert second.artists[0].status is ReleaseDiscoveryStatus.SUCCESS
        assert [candidate.release.local_id for candidate in second.artists[0].candidates] == [
            final_release.local_id
        ]
        assert [call[2] for call in source.calls] == [
            None,
            "next-page",
            "must-not-fetch",
            "must-not-fetch",
        ]
        assert catalog.get_release_check_continuation("spotify", artist.local_id) is None
        completed_cursor = catalog.get_release_check_cursor("spotify", artist.local_id)
        assert completed_cursor is not None
        assert completed_cursor.last_successful_at == NOW + timedelta(days=1)


def test_release_discovery_deduplicates_provider_and_obvious_title_date_variants(
    tmp_path: Path,
) -> None:
    """Catches duplicate inbox candidates for the same release under provider variants."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("duplicate")
        _watch(application, artist)
        source = FakeReleaseSource()
        original = _release("release-a", artist, title="Same  Title")
        source.pages[("duplicate", None)] = Page(
            (original, original, _release("release-b", artist, title="same title")),
            None,
        )

        result = application.discover_releases("spotify", source, checked_at=NOW)

        artist_result = result.artists[0]
        assert artist_result.records_seen == 3
        assert [candidate.release.local_id for candidate in artist_result.candidates] == [
            original.local_id
        ]
        assert catalog.get_release("release:release-a") is not None
        assert catalog.get_release("release:release-b") is None


def test_release_discovery_emits_new_then_updated_then_nothing_for_an_idempotent_rerun(
    tmp_path: Path,
) -> None:
    """Catches treating all rechecks as new or failing to surface a material change."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("changed")
        _watch(application, artist)
        source = FakeReleaseSource()
        source.pages[("changed", None)] = Page(
            (_release("release", artist, title="Original"),), None
        )

        first = application.discover_releases("spotify", source, checked_at=NOW)
        source.pages[("changed", None)] = Page(
            (_release("release", artist, title="Revised"),), None
        )
        second = application.discover_releases(
            "spotify", source, checked_at=NOW + timedelta(days=1)
        )
        third = application.discover_releases("spotify", source, checked_at=NOW + timedelta(days=2))

        assert first.artists[0].candidates[0].kind is ReleaseCandidateKind.NEW
        assert second.artists[0].candidates[0].kind is ReleaseCandidateKind.UPDATED
        assert third.artists[0].candidates == ()
        state = catalog.get_release_discovery("release:release")
        assert state is not None
        assert state.first_seen_at == NOW
        assert state.last_seen_at == NOW + timedelta(days=2)


def test_release_discovery_isolates_artist_failures_and_preserves_prior_cursor(
    tmp_path: Path,
) -> None:
    """Catches one artist failure erasing saved state or leaking source diagnostics."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        failed_artist = _artist("failed")
        successful_artist = _artist("successful")
        _watch(application, failed_artist)
        _watch(application, successful_artist)
        source = FakeReleaseSource()
        source.pages[("failed", None)] = Page((_release("old", failed_artist),), None)
        source.pages[("successful", None)] = Page((), None)
        application.discover_releases("spotify", source, checked_at=NOW)
        before = catalog.get_release_check_cursor("spotify", failed_artist.local_id)
        assert before is not None
        source.pages[("failed", None)] = RuntimeError(RAW_ERROR_CANARY)
        source.pages[("successful", None)] = Page((_release("fresh", successful_artist),), None)

        result = application.discover_releases(
            "spotify", source, checked_at=NOW + timedelta(days=1)
        )

        by_artist = {item.artist_local_id: item for item in result.artists}
        assert by_artist[failed_artist.local_id].status is ReleaseDiscoveryStatus.FAILED
        assert by_artist[failed_artist.local_id].candidates == ()
        assert by_artist[successful_artist.local_id].status is ReleaseDiscoveryStatus.SUCCESS
        assert catalog.get_release_check_cursor("spotify", failed_artist.local_id) == before
        assert catalog.get_release("release:fresh") is not None
        assert RAW_ERROR_CANARY not in repr(result)


def test_release_discovery_rejects_non_release_pages_without_persisting_partial_state(
    tmp_path: Path,
) -> None:
    """Catches malformed source output being persisted or exposed as a diagnostic."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("invalid")
        _watch(application, artist)
        source = FakeReleaseSource()
        source.pages[("invalid", None)] = Page((object(),), None)  # type: ignore[arg-type]

        result = application.discover_releases("spotify", source, checked_at=NOW)

        assert result.artists[0].status is ReleaseDiscoveryStatus.FAILED
        assert result.artists[0].records_seen == 0
        assert catalog.get_release_check_cursor("spotify", artist.local_id) is None
        assert RAW_ERROR_CANARY not in repr(result)


def test_discover_releases_rejects_a_malformed_source_name(tmp_path: Path) -> None:
    """Catches an unbounded or malformed source name reaching provider dispatch."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        with pytest.raises(ValueError, match="source_name"):
            application.discover_releases("", FakeReleaseSource(), checked_at=NOW)
        with pytest.raises(ValueError, match="source_name"):
            application.discover_releases("has spaces", FakeReleaseSource(), checked_at=NOW)


def test_discover_releases_rejects_a_source_missing_the_provider_contract(tmp_path: Path) -> None:
    """Catches a non-conforming source object reaching provider dispatch."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        with pytest.raises(ValueError, match="source"):
            application.discover_releases(
                "spotify",
                object(),
                checked_at=NOW,  # type: ignore[arg-type]
            )


def test_discover_releases_treats_a_malformed_release_page_as_an_artist_failure(
    tmp_path: Path,
) -> None:
    """Catches a source returning something other than a Page crashing the whole refresh."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one", "One")
        _watch(application, artist)
        source = FakeReleaseSource()
        source.pages[("one", None)] = "not-a-page"  # type: ignore[assignment]

        result = application.discover_releases("spotify", source, checked_at=NOW)

        assert result.artists[0].status is ReleaseDiscoveryStatus.FAILED


def _multi_source_artist(mb_id: str, deezer_id: str, name: str = "Artist") -> Artist:
    return Artist(
        f"artist:{mb_id}",
        name,
        (
            SourceReference("musicbrainz", mb_id, None, NOW),
            SourceReference("deezer", deezer_id, None, NOW),
        ),
        IdentityConfidence.EXTERNAL_ID,
        NOW,
    )


def _cross_source_release(
    native_id: str,
    source: str,
    artist: Artist,
    *,
    title: str = "Shared Release",
    release_date: date = date(2026, 8, 1),
) -> Release:
    return Release(
        f"release:{source}:{native_id}",
        title,
        "album",
        release_date,
        ReleaseDatePrecision.DAY,
        (artist.local_id,),
        (SourceReference(source, native_id, f"https://example.test/{source}/{native_id}", NOW),),
        NOW,
    )


def test_cross_source_release_discovery_merges_into_one_release_with_two_source_refs(
    tmp_path: Path,
) -> None:
    """AC: the same synthetic release discovered via MusicBrainz and Deezer
    produces one release, two source refs, one inbox item."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _multi_source_artist("mb-shared", "deezer-shared")
        _watch(application, artist)

        mb_source = FakeReleaseSource()
        mb_source.pages[("mb-shared", None)] = Page(
            (_cross_source_release("rg-shared", "musicbrainz", artist),), None
        )
        first = application.discover_releases("musicbrainz", mb_source, checked_at=NOW)
        assert len(first.artists[0].candidates) == 1
        assert first.artists[0].candidates[0].kind is ReleaseCandidateKind.NEW

        deezer_source = FakeReleaseSource()
        deezer_source.pages[("deezer-shared", None)] = Page(
            (_cross_source_release("al-shared", "deezer", artist),), None
        )
        second = application.discover_releases(
            "deezer", deezer_source, checked_at=NOW + timedelta(hours=1)
        )
        # A cross-source match creates no new inbox candidate.
        assert second.artists[0].candidates == ()

        stored_release = application.get_release("release:musicbrainz:rg-shared")
        assert stored_release is not None
        assert len(stored_release.source_refs) == 2
        assert {ref.source for ref in stored_release.source_refs} == {"musicbrainz", "deezer"}

        # No second release was created for the Deezer discovery.
        assert application.get_release("release:deezer:al-shared") is None


def test_cross_source_dedupe_tolerates_a_one_day_date_offset(tmp_path: Path) -> None:
    """AC: a one-day date-precision offset still matches."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _multi_source_artist("mb-offset", "deezer-offset")
        _watch(application, artist)

        mb_source = FakeReleaseSource()
        mb_source.pages[("mb-offset", None)] = Page(
            (
                _cross_source_release(
                    "rg-offset", "musicbrainz", artist, release_date=date(2026, 8, 1)
                ),
            ),
            None,
        )
        application.discover_releases("musicbrainz", mb_source, checked_at=NOW)

        deezer_source = FakeReleaseSource()
        deezer_source.pages[("deezer-offset", None)] = Page(
            (_cross_source_release("al-offset", "deezer", artist, release_date=date(2026, 8, 2)),),
            None,
        )
        second = application.discover_releases(
            "deezer", deezer_source, checked_at=NOW + timedelta(hours=1)
        )
        assert second.artists[0].candidates == ()

        stored_release = application.get_release("release:musicbrainz:rg-offset")
        assert stored_release is not None
        assert len(stored_release.source_refs) == 2
        assert application.get_release("release:deezer:al-offset") is None


def test_cross_source_dedupe_does_not_match_a_different_title(tmp_path: Path) -> None:
    """AC: different titles do not match."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _multi_source_artist("mb-distinct", "deezer-distinct")
        _watch(application, artist)

        mb_source = FakeReleaseSource()
        mb_source.pages[("mb-distinct", None)] = Page(
            (_cross_source_release("rg-distinct", "musicbrainz", artist, title="Album One"),),
            None,
        )
        application.discover_releases("musicbrainz", mb_source, checked_at=NOW)

        deezer_source = FakeReleaseSource()
        deezer_source.pages[("deezer-distinct", None)] = Page(
            (
                _cross_source_release(
                    "al-distinct",
                    "deezer",
                    artist,
                    title="A Completely Different Album",
                ),
            ),
            None,
        )
        second = application.discover_releases(
            "deezer", deezer_source, checked_at=NOW + timedelta(hours=1)
        )
        # Different titles do not match: the second discovery is its own NEW candidate.
        assert len(second.artists[0].candidates) == 1
        assert second.artists[0].candidates[0].kind is ReleaseCandidateKind.NEW

        mb_release = application.get_release("release:musicbrainz:rg-distinct")
        deezer_release = application.get_release("release:deezer:al-distinct")
        assert mb_release is not None
        assert deezer_release is not None
        assert len(mb_release.source_refs) == 1
        assert len(deezer_release.source_refs) == 1
