"""Normalizer edge cases: date precision, rejection, and artist resolution."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from music_friend.domain import ReleaseDatePrecision
from music_friend.providers.musicbrainz.normalize import normalize_release_group

FIXED_NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
MBID = "11111111-1111-1111-1111-111111111111"
RGID = "22222222-2222-2222-2222-222222222222"


def _release_group(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": RGID,
        "title": "Synthetic Release",
        "primary-type": "Album",
        "first-release-date": "2026-05-01",
        "artist-credit": [{"artist": {"id": MBID}}],
    }
    base.update(overrides)
    return base


def test_year_only_date_yields_year_precision() -> None:
    release = normalize_release_group(
        _release_group(**{"first-release-date": "2026"}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.release_date == date(2026, 1, 1)
    assert release.date_precision == ReleaseDatePrecision.YEAR


def test_year_month_date_yields_month_precision() -> None:
    release = normalize_release_group(
        _release_group(**{"first-release-date": "2026-05"}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.release_date == date(2026, 5, 1)
    assert release.date_precision == ReleaseDatePrecision.MONTH


def test_full_date_yields_day_precision() -> None:
    release = normalize_release_group(
        _release_group(**{"first-release-date": "2026-05-17"}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.release_date == date(2026, 5, 17)
    assert release.date_precision == ReleaseDatePrecision.DAY


def test_empty_first_release_date_is_rejected() -> None:
    release = normalize_release_group(
        _release_group(**{"first-release-date": ""}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is None


def test_missing_first_release_date_is_rejected() -> None:
    group = _release_group()
    del group["first-release-date"]
    release = normalize_release_group(group, artist_id_map={MBID: "local-artist"}, now=FIXED_NOW)
    assert release is None


def test_unresolvable_artist_credit_is_rejected() -> None:
    release = normalize_release_group(
        _release_group(),
        artist_id_map={},
        now=FIXED_NOW,
    )
    assert release is None


def test_non_album_single_ep_type_still_normalizes_but_lowercases() -> None:
    release = normalize_release_group(
        _release_group(**{"primary-type": "Broadcast"}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.release_type == "broadcast"


def test_missing_id_is_rejected() -> None:
    group = _release_group()
    del group["id"]
    assert (
        normalize_release_group(group, artist_id_map={MBID: "local-artist"}, now=FIXED_NOW) is None
    )


def test_missing_title_is_rejected() -> None:
    group = _release_group()
    del group["title"]
    assert (
        normalize_release_group(group, artist_id_map={MBID: "local-artist"}, now=FIXED_NOW) is None
    )


def test_missing_primary_type_is_rejected() -> None:
    group = _release_group()
    del group["primary-type"]
    assert (
        normalize_release_group(group, artist_id_map={MBID: "local-artist"}, now=FIXED_NOW) is None
    )


def test_non_string_primary_type_is_rejected() -> None:
    release = normalize_release_group(
        _release_group(**{"primary-type": 42}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is None


@pytest.mark.parametrize("malformed_date", ["not-a-date", "2026-13", "2026-02-30", "abcd"])
def test_unparseable_dates_are_rejected(malformed_date: str) -> None:
    release = normalize_release_group(
        _release_group(**{"first-release-date": malformed_date}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is None


def test_non_list_artist_credit_is_rejected() -> None:
    release = normalize_release_group(
        _release_group(**{"artist-credit": "not-a-list"}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is None


def test_artist_credit_entries_that_are_not_mappings_are_skipped() -> None:
    release = normalize_release_group(
        _release_group(**{"artist-credit": ["not-a-mapping", {"artist": {"id": MBID}}]}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.artist_refs == ("local-artist",)


def test_artist_credit_missing_artist_object_is_skipped() -> None:
    release = normalize_release_group(
        _release_group(**{"artist-credit": [{"no-artist-key": True}, {"artist": {"id": MBID}}]}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.artist_refs == ("local-artist",)


def test_artist_credit_with_non_string_mbid_is_skipped() -> None:
    release = normalize_release_group(
        _release_group(**{"artist-credit": [{"artist": {"id": 42}}, {"artist": {"id": MBID}}]}),
        artist_id_map={MBID: "local-artist"},
        now=FIXED_NOW,
    )
    assert release is not None
    assert release.artist_refs == ("local-artist",)


def test_local_id_is_stable_for_the_same_release_group_id() -> None:
    first = normalize_release_group(
        _release_group(), artist_id_map={MBID: "local-artist"}, now=FIXED_NOW
    )
    second = normalize_release_group(
        _release_group(), artist_id_map={MBID: "local-artist"}, now=FIXED_NOW
    )
    assert first is not None and second is not None
    assert first.local_id == second.local_id
