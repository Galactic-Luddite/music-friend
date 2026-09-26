from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    CatalogItem,
    CatalogItemBatch,
    CatalogSyncResult,
    IdentityConfidence,
    SourceCapability,
    SourceReference,
    SyncCapabilityResult,
    SyncCapabilityStatus,
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
from music_friend.tools import catalog_sync as sync_module

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
RAW_ERROR_CANARY = "raw-provider-body-canary"


def _artist(native_id: str, name: str | None = None) -> Artist:
    return Artist(
        f"artist:{native_id}",
        name or native_id,
        (SourceReference("spotify", native_id, None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _track(native_id: str, *artists: Artist) -> CatalogItem:
    return CatalogItem(
        "track",
        f"track:{native_id}",
        native_id,
        tuple(artist.local_id for artist in artists),
        (SourceReference("spotify", native_id, None, NOW),),
        NOW,
    )


class FakeCatalogSource:
    def __init__(self) -> None:
        followed_one = _artist("followed-1")
        followed_two = _artist("followed-2")
        saved_one = _artist("saved-1")
        saved_two = _artist("saved-2")
        self.followed_pages = {
            None: Page((followed_one,), "followed-next"),
            "followed-next": Page((followed_two,), None),
        }
        self.saved_pages = {
            None: CatalogItemBatch(
                (_track("track-1", saved_one, saved_two),),
                (saved_one, saved_two),
                "saved-next",
            ),
            "saved-next": CatalogItemBatch(
                (_track("track-2", saved_one),),
                (saved_one,),
                None,
            ),
        }
        self.top_pages = {
            "short_term": Page((_artist("top-short"), _artist("shared")), None),
            "medium_term": Page((_artist("shared"),), None),
            "long_term": Page((_artist("top-long"),), None),
        }
        self.failures: set[str] = set()
        self.top_calls: list[tuple[str, int]] = []

    def capabilities(self) -> ProviderCapabilities:
        capabilities = frozenset(Capability)
        return ProviderCapabilities(capabilities, capabilities)

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self.capabilities())

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        self._fail("followed")
        return self.followed_pages[cursor]

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        self._fail("saved")
        return self.saved_pages[cursor]

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        raise AssertionError("unused")

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        self.top_calls.append((time_range, limit))
        self._fail(f"top:{time_range}")
        return self.top_pages[time_range]

    def recent_releases(
        self, artist_refs: Sequence[SourceReference], since: datetime
    ) -> Page[object]:
        raise AssertionError("unused")

    def _fail(self, operation: str) -> None:
        if operation in self.failures:
            raise RuntimeError(RAW_ERROR_CANARY)


def _result_map(result: CatalogSyncResult) -> dict[SourceCapability, SyncCapabilityResult]:
    return {item.capability: item for item in result.capabilities}


def _saved_page(native_id: str, cursor: str | None) -> CatalogItemBatch:
    artist = _artist(native_id)
    return CatalogItemBatch((_track(f"track-{native_id}", artist),), (artist,), cursor)


def test_catalog_sync_paginates_all_evidence_and_requests_each_top_range_at_fifty(
    tmp_path: Path,
) -> None:
    """Catches first-page-only imports, primary-credit-only saves, and wrong top limits."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()

        first = application.synchronize_catalog("spotify", source)
        first_ids = tuple(
            evidence.local_id
            for evidence in catalog.list_affinity_evidence("artist:saved-1", limit=20)
        )
        second = application.synchronize_catalog("spotify", source)

        assert source.top_calls == [
            ("short_term", 50),
            ("medium_term", 50),
            ("long_term", 50),
            ("short_term", 50),
            ("medium_term", 50),
            ("long_term", 50),
        ]
        results = _result_map(first)
        assert all(result.status is SyncCapabilityStatus.SUCCESS for result in results.values())
        assert results[SourceCapability.FOLLOWED_ARTISTS].pages_seen == 2
        assert results[SourceCapability.FOLLOWED_ARTISTS].evidence_count == 2
        assert results[SourceCapability.SAVED_ITEMS].pages_seen == 2
        assert results[SourceCapability.SAVED_ITEMS].evidence_count == 3
        assert tuple(
            evidence.evidence_key
            for evidence in catalog.list_affinity_evidence("artist:saved-1", limit=20)
        ) == ("track-1", "track-2")
        assert tuple(
            evidence.evidence_key
            for evidence in catalog.list_affinity_evidence("artist:saved-2", limit=20)
        ) == ("track-1",)
        assert catalog.get_affinity_score("artist:shared").top_short_term_rank == 2
        assert catalog.get_affinity_score("artist:shared").top_medium_term_rank == 1
        assert (
            tuple(
                evidence.local_id
                for evidence in catalog.list_affinity_evidence("artist:saved-1", limit=20)
            )
            == first_ids
        )
        assert second == first


def test_catalog_sync_failure_preserves_only_that_capability_and_returns_no_raw_error(
    tmp_path: Path,
) -> None:
    """Catches cross-capability rollback, destructive failure replacement, and error leakage."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        application.synchronize_catalog("spotify", source)
        saved_before = catalog.list_affinity_evidence("artist:saved-1", limit=20)
        medium_before = catalog.list_affinity_evidence("artist:shared", limit=20)
        source.failures = {"saved", "top:medium_term"}
        source.top_pages["short_term"] = Page((_artist("replacement-short"),), None)

        result = application.synchronize_catalog("spotify", source)

        results = _result_map(result)
        assert results[SourceCapability.SAVED_ITEMS].status is SyncCapabilityStatus.FAILED
        assert (
            results[SourceCapability.TOP_ARTISTS_MEDIUM_TERM].status is SyncCapabilityStatus.FAILED
        )
        assert (
            results[SourceCapability.TOP_ARTISTS_SHORT_TERM].status is SyncCapabilityStatus.SUCCESS
        )
        assert catalog.list_affinity_evidence("artist:saved-1", limit=20) == saved_before
        assert any(
            evidence.kind is AffinityEvidenceKind.TOP_MEDIUM_TERM
            for evidence in catalog.list_affinity_evidence("artist:shared", limit=20)
        )
        assert catalog.get_affinity_score("artist:replacement-short").top_short_term_rank == 1
        assert catalog.get_affinity_score("artist:top-short").top_short_term_rank is None
        assert medium_before
        assert RAW_ERROR_CANARY not in repr(result)


@pytest.mark.parametrize("operation", ("followed", "saved"))
@pytest.mark.parametrize(
    "continuations",
    (("same", "same"), ("cursor-a", "cursor-b", "cursor-a")),
    ids=("repeated-cursor", "multi-node-cycle"),
)
def test_catalog_sync_rejects_cursor_cycles_without_persisting_partial_capability(
    tmp_path: Path,
    operation: str,
    continuations: tuple[str, ...],
) -> None:
    """Catches cursor cycles that grow state forever or persist their partial records."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        page_inputs: tuple[str | None, ...] = (None, *continuations[:-1])
        native_ids = tuple(f"cycle-{operation}-{index}" for index in range(len(continuations)))
        if operation == "followed":
            source.followed_pages = {
                cursor: Page((_artist(native_id),), next_cursor)
                for cursor, native_id, next_cursor in zip(
                    page_inputs, native_ids, continuations, strict=True
                )
            }
            capability = SourceCapability.FOLLOWED_ARTISTS
        else:
            source.saved_pages = {
                cursor: _saved_page(native_id, next_cursor)
                for cursor, native_id, next_cursor in zip(
                    page_inputs, native_ids, continuations, strict=True
                )
            }
            capability = SourceCapability.SAVED_ITEMS

        result = application.synchronize_catalog("spotify", source)

        failure = _result_map(result)[capability]
        assert failure.status is SyncCapabilityStatus.FAILED
        assert failure.pages_seen == len(continuations)
        assert failure.artists_seen == len(continuations)
        assert failure.evidence_count == len(continuations)
        assert all(catalog.get_artist(f"artist:{native_id}") is None for native_id in native_ids)
        assert RAW_ERROR_CANARY not in repr(result)


def test_followed_sync_enforces_distinct_artist_and_evidence_caps_during_accumulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches followed dictionaries growing one record beyond their safety bound."""
    monkeypatch.setattr(sync_module, "_MAX_SYNC_COUNT", 2)
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        overflow = tuple(_artist(f"followed-overflow-{index}") for index in range(3))
        source.followed_pages = {None: Page(overflow, None)}

        result = application.synchronize_catalog("spotify", source)

        failure = _result_map(result)[SourceCapability.FOLLOWED_ARTISTS]
        assert failure.status is SyncCapabilityStatus.FAILED
        assert failure.pages_seen == 1
        assert failure.artists_seen == 2
        assert failure.evidence_count == 2
        assert all(catalog.get_artist(artist.local_id) is None for artist in overflow)
        assert (
            max(
                failure.pages_seen,
                failure.artists_seen,
                failure.evidence_count,
            )
            <= 2
        )
        assert RAW_ERROR_CANARY not in repr(result)


def test_saved_sync_enforces_evidence_cap_during_credited_artist_fanout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches saved-track credit fanout inserting evidence beyond the safety bound."""
    monkeypatch.setattr(sync_module, "_MAX_SYNC_COUNT", 2)
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        first = _artist("fanout-1")
        second = _artist("fanout-2")
        source.saved_pages = {
            None: CatalogItemBatch(
                (
                    _track("fanout-track-1", first, second),
                    _track("fanout-track-2", first, second),
                ),
                (first, second),
                None,
            )
        }

        result = application.synchronize_catalog("spotify", source)

        failure = _result_map(result)[SourceCapability.SAVED_ITEMS]
        assert failure.status is SyncCapabilityStatus.FAILED
        assert failure.pages_seen == 1
        assert failure.artists_seen == 2
        assert failure.evidence_count == 2
        assert catalog.get_artist(first.local_id) is None
        assert catalog.get_artist(second.local_id) is None
        assert (
            max(
                failure.pages_seen,
                failure.artists_seen,
                failure.evidence_count,
            )
            <= 2
        )
        assert RAW_ERROR_CANARY not in repr(result)


def test_capability_transaction_rolls_back_inserted_artists_and_preserves_prior_state(
    tmp_path: Path,
) -> None:
    """Catches evidence-write failure committing prerequisite artists or deleting prior facts."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        prior = _artist("prior-followed", "Prior Name")
        catalog.put_artist(prior)
        prior_evidence = AffinityEvidence(
            "prior-evidence",
            prior.local_id,
            "spotify",
            AffinityEvidenceKind.FOLLOWED,
            "prior-followed",
            None,
            NOW,
        )
        catalog.put_affinity_evidence(prior_evidence)
        connection = catalog._connection
        assert connection is not None
        connection.execute(
            """
            CREATE TRIGGER reject_followed_evidence
            BEFORE INSERT ON affinity_evidence
            WHEN NEW.source = 'spotify' AND NEW.kind = 'followed'
            BEGIN
                SELECT RAISE(ABORT, 'raw-provider-body-canary');
            END
            """
        )
        source = FakeCatalogSource()
        replacement = _artist("prior-followed", "Replacement Name")
        rolled_back = _artist("rolled-back-followed")
        source.followed_pages = {None: Page((replacement, rolled_back), None)}
        committed = _artist("committed-saved")
        source.saved_pages = {
            None: CatalogItemBatch(
                (_track("committed-track", committed),),
                (committed,),
                None,
            )
        }

        result = application.synchronize_catalog("spotify", source)

        results = _result_map(result)
        assert results[SourceCapability.FOLLOWED_ARTISTS].status is SyncCapabilityStatus.FAILED
        assert results[SourceCapability.SAVED_ITEMS].status is SyncCapabilityStatus.SUCCESS
        assert catalog.get_artist(prior.local_id) == prior
        assert catalog.get_artist(rolled_back.local_id) is None
        assert catalog.get_affinity_evidence(prior_evidence.local_id) == prior_evidence
        assert catalog.get_artist(committed.local_id) == committed
        assert catalog.list_affinity_evidence(committed.local_id, limit=10)
        assert RAW_ERROR_CANARY not in repr(result)


def test_synchronize_catalog_rejects_invalid_inputs(tmp_path: Path) -> None:
    """Catches a malformed catalog, source name, or source reaching provider dispatch."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        with pytest.raises(ValueError, match="catalog"):
            sync_module.synchronize_catalog(
                object(),
                "spotify",
                FakeCatalogSource(),  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="source_name"):
            sync_module.synchronize_catalog(catalog, "has spaces", FakeCatalogSource())
        with pytest.raises(ValueError, match="source"):
            sync_module.synchronize_catalog(
                catalog,
                "spotify",
                object(),  # type: ignore[arg-type]
            )


def test_catalog_freshness_ttl_stays_below_the_scheduled_refresh_interval() -> None:
    """Catalog TTL is derived from DAILY_REFRESH_MINUTES and stays below it."""
    from datetime import timedelta

    from music_friend.domain import DAILY_REFRESH_MINUTES

    catalog_ttl = sync_module.FRESHNESS_TTL
    refresh_interval = timedelta(minutes=DAILY_REFRESH_MINUTES)

    # TTL must be less than the refresh interval
    assert catalog_ttl < refresh_interval
    # TTL should be refresh_interval minus 4 hours
    expected = refresh_interval - timedelta(hours=4)
    assert catalog_ttl == expected


def test_two_consecutive_syncs_within_ttl_skips_fresh(tmp_path: Path) -> None:
    """A second sync within TTL skips all capabilities and reports skipped_fresh."""
    from datetime import timedelta

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        checked_at = NOW

        # First sync: full run
        result1 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at, force=False
        )
        results1 = _result_map(result1)

        # All capabilities should be SUCCESS on first run
        for status in results1.values():
            assert status.status is SyncCapabilityStatus.SUCCESS
            assert status.pages_seen > 0

        # Second sync: shortly after the first one (within TTL)
        checked_at2 = checked_at + timedelta(minutes=5)
        result2 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at2, force=False
        )
        results2 = _result_map(result2)

        # All capabilities should be SKIPPED_FRESH and make zero requests
        for capability, status in results2.items():
            assert status.status is SyncCapabilityStatus.SKIPPED_FRESH
            assert status.pages_seen == 0
            assert status.artists_seen == 0
            assert status.evidence_count == 0


def test_failed_run_does_not_mark_capability_fresh(tmp_path: Path) -> None:
    """A failed capability does not update last_successful_at."""
    from datetime import timedelta

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        checked_at = NOW

        # First sync: mark followed_artists as failed
        source.failures.add("followed")
        result1 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at, force=False
        )
        results1 = _result_map(result1)
        assert results1[SourceCapability.FOLLOWED_ARTISTS].status is SyncCapabilityStatus.FAILED

        # Second sync: shortly after (within TTL), but followed should not be skipped
        # because it never completed successfully
        source.failures.clear()
        checked_at2 = checked_at + timedelta(minutes=5)
        result2 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at2, force=False
        )
        results2 = _result_map(result2)

        # FOLLOWED_ARTISTS should retry (SUCCESS), not skip
        assert results2[SourceCapability.FOLLOWED_ARTISTS].status is SyncCapabilityStatus.SUCCESS
        assert results2[SourceCapability.FOLLOWED_ARTISTS].pages_seen > 0


def test_force_bypasses_freshness_check(tmp_path: Path) -> None:
    """With force=True, all capabilities refresh fully even within TTL."""
    from datetime import timedelta

    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        source = FakeCatalogSource()
        checked_at = NOW

        # First sync: full run
        result1 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at, force=False
        )
        results1 = _result_map(result1)

        for status in results1.values():
            assert status.status is SyncCapabilityStatus.SUCCESS

        # Second sync: shortly after, but with force=True
        checked_at2 = checked_at + timedelta(minutes=5)
        result2 = application.synchronize_catalog(
            "spotify", source, checked_at=checked_at2, force=True
        )
        results2 = _result_map(result2)

        # All capabilities should be SUCCESS and make requests
        for capability, status in results2.items():
            assert status.status is SyncCapabilityStatus.SUCCESS
            assert status.pages_seen > 0
