"""Issue #62: the single release write path, exercised against a real catalog."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.configuration import LocalConfig
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
from music_friend.runtimes import cli
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.inbox_maintenance import merge_all_duplicate_pairs
from music_friend.tools.refresh import update_inbox_state
from music_friend.tools.release_observation import (
    content_version_of,
    record_release_observation,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
SOURCES = ("musicbrainz", "deezer")


class _ConfigStore:
    def load(self) -> object:
        return LocalConfig()

    def save(self, value: LocalConfig) -> None:
        raise AssertionError("the duplicates command never saves configuration")


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


# Issue #63: the release identity ladder.


def _link(source: str, native_id: str) -> SourceReference:
    return SourceReference(
        source,
        native_id,
        f"https://example.test/{source}/{native_id}",
        NOW,
        IdentityConfidence.PROVISIONAL,
    )


def _refs(catalog: Catalog, release_local_id: str) -> dict[tuple[str, str], IdentityConfidence]:
    stored = catalog.get_release(release_local_id)
    assert stored is not None
    return {(item.source, item.native_id): item.confidence for item in stored.source_refs}


def _subject(catalog: Catalog, release_local_id: str) -> str:
    stored = catalog.get_release(release_local_id)
    assert stored is not None
    return stored.subject_local_id or stored.local_id


def _entries(catalog: Catalog) -> int:
    return len(catalog.list_inbox_entries(None, limit=50))


def _discovered(catalog: Catalog, release: Release) -> Release:
    """Store a release the way release discovery does before its observation is recorded."""
    catalog.put_release(release)
    return release


def test_native_id_match_wins_before_title_key(tmp_path: Path) -> None:
    """Tier 1: a confirmed (source, native_id) decides, even when the report's new title
    now matches another stored release's title key exactly."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        first = _observe(catalog, _release("deezer", "al-1", title="Synthetic Record"))
        _observe(catalog, _release("spotify", "sp-9", title="Other Record"))

        outcome = _observe(
            catalog,
            _release("deezer", "al-1", title="Other Record"),
            at=NOW + timedelta(days=1),
        )

        assert outcome.method is IdentityMethod.NATIVE_ID  # type: ignore[attr-defined]
        assert outcome.release_local_id == "release:deezer:al-1"  # type: ignore[attr-defined]
        assert outcome.subject_local_id == first.subject_local_id  # type: ignore[attr-defined]
        assert outcome.kind == "updated"  # type: ignore[attr-defined]
        assert _refs(catalog, "release:spotify:sp-9") == {
            ("spotify", "sp-9"): IdentityConfidence.SOURCE_ONLY
        }
        assert _entries(catalog) == 2


@pytest.mark.parametrize("discovered_first", [False, True], ids=["direct", "discovered"])
def test_external_link_merges_in_either_arrival_order(
    tmp_path: Path, discovered_first: bool
) -> None:
    """MusicBrainz first: Deezer's own report hits the provisional reference, corroborates
    it (same title key and date), promotes it to EXTERNAL_ID and adds no item. Deezer first:
    MusicBrainz's link finds Deezer's confirmed reference at tier 2 and merges. Both hold
    whether or not discovery stored the release before its observation."""
    observe_new = _discovered if discovered_first else (lambda _catalog, release: release)
    with Catalog.open(tmp_path / "mb-first.sqlite3") as catalog:
        _artist(catalog)
        musicbrainz = _observe(
            catalog, _release("musicbrainz", "rg-1"), links=(_link("deezer", "al-1"),)
        )
        assert _refs(catalog, "release:musicbrainz:rg-1")[("deezer", "al-1")] is (
            IdentityConfidence.PROVISIONAL
        )

        deezer = _observe(
            catalog,
            observe_new(catalog, _release("deezer", "al-1", title="Synthetic  record")),
            at=NOW + timedelta(hours=1),
        )

        assert deezer.method is IdentityMethod.NATIVE_ID  # type: ignore[attr-defined]
        assert deezer.subject_local_id == musicbrainz.subject_local_id  # type: ignore[attr-defined]
        assert deezer.kind == "provenance_attached"  # type: ignore[attr-defined]
        assert not deezer.signal_created  # type: ignore[attr-defined]
        assert _refs(catalog, "release:musicbrainz:rg-1")[("deezer", "al-1")] is (
            IdentityConfidence.EXTERNAL_ID
        )
        assert _entries(catalog) == 1

    with Catalog.open(tmp_path / "deezer-first.sqlite3") as catalog:
        _artist(catalog)
        deezer = _observe(catalog, _release("deezer", "al-1"))

        musicbrainz = _observe(
            catalog,
            observe_new(catalog, _release("musicbrainz", "rg-1")),
            at=NOW + timedelta(hours=1),
            links=(_link("deezer", "al-1"), _link("spotify", "sp-1")),
        )

        assert musicbrainz.method is IdentityMethod.EXTERNAL_LINK  # type: ignore[attr-defined]
        assert musicbrainz.subject_local_id == deezer.subject_local_id  # type: ignore[attr-defined]
        assert musicbrainz.kind == "provenance_attached"  # type: ignore[attr-defined]
        assert _entries(catalog) == 1
        holder = musicbrainz.release_local_id  # type: ignore[attr-defined]
        # The empty Spotify slot is filled, but only provisionally.
        assert _refs(catalog, holder)[("spotify", "sp-1")] is IdentityConfidence.PROVISIONAL


@pytest.mark.parametrize("discovered_first", [False, True], ids=["direct", "discovered"])
def test_wrong_url_rel_is_detached_not_merged(tmp_path: Path, discovered_first: bool) -> None:
    """A MusicBrainz url-rel to a Deezer album whose own report disagrees on title and date:
    the provisional reference is detached, Deezer's release becomes its own subject and
    item, one identity_conflict is recorded, and nothing is hidden."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        musicbrainz = _observe(
            catalog,
            _release("musicbrainz", "rg-1", title="Album A", release_date=date(2026, 1, 1)),
            links=(_link("deezer", "al-9"),),
        )
        report = _release("deezer", "al-9", title="Different Album", release_date=date(2026, 6, 1))
        if discovered_first:
            _discovered(catalog, report)

        deezer = _observe(catalog, report, at=NOW + timedelta(hours=1))

        assert deezer.kind == "conflict"  # type: ignore[attr-defined]
        assert deezer.identity_conflicts == 1  # type: ignore[attr-defined]
        assert deezer.release_local_id == "release:deezer:al-9"  # type: ignore[attr-defined]
        assert deezer.subject_local_id != musicbrainz.subject_local_id  # type: ignore[attr-defined]
        assert ("deezer", "al-9") not in _refs(catalog, "release:musicbrainz:rg-1")
        assert _facts(catalog, "release:deezer:al-9").count(("identity_conflict", "deezer")) == 1
        assert _entries(catalog) == 2
        # A late-link pair is not opened: the ladder already decided these are different.
        assert catalog.list_open_release_identity_conflicts() == ()


@pytest.mark.parametrize(
    ("linked_title", "reported_title"),
    [
        pytest.param("Synthetic Record", "Synthetic Record (Deluxe Edition)", id="deluxe"),
        pytest.param("Song (Remixer A Remix)", "Song (Remixer B Remix)", id="two-remixers"),
        pytest.param("Song", "Song (Remixer A Remix)", id="original-vs-remix"),
    ],
)
def test_empty_slot_provisional_link_needs_title_key_and_date_corroboration(
    tmp_path: Path, linked_title: str, reported_title: str
) -> None:
    """Negative widening: a provisional link that filled an empty slot never counts for a
    report that is a different edition, remixer or remix, even on the same date."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        musicbrainz = _observe(
            catalog,
            _release("musicbrainz", "rg-1", title=linked_title),
            links=(_link("deezer", "al-1"),),
        )

        deezer = _observe(catalog, _release("deezer", "al-1", title=reported_title))

        assert deezer.kind == "conflict"  # type: ignore[attr-defined]
        assert deezer.subject_local_id != musicbrainz.subject_local_id  # type: ignore[attr-defined]
        assert _entries(catalog) == 2


def test_provisional_link_needs_the_same_date_and_a_shared_artist(tmp_path: Path) -> None:
    """Title alone never corroborates: two days apart, or an unrelated artist, stays apart;
    one day apart at day precision is the only date widening."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        catalog.put_artist(
            Artist(
                "artist:two",
                "Synthetic Artist Two",
                (SourceReference("spotify", "synthetic-two", None, NOW),),
                IdentityConfidence.SOURCE_ONLY,
                NOW,
            )
        )
        for native_id in ("rg-1", "rg-2", "rg-3"):
            _observe(
                catalog, _release("musicbrainz", native_id), links=(_link("deezer", native_id),)
            )

        two_days = _observe(catalog, _release("deezer", "rg-1", release_date=date(2026, 8, 16)))
        other_artist = _observe(
            catalog,
            replace(_release("deezer", "rg-2"), artist_refs=("artist:two",)),
        )
        one_day = _observe(catalog, _release("deezer", "rg-3", release_date=date(2026, 8, 15)))

        assert two_days.kind == "conflict"  # type: ignore[attr-defined]
        assert other_artist.kind == "conflict"  # type: ignore[attr-defined]
        assert one_day.kind == "provenance_attached"  # type: ignore[attr-defined]
        assert one_day.release_local_id == "release:musicbrainz:rg-3"  # type: ignore[attr-defined]


def test_late_link_between_existing_subjects_is_a_conflict_not_a_merge(tmp_path: Path) -> None:
    """A link naming a release that already has its own subject records identity_conflict,
    re-points nothing, and the pair appears in the ``data inbox duplicates`` dry run."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        deezer = _observe(catalog, _release("deezer", "al-1", title="Deezer Title"))
        musicbrainz = _observe(catalog, _release("musicbrainz", "rg-1", title="Brainz Title"))

        late = _observe(
            catalog,
            _release("musicbrainz", "rg-1", title="Brainz Title"),
            at=NOW + timedelta(days=1),
            links=(_link("deezer", "al-1"),),
        )

        assert late.identity_conflicts == 1  # type: ignore[attr-defined]
        assert late.subject_local_id == musicbrainz.subject_local_id  # type: ignore[attr-defined]
        assert _subject(catalog, "release:deezer:al-1") == deezer.subject_local_id  # type: ignore[attr-defined]
        assert ("deezer", "al-1") not in _refs(catalog, "release:musicbrainz:rg-1")
        assert ("identity_conflict", "musicbrainz") in _facts(catalog, "release:musicbrainz:rg-1")
        assert catalog.list_open_release_identity_conflicts() == (
            ("release:musicbrainz:rg-1", "release:deezer:al-1"),
        )
        # Recording the same late link again is idempotent.
        _observe(
            catalog,
            _release("musicbrainz", "rg-1", title="Brainz Title"),
            at=NOW + timedelta(days=2),
            links=(_link("deezer", "al-1"),),
        )
        assert len(catalog.list_open_release_identity_conflicts()) == 1

        application = MusicFriendApplication(catalog)
        before = _entries(catalog)
        stdout, stderr = io.StringIO(), io.StringIO()
        result = cli.run_cli(
            ["data", "inbox", "duplicates", "--json"],
            stdout=stdout,
            stderr=stderr,
            application=application,
            config_store=_ConfigStore(),
            secret_prompt=lambda _message: "",
        )

        assert result == 0, stderr.getvalue()
        payload = json.loads(stdout.getvalue())
        assert payload["conflicts"] == [
            {"release": "release:musicbrainz:rg-1", "other": "release:deezer:al-1"}
        ]
        assert [
            (item["tier"], {item["keep"]["release"], item["other"]["release"]})
            for item in payload["candidates"]
        ] == [("external_link", {"release:musicbrainz:rg-1", "release:deezer:al-1"})]
        assert _entries(catalog) == before
        assert application.count_open_identity_conflicts() == 1

        merged = cli.run_cli(
            ["data", "inbox", "duplicates", "--merge", "--yes", "--json"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            application=application,
            config_store=_ConfigStore(),
            secret_prompt=lambda _message: "",
        )
        assert merged == 0
        assert _entries(catalog) == 1
        assert application.count_open_identity_conflicts() == 0


def test_ambiguous_identity_stays_separate(tmp_path: Path) -> None:
    """A tier-1 key held by two distinct subjects: the report becomes its own subject and
    item, identity_ambiguous is recorded, and neither existing subject changes."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        shared = SourceReference("deezer", "al-shared", None, NOW)
        first = _observe(catalog, _release("spotify", "sp-1"))
        second = _observe(catalog, _release("spotify", "sp-2", title="Other Record"))
        for local_id in ("release:spotify:sp-1", "release:spotify:sp-2"):
            stored = catalog.get_release(local_id)
            assert stored is not None
            catalog.put_release(replace(stored, source_refs=(*stored.source_refs, shared)))

        outcome = _observe(catalog, _release("deezer", "al-shared"))

        assert outcome.kind == "ambiguous"  # type: ignore[attr-defined]
        assert outcome.subject_local_id not in {  # type: ignore[attr-defined]
            first.subject_local_id,  # type: ignore[attr-defined]
            second.subject_local_id,  # type: ignore[attr-defined]
        }
        assert ("identity_ambiguous", "deezer") in _facts(catalog, "release:deezer:al-shared")
        assert _entries(catalog) == 3


def test_tier_two_links_naming_two_subjects_are_a_conflict_not_a_pick(tmp_path: Path) -> None:
    """Exactly one or none at tier 2 as well: links resolving to two subjects merge nowhere."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        _observe(catalog, _release("deezer", "al-1"))
        _observe(catalog, _release("spotify", "sp-1", title="Other Record"))

        outcome = _observe(
            catalog,
            _release("musicbrainz", "rg-1"),
            links=(_link("deezer", "al-1"), _link("spotify", "sp-1")),
        )

        assert outcome.kind == "conflict"  # type: ignore[attr-defined]
        assert outcome.method is IdentityMethod.EXTERNAL_LINK  # type: ignore[attr-defined]
        assert _entries(catalog) == 3


def test_never_merges_two_native_ids_of_one_source(tmp_path: Path) -> None:
    """Same-source guard: a candidate never joins a subject that already holds another
    confirmed id of its own source, whatever tier 1 or tier 2 says."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        standard = _observe(catalog, _release("deezer", "al-1"))
        musicbrainz = _observe(
            catalog, _release("musicbrainz", "rg-1"), links=(_link("deezer", "al-1"),)
        )
        assert musicbrainz.subject_local_id == standard.subject_local_id  # type: ignore[attr-defined]

        # Tier 2: a second MusicBrainz release group linking the same Deezer album.
        second_group = _observe(
            catalog, _release("musicbrainz", "rg-2"), links=(_link("deezer", "al-1"),)
        )
        # Tier 1: a harvested link to a second Deezer album onto the joined subject.
        deluxe = _observe(
            catalog,
            _release("deezer", "al-2", title="Synthetic Record"),
            links=(_link("musicbrainz", "rg-1"),),
        )

        assert second_group.subject_local_id != standard.subject_local_id  # type: ignore[attr-defined]
        assert second_group.identity_conflicts == 1  # type: ignore[attr-defined]
        assert deluxe.subject_local_id != standard.subject_local_id  # type: ignore[attr-defined]
        assert ("deezer", "al-2") not in _refs(catalog, "release:deezer:al-1")


def test_same_title_from_unrelated_artists_never_joins_through_a_provisional_link(
    tmp_path: Path,
) -> None:
    """Negative widening: the same title and date from an unrelated artist stays separate."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        catalog.put_artist(
            Artist(
                "artist:two",
                "Synthetic Artist Two",
                (SourceReference("spotify", "synthetic-two", None, NOW),),
                IdentityConfidence.SOURCE_ONLY,
                NOW,
            )
        )
        musicbrainz = _observe(
            catalog, _release("musicbrainz", "rg-1"), links=(_link("deezer", "al-1"),)
        )
        stranger = replace(_release("deezer", "al-1"), artist_refs=("artist:two",))

        outcome = record_release_observation(
            catalog,
            ReleaseObservation(stranger, "deezer", "al-1", "artist:two", observed_at=NOW),
            release_sources=SOURCES,
        )

        assert outcome.subject_local_id != musicbrainz.subject_local_id  # type: ignore[attr-defined]
        assert _entries(catalog) == 2


def test_an_observation_cannot_claim_its_own_reference_provisionally(tmp_path: Path) -> None:
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        release = replace(_release("deezer", "al-1"), source_refs=(_link("deezer", "al-1"),))
        with pytest.raises(ValueError, match="cannot be provisional"):
            _observe(catalog, release)
        assert catalog.get_release(release.local_id) is None


def test_provisional_links_on_two_subjects_are_ambiguous_and_a_merge_confirms_them(
    tmp_path: Path,
) -> None:
    """Two release groups provisionally linking one album: the album's report joins neither.
    A reviewed ``data inbox duplicates`` merge then confirms the link it joined."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        _observe(catalog, _release("musicbrainz", "rg-1"), links=(_link("spotify", "sp-1"),))
        _observe(
            catalog,
            _release("musicbrainz", "rg-2", title="Other Record"),
            links=(_link("spotify", "sp-1"),),
        )

        outcome = _observe(catalog, _release("spotify", "sp-1"), at=NOW + timedelta(hours=1))

        assert outcome.kind == "ambiguous"  # type: ignore[attr-defined]
        assert _entries(catalog) == 3
        assert _refs(catalog, "release:musicbrainz:rg-1")[("spotify", "sp-1")] is (
            IdentityConfidence.PROVISIONAL
        )

        merged = merge_all_duplicate_pairs(MusicFriendApplication(catalog), now=NOW)

        assert len(merged) == 1
        assert _refs(catalog, "release:musicbrainz:rg-1")[("spotify", "sp-1")] is (
            IdentityConfidence.USER_CONFIRMED
        )
        assert _entries(catalog) == 2


def test_a_confirmed_holder_with_another_id_of_the_source_is_not_a_tier_one_match(
    tmp_path: Path,
) -> None:
    """Same-source guard at tier 1: a subject that already holds a different confirmed
    Deezer id never absorbs another Deezer album, even one it also carries."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        _artist(catalog)
        _observe(catalog, _release("deezer", "al-1"))
        stored = catalog.get_release("release:deezer:al-1")
        assert stored is not None
        catalog.put_release(
            replace(
                stored,
                source_refs=(*stored.source_refs, SourceReference("deezer", "al-2", None, NOW)),
            )
        )

        outcome = _observe(catalog, _release("deezer", "al-2"))

        assert outcome.method is IdentityMethod.NONE  # type: ignore[attr-defined]
        assert outcome.release_local_id == "release:deezer:al-2"  # type: ignore[attr-defined]
        assert _entries(catalog) == 2
