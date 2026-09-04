from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

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
from music_friend.store import Catalog

OFFSET = timezone(timedelta(hours=5, minutes=45))
OBSERVED = datetime(2026, 8, 31, 23, 17, 41, 123456, tzinfo=OFFSET)
LATER = datetime(2026, 9, 1, 1, 2, 3, 654321, tzinfo=timezone(timedelta(hours=-7)))


def _source(source: str, native_id: str, position: int = 0) -> SourceReference:
    return SourceReference(
        source=source,
        native_id=native_id,
        canonical_url=f"https://example.test/{source}/{position}",
        observed_at=OBSERVED,
    )


def _artist(local_id: str = "artist-1") -> Artist:
    return Artist(
        local_id=local_id,
        display_name="O'Brien; DROP TABLE artists; --",
        source_refs=(
            _source("first-source", "native-'one", 1),
            _source("second-source", "same", 2),
        ),
        identity_confidence=IdentityConfidence.USER_CONFIRMED,
        observed_at=OBSERVED,
    )


def test_artist_round_trip_preserves_fields_and_normalizes_offset(catalog: Catalog) -> None:
    expected = _artist()

    catalog.put_artist(expected)

    actual = catalog.get_artist(expected.local_id)
    assert isinstance(actual, Artist)
    assert asdict(actual) == asdict(expected)
    assert actual.observed_at.isoformat() == "2026-08-31T17:32:41.123456+00:00"
    assert tuple(reference.source for reference in actual.source_refs) == (
        "first-source",
        "second-source",
    )


def test_release_round_trip_preserves_artist_order_and_date(catalog: Catalog) -> None:
    first = _artist("artist-1")
    second = Artist(
        local_id="artist-2",
        display_name="Second Artist",
        source_refs=(_source("release-source", "artist-2"),),
        identity_confidence=IdentityConfidence.EXTERNAL_ID,
        observed_at=LATER,
    )
    catalog.put_artist(first)
    catalog.put_artist(second)
    expected = Release(
        local_id="release-1",
        title="Apostrophe's; SELECT * FROM releases",
        release_type="album",
        release_date=date(2026, 8, 31),
        date_precision=ReleaseDatePrecision.DAY,
        artist_refs=(second.local_id, first.local_id),
        source_refs=(_source("release-source", "release-1", 3),),
        observed_at=OBSERVED,
    )

    catalog.put_release(expected)

    actual = catalog.get_release(expected.local_id)
    assert isinstance(actual, Release)
    assert asdict(actual) == asdict(expected)
    assert actual.artist_refs == ("artist-2", "artist-1")


def test_event_round_trip_preserves_links_artist_order_and_normalizes_offsets(
    catalog: Catalog,
) -> None:
    first = _artist("artist-1")
    second = Artist(
        local_id="artist-2",
        display_name="Second Artist",
        source_refs=(_source("event-source", "artist-2"),),
        identity_confidence=IdentityConfidence.SOURCE_ONLY,
        observed_at=OBSERVED,
    )
    catalog.put_artist(first)
    catalog.put_artist(second)
    expected = Event(
        local_id="event-1",
        title="Tonight's Show; DELETE FROM events",
        artist_refs=(first.local_id, second.local_id),
        venue_name="The O'Brien Room",
        locality="St. John's",
        starts_at=LATER,
        time_precision="second",
        source_links=("https://example.test/two", "https://example.test/one"),
        source_refs=(_source("event-source", "event-1", 4),),
        observed_at=OBSERVED,
    )

    catalog.put_event(expected)

    actual = catalog.get_event(expected.local_id)
    assert isinstance(actual, Event)
    assert asdict(actual) == asdict(expected)
    assert actual.source_links == ("https://example.test/two", "https://example.test/one")
    assert actual.starts_at is not None
    assert actual.starts_at.isoformat() == "2026-09-01T08:02:03.654321+00:00"


def test_interest_round_trip_preserves_canonical_record(catalog: Catalog) -> None:
    artist = _artist()
    catalog.put_artist(artist)
    expected = Interest(
        local_id="interest-1",
        kind=InterestKind.ARTIST,
        target_local_id=artist.local_id,
        status=InterestStatus.ACTIVE,
        created_by="user's choice; --",
        created_at=OBSERVED,
        updated_at=LATER,
    )

    catalog.put_interest(expected)

    actual = catalog.get_interest(expected.local_id)
    assert isinstance(actual, Interest)
    assert asdict(actual) == asdict(expected)


def test_observation_round_trip_preserves_canonical_record(catalog: Catalog) -> None:
    artist = _artist()
    catalog.put_artist(artist)
    expected = Observation(
        local_id="observation-1",
        source="first-source",
        native_id="native-'one",
        record_kind="artist",
        record_local_id=artist.local_id,
        fact_name="tour_status'; DROP TABLE observations; --",
        observed_at=LATER,
    )

    catalog.put_observation(expected)

    actual = catalog.get_observation(expected.local_id)
    assert isinstance(actual, Observation)
    assert asdict(actual) == asdict(expected)


def test_check_time_round_trip_normalizes_offset(catalog: Catalog) -> None:
    catalog.set_check_time("source'; DELETE FROM check_times; --", OBSERVED)

    actual = catalog.get_check_time("source'; DELETE FROM check_times; --")

    assert actual == OBSERVED
    assert actual is not None
    assert actual.isoformat() == "2026-08-31T17:32:41.123456+00:00"


def test_missing_foreign_artist_rejects_release(catalog: Catalog) -> None:
    release = Release(
        local_id="release-missing-artist",
        title="Missing",
        release_type="single",
        release_date=date(2026, 1, 1),
        date_precision=ReleaseDatePrecision.YEAR,
        artist_refs=("not-present",),
        source_refs=(_source("source", "release-missing"),),
        observed_at=OBSERVED,
    )

    with pytest.raises(sqlite3.IntegrityError):
        catalog.put_release(release)

    assert catalog.get_release(release.local_id) is None


def test_transaction_rolls_back_first_record_when_second_fails(catalog: Catalog) -> None:
    artist = _artist("rollback-artist")
    release = Release(
        local_id="rollback-release",
        title="Invalid release",
        release_type="album",
        release_date=date(2026, 1, 1),
        date_precision=ReleaseDatePrecision.YEAR,
        artist_refs=("missing-artist",),
        source_refs=(_source("source", "rollback-release"),),
        observed_at=OBSERVED,
    )

    with pytest.raises(sqlite3.IntegrityError):
        with catalog.transaction():
            catalog.put_artist(artist)
            catalog.put_release(release)

    assert catalog.get_artist(artist.local_id) is None
    assert catalog.get_release(release.local_id) is None


def test_caught_nested_failure_rolls_back_only_inner_writes(catalog: Catalog) -> None:
    before = _artist("before-inner")
    inner = _artist("inner-rolled-back")
    after = _artist("after-inner")

    with catalog.transaction():
        catalog.put_artist(before)
        try:
            with catalog.transaction():
                catalog.put_artist(inner)
                raise RuntimeError("inner failure")
        except RuntimeError:
            pass
        catalog.put_artist(after)

    assert catalog.get_artist(before.local_id) == before
    assert catalog.get_artist(inner.local_id) is None
    assert catalog.get_artist(after.local_id) == after


def test_same_native_id_from_different_sources_is_not_substituted(catalog: Catalog) -> None:
    expected = Artist(
        local_id="source-isolation",
        display_name="Source Isolation",
        source_refs=(
            _source("source-a", "shared-native", 1),
            _source("source-b", "shared-native", 2),
        ),
        identity_confidence=IdentityConfidence.EXTERNAL_ID,
        observed_at=OBSERVED,
    )

    catalog.put_artist(expected)

    actual = catalog.get_artist(expected.local_id)
    assert actual is not None
    assert tuple((ref.source, ref.native_id) for ref in actual.source_refs) == (
        ("source-a", "shared-native"),
        ("source-b", "shared-native"),
    )


def test_disconnect_source_removes_only_selected_source_state(catalog: Catalog) -> None:
    artist = Artist(
        local_id="preserved-artist",
        display_name="Preserved Artist",
        source_refs=(
            _source("disconnect-me", "shared", 1),
            _source("keep-me", "shared", 2),
        ),
        identity_confidence=IdentityConfidence.EXTERNAL_ID,
        observed_at=OBSERVED,
    )
    catalog.put_artist(artist)
    interest = Interest(
        local_id="preserved-interest",
        kind=InterestKind.ARTIST,
        target_local_id=artist.local_id,
        status=InterestStatus.ACTIVE,
        created_by="user",
        created_at=OBSERVED,
        updated_at=OBSERVED,
    )
    catalog.put_interest(interest)
    removed_observation = Observation(
        local_id="removed-observation",
        source="disconnect-me",
        native_id="shared",
        record_kind="artist",
        record_local_id=artist.local_id,
        fact_name="status",
        observed_at=OBSERVED,
    )
    kept_observation = Observation(
        local_id="kept-observation",
        source="keep-me",
        native_id="shared",
        record_kind="artist",
        record_local_id=artist.local_id,
        fact_name="status",
        observed_at=OBSERVED,
    )
    catalog.put_observation(removed_observation)
    catalog.put_observation(kept_observation)
    catalog.set_check_time("disconnect-me", OBSERVED)

    catalog.disconnect_source("disconnect-me")

    remaining_artist = catalog.get_artist(artist.local_id)
    assert remaining_artist is not None
    assert tuple(ref.source for ref in remaining_artist.source_refs) == ("keep-me",)
    assert catalog.get_interest(interest.local_id) is not None
    assert catalog.get_observation(removed_observation.local_id) is None
    assert catalog.get_observation(kept_observation.local_id) is not None
    assert catalog.get_check_time("disconnect-me") is None
    orphan_count = catalog._connection.execute(
        "SELECT COUNT(*) FROM source_references WHERE source = ?", ("disconnect-me",)
    ).fetchone()[0]
    assert orphan_count == 0


def test_artist_upsert_preserves_retained_observation_and_removes_remapped_one(
    catalog: Catalog,
) -> None:
    original = Artist(
        local_id="remapped-artist",
        display_name="Original",
        source_refs=(_source("remove-source", "remove-id"), _source("keep-source", "keep-id")),
        identity_confidence=IdentityConfidence.SOURCE_ONLY,
        observed_at=OBSERVED,
    )
    catalog.put_artist(original)
    removed = Observation(
        local_id="removed-after-remap",
        source="remove-source",
        native_id="remove-id",
        record_kind="artist",
        record_local_id=original.local_id,
        fact_name="status",
        observed_at=OBSERVED,
    )
    retained = Observation(
        local_id="retained-after-remap",
        source="keep-source",
        native_id="keep-id",
        record_kind="artist",
        record_local_id=original.local_id,
        fact_name="status",
        observed_at=OBSERVED,
    )
    catalog.put_observation(removed)
    catalog.put_observation(retained)
    replacement = Artist(
        local_id=original.local_id,
        display_name="Replacement",
        source_refs=(_source("keep-source", "keep-id"), _source("new-source", "new-id")),
        identity_confidence=IdentityConfidence.USER_CONFIRMED,
        observed_at=LATER,
    )

    catalog.put_artist(replacement)

    actual = catalog.get_artist(original.local_id)
    assert actual is not None
    assert asdict(actual) == asdict(replacement)
    assert catalog.get_observation(removed.local_id) is None
    assert catalog.get_observation(retained.local_id) is not None


def test_release_upsert_replaces_ordered_artist_and_source_links(catalog: Catalog) -> None:
    first = _artist("release-upsert-artist-1")
    second = _artist("release-upsert-artist-2")
    catalog.put_artist(first)
    catalog.put_artist(second)
    original = Release(
        local_id="release-upsert",
        title="Original",
        release_type="album",
        release_date=date(2026, 1, 1),
        date_precision=ReleaseDatePrecision.YEAR,
        artist_refs=(first.local_id,),
        source_refs=(_source("old-release-source", "old-release-id"),),
        observed_at=OBSERVED,
    )
    replacement = Release(
        local_id=original.local_id,
        title="Replacement",
        release_type="single",
        release_date=date(2026, 9, 1),
        date_precision=ReleaseDatePrecision.DAY,
        artist_refs=(second.local_id, first.local_id),
        source_refs=(
            _source("new-release-source", "new-release-id"),
            _source("other-release-source", "other-release-id"),
        ),
        observed_at=LATER,
    )
    catalog.put_release(original)

    catalog.put_release(replacement)

    actual = catalog.get_release(original.local_id)
    assert actual is not None
    assert asdict(actual) == asdict(replacement)


def test_event_upsert_replaces_all_ordered_links(catalog: Catalog) -> None:
    first = _artist("event-upsert-artist-1")
    second = _artist("event-upsert-artist-2")
    catalog.put_artist(first)
    catalog.put_artist(second)
    original = Event(
        local_id="event-upsert",
        title="Original event",
        artist_refs=(first.local_id,),
        venue_name=None,
        locality=None,
        starts_at=None,
        time_precision=None,
        source_links=("https://example.test/original",),
        source_refs=(_source("old-event-source", "old-event-id"),),
        observed_at=OBSERVED,
    )
    replacement = Event(
        local_id=original.local_id,
        title="Replacement event",
        artist_refs=(second.local_id, first.local_id),
        venue_name="Replacement venue",
        locality="Replacement locality",
        starts_at=LATER,
        time_precision="second",
        source_links=("https://example.test/new-2", "https://example.test/new-1"),
        source_refs=(_source("new-event-source", "new-event-id"),),
        observed_at=LATER,
    )
    catalog.put_event(original)

    catalog.put_event(replacement)

    actual = catalog.get_event(original.local_id)
    assert actual is not None
    assert asdict(actual) == asdict(replacement)


def test_failed_release_upsert_restores_old_fields_and_links(catalog: Catalog) -> None:
    artist = _artist("rollback-existing-artist")
    catalog.put_artist(artist)
    original = Release(
        local_id="rollback-existing-release",
        title="Original",
        release_type="album",
        release_date=date(2026, 1, 1),
        date_precision=ReleaseDatePrecision.YEAR,
        artist_refs=(artist.local_id,),
        source_refs=(_source("old-source", "old-id"),),
        observed_at=OBSERVED,
    )
    catalog.put_release(original)
    invalid = Release(
        local_id=original.local_id,
        title="Must roll back",
        release_type="single",
        release_date=date(2026, 9, 1),
        date_precision=ReleaseDatePrecision.DAY,
        artist_refs=(artist.local_id, "missing-artist"),
        source_refs=(_source("new-source", "new-id"),),
        observed_at=LATER,
    )

    with pytest.raises(sqlite3.IntegrityError):
        catalog.put_release(invalid)

    actual = catalog.get_release(original.local_id)
    assert actual is not None
    assert asdict(actual) == asdict(original)


def test_raw_canonical_identity_mutation_cannot_dangle_polymorphic_links(
    catalog: Catalog,
) -> None:
    artist = _artist("raw-protected-artist")
    release_artist = _artist("raw-release-artist")
    catalog.put_artist(artist)
    catalog.put_artist(release_artist)
    release = Release(
        local_id="raw-protected-release",
        title="Protected release",
        release_type="album",
        release_date=date(2026, 1, 1),
        date_precision=ReleaseDatePrecision.YEAR,
        artist_refs=(release_artist.local_id,),
        source_refs=(_source("raw-release-source", "raw-release-id"),),
        observed_at=OBSERVED,
    )
    event = Event(
        local_id="raw-protected-event",
        title="Protected event",
        artist_refs=(),
        venue_name=None,
        locality=None,
        starts_at=None,
        time_precision=None,
        source_links=(),
        source_refs=(_source("raw-event-source", "raw-event-id"),),
        observed_at=OBSERVED,
    )
    catalog.put_release(release)
    catalog.put_event(event)
    connection = catalog._connection
    assert connection is not None

    for table, local_id in (
        ("artists", artist.local_id),
        ("releases", release.local_id),
        ("events", event.local_id),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {table} SET local_id = ? WHERE local_id = ?",
                (f"changed-{local_id}", local_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"DELETE FROM {table} WHERE local_id = ?", (local_id,))
