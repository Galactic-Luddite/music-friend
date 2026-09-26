"""Unit tests for normalize_album's branch coverage."""

from __future__ import annotations

from datetime import date, datetime, timezone

from music_friend.domain import ReleaseDatePrecision
from music_friend.providers.deezer.normalize import normalize_album

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def _album(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 42,
        "title": "Synthetic Album",
        "link": "https://www.deezer.com/album/42",
        "release_date": "2026-05-01",
        "record_type": "album",
    }
    base.update(overrides)
    return base


def test_normalize_album_happy_path() -> None:
    release = normalize_album(_album(), artist_native_id="1000001", now=NOW)
    assert release is not None
    assert release.title == "Synthetic Album"
    assert release.release_type == "album"
    assert release.release_date == date(2026, 5, 1)
    assert release.date_precision is ReleaseDatePrecision.DAY
    assert release.source_refs[0].canonical_url == "https://www.deezer.com/album/42"


def test_normalize_album_missing_id_returns_none() -> None:
    assert normalize_album(_album(id=None), artist_native_id="1", now=NOW) is None


def test_normalize_album_non_integer_id_returns_none() -> None:
    assert normalize_album(_album(id="42"), artist_native_id="1", now=NOW) is None


def test_normalize_album_boolean_id_returns_none() -> None:
    assert normalize_album(_album(id=True), artist_native_id="1", now=NOW) is None


def test_normalize_album_missing_title_returns_none() -> None:
    assert normalize_album(_album(title=None), artist_native_id="1", now=NOW) is None


def test_normalize_album_missing_record_type_returns_none() -> None:
    assert normalize_album(_album(record_type=None), artist_native_id="1", now=NOW) is None


def test_normalize_album_non_string_record_type_returns_none() -> None:
    assert normalize_album(_album(record_type=1), artist_native_id="1", now=NOW) is None


def test_normalize_album_missing_release_date_returns_none() -> None:
    assert normalize_album(_album(release_date=None), artist_native_id="1", now=NOW) is None


def test_normalize_album_non_string_release_date_returns_none() -> None:
    assert normalize_album(_album(release_date=20260501), artist_native_id="1", now=NOW) is None


def test_normalize_album_unparseable_release_date_returns_none() -> None:
    assert normalize_album(_album(release_date="not-a-date"), artist_native_id="1", now=NOW) is None


def test_normalize_album_missing_link_falls_back_to_a_constructed_url() -> None:
    release = normalize_album(_album(link=None), artist_native_id="1", now=NOW)
    assert release is not None
    assert release.source_refs[0].canonical_url == "https://www.deezer.com/album/42"


def test_normalize_album_year_precision_date() -> None:
    release = normalize_album(_album(release_date="2026"), artist_native_id="1", now=NOW)
    assert release is not None
    assert release.date_precision is ReleaseDatePrecision.YEAR
    assert release.release_date == date(2026, 1, 1)


def test_normalize_album_month_precision_date() -> None:
    release = normalize_album(_album(release_date="2026-05"), artist_native_id="1", now=NOW)
    assert release is not None
    assert release.date_precision is ReleaseDatePrecision.MONTH
    assert release.release_date == date(2026, 5, 1)


def test_normalize_album_out_of_range_date_returns_none() -> None:
    assert normalize_album(_album(release_date="2026-13-01"), artist_native_id="1", now=NOW) is None


def test_normalize_album_blank_release_date_returns_none() -> None:
    assert normalize_album(_album(release_date="   "), artist_native_id="1", now=NOW) is None


def test_normalize_album_rejects_a_purely_hostile_title() -> None:
    release = normalize_album(_album(title="\x1b\x1b\x1b"), artist_native_id="1", now=NOW)
    assert release is None


def test_normalize_album_distinct_artist_native_ids_yield_distinct_placeholder_artist_refs() -> (
    None
):
    first = normalize_album(_album(), artist_native_id="1000001", now=NOW)
    second = normalize_album(_album(), artist_native_id="2000002", now=NOW)
    assert first is not None and second is not None
    assert first.artist_refs != second.artist_refs
