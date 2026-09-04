from __future__ import annotations

import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    Artist,
    Event,
    IdentityConfidence,
    Interest,
    InterestKind,
    InterestStatus,
    Observation,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.store.portable import PurgeResult, delete_catalog, purge_source

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _seed_two_sources(catalog: Catalog) -> None:
    catalog.put_artist(
        Artist(
            local_id="artist-1",
            display_name="Artist",
            source_refs=(
                SourceReference("remove", "one", None, NOW),
                SourceReference("keep", "one", None, NOW),
            ),
            identity_confidence=IdentityConfidence.EXTERNAL_ID,
            observed_at=NOW,
        )
    )
    catalog.put_interest(
        Interest(
            local_id="interest-1",
            kind=InterestKind.ARTIST,
            target_local_id="artist-1",
            status=InterestStatus.ACTIVE,
            created_by="user",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for source in ("remove", "keep"):
        catalog.put_observation(
            Observation(
                local_id=f"observation-{source}",
                source=source,
                native_id="one",
                record_kind="artist",
                record_local_id="artist-1",
                fact_name="following",
                observed_at=NOW,
            )
        )
        catalog.set_check_time(source, NOW)


def test_purge_source_removes_only_source_owned_state_and_reports_exact_counts(
    catalog: Catalog,
) -> None:
    _seed_two_sources(catalog)

    result = purge_source(catalog, "remove")

    assert result == PurgeResult(mapping_count=1, observation_count=1, check_time_count=1)
    artist = catalog.get_artist("artist-1")
    assert artist is not None
    assert [(item.source, item.native_id) for item in artist.source_refs] == [("keep", "one")]
    assert catalog.get_interest("interest-1") is not None
    assert catalog.get_observation("observation-remove") is None
    assert catalog.get_observation("observation-keep") is not None
    assert catalog.get_check_time("remove") is None
    assert catalog.get_check_time("keep") == NOW


def test_purge_source_with_no_state_reports_zeroes(catalog: Catalog) -> None:
    assert purge_source(catalog, "absent") == PurgeResult(0, 0, 0)


def test_purge_final_source_retains_interested_artist_with_empty_provenance(
    catalog: Catalog,
) -> None:
    _seed_two_sources(catalog)
    purge_source(catalog, "remove")

    result = purge_source(catalog, "keep")

    assert result == PurgeResult(1, 1, 1)
    artist = catalog.get_artist("artist-1")
    assert artist is not None
    assert artist.source_refs == ()
    assert catalog.get_interest("interest-1") is not None


def test_purge_final_source_deletes_uninterested_canonical_record(catalog: Catalog) -> None:
    catalog.put_artist(
        Artist(
            local_id="orphan",
            display_name="Orphan",
            source_refs=(SourceReference("remove", "orphan", None, NOW),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=NOW,
        )
    )

    purge_source(catalog, "remove")

    assert catalog.get_artist("orphan") is None


def test_purge_retains_sourceless_artist_referenced_by_retained_release(
    catalog: Catalog,
) -> None:
    catalog.put_artist(
        Artist(
            local_id="artist-root",
            display_name="Artist Root",
            source_refs=(SourceReference("remove", "artist", None, NOW),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=NOW,
        )
    )
    catalog.put_release(
        Release(
            local_id="retained-release",
            title="Retained Release",
            release_type="album",
            release_date=date(2026, 1, 1),
            date_precision=ReleaseDatePrecision.YEAR,
            artist_refs=("artist-root",),
            source_refs=(SourceReference("keep", "release", None, NOW),),
            observed_at=NOW,
        )
    )

    purge_source(catalog, "remove")

    artist = catalog.get_artist("artist-root")
    assert artist is not None
    assert artist.source_refs == ()
    assert catalog.get_release("retained-release") is not None


def test_purge_retains_sourceless_artist_referenced_by_retained_event(
    catalog: Catalog,
) -> None:
    catalog.put_artist(
        Artist(
            local_id="event-artist-root",
            display_name="Event Artist Root",
            source_refs=(SourceReference("remove", "artist-event", None, NOW),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=NOW,
        )
    )
    catalog.put_event(
        Event(
            local_id="retained-event",
            title="Retained Event",
            artist_refs=("event-artist-root",),
            venue_name=None,
            locality=None,
            starts_at=None,
            time_precision=None,
            source_links=(),
            source_refs=(SourceReference("keep", "retained-event", None, NOW),),
            observed_at=NOW,
        )
    )

    purge_source(catalog, "remove")

    artist = catalog.get_artist("event-artist-root")
    assert artist is not None
    assert artist.source_refs == ()
    assert catalog.get_event("retained-event") is not None


def test_purge_deletes_unrooted_release_and_event_before_their_artist(
    catalog: Catalog,
) -> None:
    catalog.put_artist(
        Artist(
            local_id="shared-artist",
            display_name="Shared Artist",
            source_refs=(SourceReference("keep", "artist", None, NOW),),
            identity_confidence=IdentityConfidence.SOURCE_ONLY,
            observed_at=NOW,
        )
    )
    catalog.put_release(
        Release(
            local_id="orphan-release",
            title="Orphan Release",
            release_type="album",
            release_date=date(2026, 1, 1),
            date_precision=ReleaseDatePrecision.YEAR,
            artist_refs=("shared-artist",),
            source_refs=(SourceReference("remove", "release", None, NOW),),
            observed_at=NOW,
        )
    )
    catalog.put_event(
        Event(
            local_id="orphan-event",
            title="Orphan Event",
            artist_refs=("shared-artist",),
            venue_name=None,
            locality=None,
            starts_at=None,
            time_precision=None,
            source_links=(),
            source_refs=(SourceReference("remove", "event", None, NOW),),
            observed_at=NOW,
        )
    )

    purge_source(catalog, "remove")

    assert catalog.get_release("orphan-release") is None
    assert catalog.get_event("orphan-event") is None
    assert catalog.get_artist("shared-artist") is not None


def test_delete_catalog_closes_and_removes_only_catalog_and_sidecars(
    catalog: Catalog, catalog_path: Path
) -> None:
    export = catalog_path.parent / "user-export.json"
    export.write_text("keep", encoding="utf-8")

    delete_catalog(catalog)

    assert not catalog_path.exists()
    assert not Path(f"{catalog_path}-wal").exists()
    assert not Path(f"{catalog_path}-shm").exists()
    assert export.read_text(encoding="utf-8") == "keep"
    assert catalog_path.parent.exists()
    with pytest.raises(CatalogUnavailableError):
        catalog.get_artist("anything")


def test_delete_catalog_preserves_existing_parent_mode(
    catalog: Catalog, catalog_path: Path
) -> None:
    os.chmod(catalog_path.parent, 0o750)

    delete_catalog(catalog)

    assert stat.S_IMODE(catalog_path.parent.stat().st_mode) == 0o750


def test_delete_catalog_never_recreates_missing_parent(
    catalog: Catalog, catalog_path: Path
) -> None:
    original_parent = catalog_path.parent
    moved_parent = original_parent.with_name("moved-private")
    original_parent.rename(moved_parent)

    with pytest.raises(OSError):
        delete_catalog(catalog)

    assert not original_parent.exists()
    assert (moved_parent / catalog_path.name).exists()


def test_delete_catalog_refuses_target_substitution_and_leaves_files(
    catalog: Catalog, catalog_path: Path
) -> None:
    original = catalog_path.with_name("original.sqlite3")
    catalog_path.rename(original)
    victim = catalog_path.with_name("victim.txt")
    victim.write_text("keep", encoding="utf-8")
    catalog_path.symlink_to(victim)

    with pytest.raises((OSError, ValueError)):
        delete_catalog(catalog)

    assert original.exists()
    assert catalog_path.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"
    with pytest.raises(CatalogUnavailableError):
        catalog.get_artist("anything")


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_delete_catalog_refuses_sidecar_symlink_without_partial_deletion(
    catalog: Catalog,
    catalog_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    victim = tmp_path / f"victim{suffix}.txt"
    victim.write_text("keep", encoding="utf-8")
    original_close = catalog.close

    def close_then_substitute() -> None:
        original_close()
        Path(f"{catalog_path}{suffix}").symlink_to(victim)

    monkeypatch.setattr(catalog, "close", close_then_substitute)

    with pytest.raises(OSError):
        delete_catalog(catalog)

    assert catalog_path.exists()
    assert Path(f"{catalog_path}{suffix}").is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"
    with pytest.raises(CatalogUnavailableError):
        catalog.get_artist("anything")
