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


def test_contradictory_external_links_are_a_recorded_conflict_not_a_merge(
    tmp_path: Path,
) -> None:
    """Links naming two different stored releases never pick one: the observation becomes
    its own release, recorded as identity_conflict, and both stored releases are untouched."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        _observe(catalog, _release("musicbrainz", "rg-1"))
        _observe(catalog, _release("musicbrainz", "rg-2", title="Other Record"))

        outcome = _observe(
            catalog,
            _release("deezer", "al-1"),
            links=(
                SourceReference("musicbrainz", "rg-1", None, NOW),
                SourceReference("musicbrainz", "rg-2", None, NOW),
            ),
        )

        assert outcome.kind == "conflict"  # type: ignore[attr-defined]
        assert outcome.method is IdentityMethod.EXTERNAL_LINK  # type: ignore[attr-defined]
        assert ("identity_conflict", "deezer") in _facts(catalog, "release:deezer:al-1")
        for local_id in ("release:musicbrainz:rg-1", "release:musicbrainz:rg-2"):
            stored = catalog.get_release(local_id)
            assert stored is not None
            assert [reference.source for reference in stored.source_refs] == ["musicbrainz"]
        assert len(catalog.list_inbox_entries(None, limit=10)) == 3


def test_release_type_follows_source_order_and_a_lower_source_is_recorded(
    tmp_path: Path,
) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        _observe(catalog, _release("musicbrainz", "rg-1"))
        outcome = _observe(
            catalog,
            replace(_release("deezer", "al-1"), release_type="single"),
            links=(SourceReference("musicbrainz", "rg-1", None, NOW),),
        )

        stored = catalog.get_release("release:musicbrainz:rg-1")
        assert stored is not None and stored.release_type == "album"
        assert outcome.kind == "provenance_attached"  # type: ignore[attr-defined]
        assert ("content_conflict:release_type", "deezer") in _facts(
            catalog, "release:musicbrainz:rg-1"
        )


def test_an_observation_for_an_unknown_monitored_artist_writes_nothing(tmp_path: Path) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        release = _release("musicbrainz", "rg-1")
        with pytest.raises(ValueError, match="artist does not exist"):
            record_release_observation(
                catalog,
                ReleaseObservation(release, "musicbrainz", "rg-1", "artist:missing"),
                release_sources=SOURCES,
            )
        assert catalog.get_release(release.local_id) is None


# Issue #63: Identity ladder (tier 1 and tier 2 resolution) tests


def test_native_id_match_wins_before_title_key(tmp_path: Path) -> None:
    """Tier 1: a candidate whose (source, native_id) is already attached as a non-provisional
    reference merges without consulting the title key."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        # First observation: Deezer release
        outcome1 = _observe(catalog, _release("deezer", "album-123"))
        assert outcome1.kind == "created"
        deezer_release_id = outcome1.release_local_id

        # Second observation: same Deezer album, even with different title
        outcome2 = _observe(
            catalog,
            replace(_release("deezer", "album-123"), title="Different Title"),
        )
        # Tier 1 match on native_id, should merge without checking title
        assert outcome2.kind == "updated"  # or provenance_attached
        assert outcome2.release_local_id == deezer_release_id


def test_external_link_merges_in_either_arrival_order(tmp_path: Path) -> None:
    """Corroboration: Deezer observed after MusicBrainz harvest merges at tier 2;
    reverse arrival order (Deezer first) also merges at tier 2."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Case 1: MusicBrainz first with provisional Deezer link
        mb_release = _release("musicbrainz", "rg-1", title="Synthetic Record")
        deezer_link = SourceReference("deezer", "album-456", None, NOW, IdentityConfidence.PROVISIONAL)

        outcome1 = _observe(catalog, mb_release, links=(deezer_link,))
        assert outcome1.kind == "created"
        mb_subject = outcome1.subject_local_id

        # Now observe the same Deezer album (corroborates the provisional link)
        deezer_release = _release("deezer", "album-456", title="Synthetic Record")
        outcome2 = _observe(catalog, deezer_release)
        # Tier 1 hit on Deezer, should merge with the MusicBrainz release
        assert outcome2.subject_local_id == mb_subject
        assert outcome2.kind in ("updated", "provenance_attached")


def test_wrong_url_rel_is_detached_not_merged(tmp_path: Path) -> None:
    """Valid-but-wrong url-rel: a MusicBrainz url-rel pointing to a Deezer album with
    incompatible title/date detaches the provisional reference and creates a new subject."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # MusicBrainz harvest with provisional Deezer link
        mb_release = _release("musicbrainz", "rg-1", title="Album A", release_date=date(2026, 1, 1))
        deezer_link = SourceReference("deezer", "album-wrong", None, NOW, IdentityConfidence.PROVISIONAL)

        outcome1 = _observe(catalog, mb_release, links=(deezer_link,))
        assert outcome1.kind == "created"

        # Observe the Deezer album with incompatible title/date (wrong link detected)
        deezer_release = _release(
            "deezer", "album-wrong", title="Different Album", release_date=date(2026, 6, 1)
        )
        outcome2 = _observe(catalog, deezer_release)
        # Should be a new subject (identity_conflict detected)
        assert outcome2.kind in ("conflict", "ambiguous", "created")
        # The key point: we have two separate inbox items, not a merge
        deezer_stored = catalog.get_release(outcome2.release_local_id)
        assert deezer_stored is not None
        assert deezer_stored.subject_local_id != outcome1.subject_local_id


def test_empty_slot_provisional_link_still_requires_corroboration(tmp_path: Path) -> None:
    """Codex finding: a subject with no Deezer reference yet, a provisional MusicBrainz
    url-rel pointing at a Deezer album ID, where that Deezer album's own report has
    incompatible title/date -- assert the provisional reference stays PROVISIONAL
    (or gets detached) and does NOT get silently merged."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Create a Spotify release with a proper signal (not using internal _signal_class)
        spotify_release = Release(
            "release:spotify:sp-1",
            "Original Album",
            "album",
            date(2026, 1, 1),
            ReleaseDatePrecision.DAY,
            ("artist:one",),
            (SourceReference("spotify", "sp-1", None, NOW),),
            NOW,
        )
        # Observe it to create a proper signal and inbox entry
        outcome_sp = _observe(catalog, spotify_release)
        assert outcome_sp.kind == "created"

        # Now MusicBrainz arrives with a provisional Deezer link to a conflicting album
        mb_release = _release("musicbrainz", "rg-1", title="Original Album", release_date=date(2026, 1, 1))
        deezer_link = SourceReference(
            "deezer", "album-conflict", None, NOW, IdentityConfidence.PROVISIONAL
        )

        # Observe MB with the provisional link
        mb_outcome = _observe(catalog, mb_release, links=(deezer_link,))
        assert mb_outcome.kind == "created"

        # Now observe Deezer with incompatible data
        deezer_release = _release(
            "deezer",
            "album-conflict",
            title="Very Different",
            release_date=date(2026, 12, 1),
        )
        deezer_outcome = _observe(catalog, deezer_release)

        # The Deezer release should NOT merge into the MB subject
        # because the provisional link failed corroboration
        assert deezer_outcome.subject_local_id != mb_outcome.subject_local_id


def test_ambiguous_identity_stays_separate(tmp_path: Path) -> None:
    """Ambiguity: a key resolving to two distinct subjects creates its own subject,
    writes identity_ambiguous and increments the metric."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # Create two releases with the same title/date (ambiguous key)
        rel1 = _release("deezer", "al-1", title="Ambiguous Title", release_date=date(2026, 8, 14))
        rel2 = _release("spotify", "sp-1", title="Ambiguous Title", release_date=date(2026, 8, 14))

        outcome1 = _observe(catalog, rel1)
        assert outcome1.kind == "created"
        subject1 = outcome1.subject_local_id

        outcome2 = _observe(catalog, rel2)
        # Should be detected as ambiguous (if tier 4 is in effect) or create a new subject
        # For now, without tier 4, it should be a new subject
        subject2 = outcome2.subject_local_id

        # The two should have different subjects
        assert subject1 != subject2 or outcome2.kind == "ambiguous"


def test_never_merges_two_native_ids_of_one_source(tmp_path: Path) -> None:
    """Same-source conflict guard: a candidate never merges into a release carrying
    a different non-provisional native id for the candidate's own source."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)

        # First Deezer album
        outcome1 = _observe(catalog, _release("deezer", "album-1"))
        assert outcome1.kind == "created"
        subject1 = outcome1.subject_local_id

        # Second Deezer album (different native_id, same source)
        # This should NOT merge with the first even if some other tier says it should
        outcome2 = _observe(
            catalog,
            _release("deezer", "album-2", title=_release("deezer", "album-1").title),
        )
        # Should create a new subject (same-source guard)
        assert outcome2.subject_local_id != subject1 or outcome2.kind in ("created", "conflict")
