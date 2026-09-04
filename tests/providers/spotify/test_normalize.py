from __future__ import annotations

import json
import traceback
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from music_friend.domain import (
    Artist,
    CatalogItem,
    CatalogItemBatch,
    IdentityConfidence,
    ReleaseDatePrecision,
)
from music_friend.domain.text import sanitize_source_text
from music_friend.errors import InvalidSourceResponseError
from music_friend.providers.spotify.normalize import (
    normalize_artist,
    normalize_release,
    normalize_track,
    normalize_track_batch,
)


def _artist(name: object = "unsafe <name>") -> dict[str, object]:
    return {"id": "artist123", "name": name, "external_urls": {"spotify": "ignored"}}


def _track(name: object = "unsafe <track>") -> dict[str, object]:
    return {
        "id": "track123",
        "name": name,
        "artists": [{"id": "artist123", "name": "ignored"}],
    }


def _release(precision: str = "day", value: str = "2026-01-02") -> dict[str, object]:
    return {
        "id": "album123",
        "name": "unsafe <album>",
        "album_type": "album",
        "release_date": value,
        "release_date_precision": precision,
        "artists": [{"id": "artist123", "name": "ignored"}],
    }


def test_artist_normalizes_only_canonical_fields(observed_at: datetime) -> None:
    artist = normalize_artist(_artist(), observed_at=observed_at)

    assert type(artist) is Artist
    assert artist.local_id == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b"
    )
    assert "spotify" not in artist.local_id
    assert "artist123" not in artist.local_id
    assert artist.display_name == "unsafe ＜name＞"
    assert artist.identity_confidence is IdentityConfidence.SOURCE_ONLY
    assert artist.source_refs[0].canonical_url == "https://open.spotify.com/artist/artist123"
    assert not hasattr(artist, "raw")


def test_track_normalizes_title_relationships_and_provenance(observed_at: datetime) -> None:
    item = normalize_track(_track(), observed_at=observed_at)

    assert type(item) is CatalogItem
    assert item.kind == "track"
    assert item.local_id == ("mf:12db052ca03cd42bd43441737bbea067839fc19dbb32d6630f60129b74e240b1")
    assert "track123" not in item.local_id
    assert item.title == "unsafe ＜track＞"
    assert item.artist_refs == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
    )
    assert item.source_refs[0].canonical_url == "https://open.spotify.com/track/track123"


def test_track_batch_normalizes_complete_credited_artist_records(observed_at: datetime) -> None:
    payload = _track()
    payload["artists"] = [
        {"id": "artist123", "name": "First <artist>"},
        {"id": "artist456", "name": "Second [artist]"},
    ]

    batch = normalize_track_batch(payload, observed_at=observed_at)

    assert type(batch) is CatalogItemBatch
    assert batch.items[0].artist_refs == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
        "mf:0738f2cd4795e5c289a6abff78a36cada828637e51f3e1bd4887118976d4b1dc",
    )
    assert tuple((artist.local_id, artist.display_name) for artist in batch.artists) == (
        (
            "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
            "First ＜artist＞",
        ),
        (
            "mf:0738f2cd4795e5c289a6abff78a36cada828637e51f3e1bd4887118976d4b1dc",
            "Second ［artist］",
        ),
    )


@pytest.mark.parametrize(
    ("precision", "value", "expected"),
    (
        ("year", "2026", "2026-01-01"),
        ("month", "2026-02", "2026-02-01"),
        ("day", "2026-02-03", "2026-02-03"),
    ),
)
def test_release_normalizes_date_precision(
    precision: str, value: str, expected: str, observed_at: datetime
) -> None:
    release = normalize_release(_release(precision, value), observed_at=observed_at)

    assert release.local_id == (
        "mf:350f46692129ac356d34473fcb123a45d52c3b0052a23ed86190ac5cc43ca565"
    )
    assert release.title == "unsafe ＜album＞"
    assert release.date_precision is ReleaseDatePrecision(precision)
    assert release.release_date.isoformat() == expected
    assert release.artist_refs == (
        "mf:6937f5f6c541b9e94720ed1b33b7fcb739a023eb06ac2ac86794ef68dffd980b",
    )
    assert release.source_refs[0].canonical_url == "https://open.spotify.com/album/album123"


@pytest.mark.parametrize(
    "payload",
    (
        None,
        [],
        {},
        {"id": None, "name": "name"},
        {"id": "bad/id", "name": "name"},
        {"id": "artist123", "name": None},
    ),
)
def test_artist_rejects_wrong_shapes_without_echoing_payload(
    payload: object, observed_at: datetime
) -> None:
    with pytest.raises(InvalidSourceResponseError) as caught:
        normalize_artist(payload, observed_at=observed_at)

    assert repr(payload) not in str(caught.value)


@pytest.mark.parametrize("bad_artists", (None, {}, [], [{"id": "artist123"}] * 51))
def test_track_rejects_missing_wrong_empty_or_oversized_artist_arrays(
    bad_artists: object, observed_at: datetime
) -> None:
    payload = _track()
    payload["artists"] = bad_artists

    with pytest.raises(InvalidSourceResponseError):
        normalize_track(payload, observed_at=observed_at)


def test_track_rejects_duplicate_artist_identifiers(observed_at: datetime) -> None:
    payload = _track()
    payload["artists"] = [{"id": "artist123"}, {"id": "artist123"}]

    with pytest.raises(InvalidSourceResponseError):
        normalize_track(payload, observed_at=observed_at)


@pytest.mark.parametrize(
    ("precision", "value"),
    (("day", "2026-02"), ("month", "2026-02-03"), ("year", "26"), ("hour", "2026")),
)
def test_release_rejects_malformed_dates(precision: str, value: str, observed_at: datetime) -> None:
    with pytest.raises(InvalidSourceResponseError):
        normalize_release(_release(precision, value), observed_at=observed_at)


def test_invalid_source_values_do_not_survive_exception_chains(observed_at: datetime) -> None:
    canary = "canaryDATE"

    with pytest.raises(InvalidSourceResponseError) as caught:
        normalize_release(_release("day", canary), observed_at=observed_at)

    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert canary not in rendered


def test_every_adversarial_string_is_sanitized_in_every_retained_text_field(
    observed_at: datetime,
) -> None:
    fixture_path = (
        Path(__file__).parents[2] / "security" / "injection-fixtures" / "source_text.json"
    )
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        raw = case["input"]
        assert normalize_artist(
            _artist(raw), observed_at=observed_at
        ).display_name == sanitize_source_text(raw, limit=96)
        assert normalize_track(_track(raw), observed_at=observed_at).title == sanitize_source_text(
            raw, limit=96
        )
        release_payload = deepcopy(_release())
        release_payload["name"] = raw
        assert normalize_release(
            release_payload, observed_at=observed_at
        ).title == sanitize_source_text(raw, limit=96)
