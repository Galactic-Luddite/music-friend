"""Issue #62: the single release write path, exercised against a real catalog."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.domain import (
    Artist,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxState,
    Release,
    ReleaseDatePrecision,
    SignalKind,
    SourceReference,
)
from music_friend.domain.observations import IdentityMethod, ReleaseObservation
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import update_inbox_state
from music_friend.tools.release_observation import (
    content_version_of,
    record_release_observation,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SOURCES = ("musicbrainz", "deezer")


def _artist(catalog: Catalog) -> Artist:
    artist = Artist(
        "artist:one",
        "Synthetic Artist One",
        (SourceReference("spotify", "synthetic-one", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )
    catalog.put_artist(artist)
    return artist


def _release(
    source: str,
    native_id: str,
    *,
    title: str = "Synthetic Record",
    release_date: date = date(2026, 8, 14),
    precision: ReleaseDatePrecision = ReleaseDatePrecision.DAY,
    local_id: str | None = None,
) -> Release:
    return Release(
        local_id or f"release:{source}:{native_id}",
        title,
        "album",
        release_date,
        precision,
        ("artist:one",),
        (SourceReference(source, native_id, f"https://example.test/{source}/{native_id}", NOW),),
        NOW,
    )


def _observe(
    catalog: Catalog,
    release: Release,
    *,
    at: datetime = NOW,
    links: tuple[SourceReference, ...] = (),
) -> object:
    reference = release.source_refs[0]
    return record_release_observation(
        catalog,
        ReleaseObservation(
            release=release,
            source=reference.source,
            native_id=reference.native_id,
            monitored_artist_local_id="artist:one",
            external_links=links,
            observed_at=at,
        ),
        release_sources=SOURCES,
    )


def _facts(catalog: Catalog, release_local_id: str) -> list[tuple[str, str]]:
    rows = (
        catalog._require_connection()
        .execute(
            "SELECT fact_name, source FROM observations WHERE record_local_id = ? ORDER BY 1, 2",
            (release_local_id,),
        )
        .fetchall()
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def test_content_version_ignores_provenance() -> None:
    """Provenance, timestamps, subject and explanation never change the content digest."""
    base = _release("musicbrainz", "rg-1")
    provenance_only = replace(
        base,
        source_refs=(
            SourceReference(
                "deezer", "al-9", "https://example.test/other", NOW + timedelta(days=3)
            ),
            SourceReference("musicbrainz", "rg-1", None, NOW + timedelta(days=3)),
        ),
        observed_at=NOW + timedelta(days=30),
        subject_local_id="subject:elsewhere",
    )

    assert content_version_of(provenance_only) == content_version_of(base)
    assert content_version_of(replace(base, title="Another Record")) != content_version_of(base)
    assert content_version_of(replace(base, release_date=date(2026, 8, 15))) != content_version_of(
        base
    )
    # The explanation is not part of a Release at all, so it cannot reach the digest; the
    # recorded signal proves the stored version is exactly this content-only digest.


def test_second_observation_is_a_no_op_and_attaching_a_source_is_provenance_only(
    tmp_path: Path,
) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        release = _release("musicbrainz", "rg-1")
        created = _observe(catalog, release)
        repeated = _observe(catalog, release, at=NOW + timedelta(days=1))
        attached = _observe(
            catalog,
            _release("deezer", "al-1"),
            at=NOW + timedelta(days=2),
            links=(SourceReference("musicbrainz", "rg-1", None, NOW),),
        )

        assert created.kind == "created" and created.signal_created  # type: ignore[attr-defined]
        assert repeated.kind == "unchanged" and not repeated.signal_created  # type: ignore[attr-defined]
        assert attached.kind == "provenance_attached"  # type: ignore[attr-defined]
        assert attached.method is IdentityMethod.EXTERNAL_LINK  # type: ignore[attr-defined]
        assert attached.release_local_id == release.local_id  # type: ignore[attr-defined]
        signals = catalog.list_signals(SignalKind.RELEASE, limit=10)
        entries = catalog.list_inbox_entries(None, limit=10)
        assert len(signals) == 1
        assert signals[0].material_version == content_version_of(release)
        assert len(entries) == 1
        assert entries[0].updated_at == NOW
        stored = catalog.get_release(release.local_id)
        assert stored is not None
        assert {reference.source for reference in stored.source_refs} == {"musicbrainz", "deezer"}
        assert catalog.get_release("release:deezer:al-1") is None
        assert ("source_attached", "deezer") in _facts(catalog, release.local_id)


@pytest.mark.parametrize("decided", [InboxState.SAVED, InboxState.DISMISSED])
def test_content_change_updates_the_existing_item_without_changing_state(
    tmp_path: Path, decided: InboxState
) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        _artist(catalog)
        original = _release("musicbrainz", "rg-1")
        _observe(catalog, original)
        entry = catalog.list_inbox_entries(None, limit=10)[0]
        first_signal = entry.latest_signal_local_id
        update_inbox_state(application, entry.local_id, decided, updated_at=NOW)

        later = NOW + timedelta(days=4)
        outcome = _observe(catalog, replace(original, release_date=date(2026, 8, 21)), at=later)

        assert outcome.kind == "updated" and outcome.signal_created  # type: ignore[attr-defined]
        entries = catalog.list_inbox_entries(None, limit=10)
        assert len(entries) == 1
        assert entries[0].local_id == entry.local_id
        assert entries[0].state is decided
        assert entries[0].created_at == entry.created_at
        assert entries[0].updated_at == later
        assert entries[0].latest_signal_local_id != first_signal
        latest = catalog.get_signal(entries[0].latest_signal_local_id)
        assert latest is not None
        assert [reason.kind for reason in latest.explanation.reasons] == [
            ExplanationReasonKind.MONITORED_ARTIST,
            ExplanationReasonKind.UPDATED_RELEASE,
        ]
        assert len(catalog.list_signals(SignalKind.RELEASE, limit=10)) == 2


def test_content_merge_precedence_is_deterministic_and_recorded(tmp_path: Path) -> None:
    """release_sources = (musicbrainz, deezer): MusicBrainz's title wins whatever arrives
    first; a DAY date beats a MONTH date from either source; each overruled value is one
    content_conflict observation against the losing source."""
    link = (SourceReference("musicbrainz", "rg-1", None, NOW),)
    deezer_link = (SourceReference("deezer", "al-1", None, NOW),)
    with Catalog.open(tmp_path / "deezer-first.sqlite3") as catalog:
        _artist(catalog)
        _observe(
            catalog,
            _release(
                "deezer",
                "al-1",
                title="Synthetic Record (Deluxe Edition)",
                release_date=date(2026, 8, 1),
                precision=ReleaseDatePrecision.MONTH,
            ),
        )
        _observe(
            catalog,
            _release(
                "musicbrainz", "rg-1", title="Synthetic Record", release_date=date(2026, 8, 14)
            ),
            links=deezer_link,
        )
        # Deezer reporting its own title again never takes the title back.
        _observe(
            catalog,
            _release(
                "deezer",
                "al-1",
                title="Synthetic Record (Deluxe Edition)",
                release_date=date(2026, 8, 1),
                precision=ReleaseDatePrecision.MONTH,
            ),
        )
        deezer_first = catalog.get_release("release:deezer:al-1")
        deezer_first_facts = _facts(catalog, "release:deezer:al-1")

    with Catalog.open(tmp_path / "musicbrainz-first.sqlite3") as catalog:
        _artist(catalog)
        _observe(
            catalog,
            _release(
                "musicbrainz",
                "rg-1",
                title="Synthetic Record",
                release_date=date(2026, 8, 1),
                precision=ReleaseDatePrecision.MONTH,
            ),
        )
        _observe(
            catalog,
            _release(
                "deezer",
                "al-1",
                title="Synthetic Record (Deluxe Edition)",
                release_date=date(2026, 8, 14),
            ),
            links=link,
        )
        musicbrainz_first = catalog.get_release("release:musicbrainz:rg-1")
        musicbrainz_first_facts = _facts(catalog, "release:musicbrainz:rg-1")

    assert deezer_first is not None and musicbrainz_first is not None
    for merged in (deezer_first, musicbrainz_first):
        assert merged.title == "Synthetic Record"
        assert merged.release_date == date(2026, 8, 14)
        assert merged.date_precision is ReleaseDatePrecision.DAY
    # Deezer-first: its title and MONTH date lose to MusicBrainz (recorded once even though
    # Deezer re-reported them); MusicBrainz-first: Deezer's title loses, MusicBrainz's
    # MONTH date loses to Deezer's DAY date.
    assert [fact for fact in deezer_first_facts if fact[0].startswith("content_conflict")] == [
        ("content_conflict:release_date", "deezer"),
        ("content_conflict:title", "deezer"),
    ]
    assert [fact for fact in musicbrainz_first_facts if fact[0].startswith("content_conflict")] == [
        ("content_conflict:release_date", "musicbrainz"),
        ("content_conflict:title", "deezer"),
    ]


def test_ambiguous_native_identity_becomes_its_own_release_and_is_recorded(
    tmp_path: Path,
) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        shared = SourceReference("musicbrainz", "rg-shared", None, NOW)
        catalog.put_release(replace(_release("musicbrainz", "a"), source_refs=(shared,)))
        catalog.put_release(replace(_release("musicbrainz", "b"), source_refs=(shared,)))

        outcome = _observe(
            catalog, _release("musicbrainz", "rg-shared", local_id="release:candidate")
        )

        assert outcome.kind == "ambiguous"  # type: ignore[attr-defined]
        assert outcome.release_local_id == "release:candidate"  # type: ignore[attr-defined]
        assert ("identity_ambiguous", "musicbrainz") in _facts(catalog, "release:candidate")
        assert catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, "release:candidate")


def test_record_release_observation_rejects_invalid_inputs(tmp_path: Path) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        release = _release("musicbrainz", "rg-1")
        observation = ReleaseObservation(release, "musicbrainz", "rg-1", "artist:one")
        with pytest.raises(ValueError, match="catalog"):
            record_release_observation(object(), observation, release_sources=SOURCES)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="observation"):
            record_release_observation(catalog, release, release_sources=SOURCES)  # type: ignore[arg-type]
        for bad in ((), ["musicbrainz"], ("",), (1,)):
            with pytest.raises(ValueError, match="release_sources"):
                record_release_observation(catalog, observation, release_sources=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="exactly its own source reference"):
            record_release_observation(
                catalog,
                ReleaseObservation(release, "deezer", "rg-1", "artist:one"),
                release_sources=SOURCES,
            )
        assert catalog.list_signals(None, limit=10) == ()
    with pytest.raises(ValueError, match="observed_at"):
        ReleaseObservation(
            release, "musicbrainz", "rg-1", "artist:one", observed_at=datetime(2026, 9, 1)
        )
    with pytest.raises(ValueError, match="external_links"):
        ReleaseObservation(release, "musicbrainz", "rg-1", "artist:one", external_links=("x",))  # type: ignore[arg-type]
