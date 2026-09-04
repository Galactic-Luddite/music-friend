from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone

import pytest

from music_friend.domain.models import (
    Artist,
    CatalogItem,
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


@pytest.fixture
def observed_at() -> datetime:
    return datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def reference(observed_at: datetime) -> SourceReference:
    return SourceReference("example", "artist-123", "https://example.test/artists/123", observed_at)


@pytest.mark.parametrize(
    ("record", "field_name", "replacement"),
    [
        (
            SourceReference(
                "example", "native-1", None, datetime(2026, 8, 31, tzinfo=timezone.utc)
            ),
            "source",
            "other-source",
        ),
        (
            Artist(
                "artist-1",
                "An Artist",
                (
                    SourceReference(
                        "example", "native-1", None, datetime(2026, 8, 31, tzinfo=timezone.utc)
                    ),
                ),
                IdentityConfidence.SOURCE_ONLY,
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "display_name",
            "Renamed",
        ),
        (
            Release(
                "release-1",
                "A Release",
                "album",
                date(2026, 8, 31),
                ReleaseDatePrecision.DAY,
                ("artist-1",),
                (
                    SourceReference(
                        "example", "release-1", None, datetime(2026, 8, 31, tzinfo=timezone.utc)
                    ),
                ),
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "title",
            "Renamed",
        ),
        (
            Event(
                "event-1",
                "A Show",
                (),
                None,
                None,
                None,
                None,
                (),
                (
                    SourceReference(
                        "example", "event-1", None, datetime(2026, 8, 31, tzinfo=timezone.utc)
                    ),
                ),
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "title",
            "Renamed",
        ),
        (
            Interest(
                "interest-1",
                InterestKind.ARTIST,
                "artist-1",
                InterestStatus.ACTIVE,
                "user",
                datetime(2026, 8, 31, tzinfo=timezone.utc),
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "status",
            InterestStatus.PAUSED,
        ),
        (
            Observation(
                "observation-1",
                "example",
                "native-1",
                "artist",
                "artist-1",
                "genre",
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "fact_name",
            "mood",
        ),
        (
            CatalogItem(
                "album",
                "item-1",
                "An Item",
                (),
                (
                    SourceReference(
                        "example", "item-1", None, datetime(2026, 8, 31, tzinfo=timezone.utc)
                    ),
                ),
                datetime(2026, 8, 31, tzinfo=timezone.utc),
            ),
            "title",
            "Renamed",
        ),
    ],
)
def test_records_are_frozen(record: object, field_name: str, replacement: object) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(record, field_name, replacement)


@pytest.mark.parametrize("value", ["", " \t\n "])
def test_required_text_rejects_empty_or_whitespace(
    value: str, reference: SourceReference, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        Artist(value, "An Artist", (reference,), IdentityConfidence.SOURCE_ONLY, observed_at)

    with pytest.raises(ValueError):
        Artist("artist-1", value, (reference,), IdentityConfidence.SOURCE_ONLY, observed_at)

    with pytest.raises(ValueError):
        SourceReference(value, "native-1", None, observed_at)

    with pytest.raises(ValueError):
        SourceReference("example", value, None, observed_at)

    with pytest.raises(ValueError):
        Release(
            "release-1",
            value,
            "album",
            date(2026, 8, 31),
            ReleaseDatePrecision.DAY,
            ("artist-1",),
            (reference,),
            observed_at,
        )

    with pytest.raises(ValueError):
        Observation("observation-1", "example", "native-1", value, "artist-1", "genre", observed_at)

    with pytest.raises(ValueError):
        Interest(
            "interest-1",
            InterestKind.ARTIST,
            "artist-1",
            InterestStatus.ACTIVE,
            value,
            observed_at,
            observed_at,
        )


@pytest.mark.parametrize(
    "constructor",
    [
        lambda observed_at: SourceReference("example", "native-1", None, observed_at),
        lambda observed_at: Artist(
            "artist-1",
            "An Artist",
            (SourceReference("example", "native-1", None, datetime.now(timezone.utc)),),
            IdentityConfidence.SOURCE_ONLY,
            observed_at,
        ),
        lambda observed_at: Release(
            "release-1",
            "A Release",
            "album",
            date(2026, 8, 31),
            ReleaseDatePrecision.DAY,
            ("artist-1",),
            (SourceReference("example", "native-1", None, datetime.now(timezone.utc)),),
            observed_at,
        ),
        lambda observed_at: Event(
            "event-1",
            "A Show",
            ("artist-1",),
            None,
            None,
            None,
            None,
            (),
            (SourceReference("example", "native-1", None, datetime.now(timezone.utc)),),
            observed_at,
        ),
        lambda observed_at: Interest(
            "interest-1",
            InterestKind.ARTIST,
            "artist-1",
            InterestStatus.ACTIVE,
            "user",
            observed_at,
            datetime.now(timezone.utc),
        ),
        lambda observed_at: Observation(
            "observation-1", "example", "native-1", "artist", "artist-1", "genre", observed_at
        ),
        lambda observed_at: CatalogItem(
            "album",
            "item-1",
            "An Item",
            ("artist-1",),
            (SourceReference("example", "native-1", None, datetime.now(timezone.utc)),),
            observed_at,
        ),
    ],
)
def test_datetimes_must_be_timezone_aware(constructor: object) -> None:
    with pytest.raises(ValueError):
        constructor(datetime(2026, 8, 31, 12, 30))  # type: ignore[operator]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/artist",
        "ftp://example.test/artist",
        "https://user:password@example.test/artist",
        "https://example.test/artist#profile",
        "https:///artist",
    ],
)
def test_canonical_urls_must_be_safe_https(url: str, observed_at: datetime) -> None:
    with pytest.raises(ValueError):
        SourceReference("example", "native-1", url, observed_at)


def test_artist_identity_uses_record_type_and_local_id(
    reference: SourceReference, observed_at: datetime
) -> None:
    first = Artist(
        "artist-1", "First Name", (reference,), IdentityConfidence.SOURCE_ONLY, observed_at
    )
    renamed = Artist(
        "artist-1", "Second Name", (reference,), IdentityConfidence.EXTERNAL_ID, observed_at
    )
    same_name = Artist(
        "artist-2", "First Name", (reference,), IdentityConfidence.SOURCE_ONLY, observed_at
    )

    assert first == renamed
    assert hash(first) == hash(renamed)
    assert first != same_name
    assert first != CatalogItem("artist", "artist-1", "First Name", (), (reference,), observed_at)


@pytest.mark.parametrize(
    ("release_date", "precision"),
    [
        (date(2026, 1, 2), ReleaseDatePrecision.YEAR),
        (date(2026, 8, 2), ReleaseDatePrecision.MONTH),
    ],
)
def test_release_rejects_inconsistent_date_precision(
    release_date: date,
    precision: ReleaseDatePrecision,
    reference: SourceReference,
    observed_at: datetime,
) -> None:
    with pytest.raises(ValueError):
        Release(
            "release-1",
            "A Release",
            "album",
            release_date,
            precision,
            ("artist-1",),
            (reference,),
            observed_at,
        )


def test_release_requires_an_artist_reference(
    reference: SourceReference, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        Release(
            "release-1",
            "A Release",
            "album",
            date(2026, 1, 1),
            ReleaseDatePrecision.YEAR,
            (),
            (reference,),
            observed_at,
        )


@pytest.mark.parametrize("precision", ["date", "hour", "minute", "second"])
def test_event_accepts_known_precision_when_start_time_exists(
    precision: str, reference: SourceReference, observed_at: datetime
) -> None:
    event = Event(
        "event-1",
        "A Show",
        ("artist-1",),
        None,
        None,
        observed_at,
        precision,
        ("https://example.test/events/1",),
        (reference,),
        observed_at,
    )

    assert event.time_precision == precision


def test_event_does_not_invent_missing_time_precision(
    reference: SourceReference, observed_at: datetime
) -> None:
    event = Event("event-1", "A Show", (), None, None, None, None, (), (reference,), observed_at)

    assert event.starts_at is None
    assert event.time_precision is None

    with pytest.raises(ValueError):
        Event("event-1", "A Show", (), None, None, None, "date", (), (reference,), observed_at)

    with pytest.raises(ValueError):
        Event("event-1", "A Show", (), None, None, observed_at, None, (), (reference,), observed_at)

    with pytest.raises(ValueError):
        Event(
            "event-1", "A Show", (), None, None, observed_at, "day", (), (reference,), observed_at
        )


@pytest.mark.parametrize(
    "link",
    [
        "http://example.test/events/1",
        "https://user:password@example.test/events/1",
        "https://example.test/events/1#tickets",
    ],
)
def test_event_source_links_must_be_safe_https(
    link: str, reference: SourceReference, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        Event(
            "event-1",
            "A Show",
            (),
            None,
            None,
            None,
            None,
            (link,),
            (reference,),
            observed_at,
        )


def test_event_start_time_must_be_timezone_aware(
    reference: SourceReference, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        Event(
            "event-1",
            "A Show",
            (),
            None,
            None,
            datetime(2026, 8, 31, 12, 30),
            "minute",
            (),
            (reference,),
            observed_at,
        )


@pytest.mark.parametrize("invalid_precision", ["day", [], 1])
def test_event_rejects_invalid_time_precision_with_value_error(
    invalid_precision: object, reference: SourceReference, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        Event(
            "event-1",
            "A Show",
            (),
            None,
            None,
            observed_at,
            invalid_precision,  # type: ignore[arg-type]
            (),
            (reference,),
            observed_at,
        )


def test_observation_fact_name_is_bounded(observed_at: datetime) -> None:
    Observation(
        "observation-1", "example", "native-1", "artist", "artist-1", "x" * 128, observed_at
    )

    with pytest.raises(ValueError):
        Observation(
            "observation-1", "example", "native-1", "artist", "artist-1", "x" * 129, observed_at
        )


@pytest.mark.parametrize(
    "build_record",
    [
        lambda refs, observed_at: Artist(
            "artist-1", "An Artist", refs, IdentityConfidence.SOURCE_ONLY, observed_at
        ),
        lambda refs, observed_at: Release(
            "release-1",
            "A Release",
            "album",
            date(2026, 8, 31),
            ReleaseDatePrecision.DAY,
            ("artist-1",),
            refs,
            observed_at,
        ),
        lambda refs, observed_at: Event(
            "event-1", "A Show", (), None, None, None, None, (), refs, observed_at
        ),
        lambda refs, observed_at: CatalogItem("album", "item-1", "An Item", (), refs, observed_at),
    ],
)
def test_source_reference_collections_are_unique_tuples(
    build_record: object, reference: SourceReference, observed_at: datetime
) -> None:
    duplicate = SourceReference("example", "artist-123", None, observed_at)

    with pytest.raises(ValueError):
        build_record([reference], observed_at)  # type: ignore[operator,arg-type]

    with pytest.raises(ValueError):
        build_record((reference, duplicate), observed_at)  # type: ignore[operator]


def test_catalog_item_contains_only_normalized_fields(
    reference: SourceReference, observed_at: datetime
) -> None:
    item = CatalogItem("album", "item-1", "An Item", ("artist-1",), (reference,), observed_at)

    assert item.kind == "album"
    assert item.artist_refs == ("artist-1",)
    assert item.source_refs == (reference,)
    assert not hasattr(item, "provider_data")


def test_canonical_storage_records_allow_empty_source_references(
    observed_at: datetime,
) -> None:
    Artist("artist-1", "Artist", (), IdentityConfidence.SOURCE_ONLY, observed_at)
    Release(
        "release-1",
        "Release",
        "album",
        date(2026, 1, 1),
        ReleaseDatePrecision.YEAR,
        ("artist-1",),
        (),
        observed_at,
    )
    Event("event-1", "Event", ("artist-1",), None, None, None, None, (), (), observed_at)

    with pytest.raises(ValueError):
        CatalogItem("album", "item-1", "Item", (), (), observed_at)


def test_domain_text_and_url_accept_exact_4096_code_point_boundary(
    observed_at: datetime,
) -> None:
    text = "x" * 4096
    url = "https://example.test/" + "x" * (4096 - len("https://example.test/"))
    reference = SourceReference(text, text, url, observed_at)
    artist = Artist(text, text, (reference,), IdentityConfidence.SOURCE_ONLY, observed_at)

    assert len(artist.local_id) == 4096
    assert len(artist.display_name) == 4096
    assert len(reference.source) == 4096
    assert len(reference.native_id) == 4096
    assert reference.canonical_url is not None
    assert len(reference.canonical_url) == 4096


@pytest.mark.parametrize(
    "build",
    (
        lambda value, observed_at: SourceReference(value, "native", None, observed_at),
        lambda value, observed_at: SourceReference("source", value, None, observed_at),
        lambda value, observed_at: SourceReference(
            "source",
            "native",
            "https://example.test/" + value,
            observed_at,
        ),
        lambda value, observed_at: Artist(
            value,
            "Artist",
            (SourceReference("source", "native", None, observed_at),),
            IdentityConfidence.SOURCE_ONLY,
            observed_at,
        ),
        lambda value, observed_at: Artist(
            "artist-1",
            value,
            (SourceReference("source", "native", None, observed_at),),
            IdentityConfidence.SOURCE_ONLY,
            observed_at,
        ),
    ),
)
def test_domain_text_and_url_reject_values_over_4096_code_points(
    build: object, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        build("x" * 4097, observed_at)  # type: ignore[operator]
