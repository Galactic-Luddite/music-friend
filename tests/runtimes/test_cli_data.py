"""Behavior tests for `music-friend data inbox duplicates`/`unmerge` (issue #64).

Replaces the retired `tests/runtimes/test_cli_dedupe_inbox.py` (PR #59's `data dedupe-inbox`).
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    Artist,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxEntry,
    InboxState,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceReference,
)
from music_friend.runtimes import cli
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import update_inbox_state

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


class _ConfigStore:
    def __init__(self, value: object = LocalConfig()) -> None:
        self.value = value

    def load(self) -> object:
        return self.value

    def save(self, value: LocalConfig) -> None:
        self.value = value


def _artist(local_id: str = "artist-1") -> Artist:
    return Artist(
        local_id,
        "Artist One",
        (SourceReference("synthetic", f"provider-{local_id}", None, NOW),),
        IdentityConfidence.SOURCE_ONLY,
        NOW,
    )


def _seed_duplicate_pair(
    application: MusicFriendApplication,
    *,
    titles: tuple[str, str] = ("I Won't Stop", "I Won’t Stop"),
    observed_ats: tuple[datetime, datetime] = (NOW, NOW + timedelta(minutes=5)),
    states: tuple[InboxState, InboxState] = (InboxState.UNREAD, InboxState.UNREAD),
    updated_ats: tuple[datetime, datetime] | None = None,
) -> None:
    """Seed two releases with a shared artist and date, each its own subject, and
    one release-kind inbox entry per subject -- the migration-014 shape a cross-source or
    pre-fold duplicate ends up in."""
    artist = _artist()
    application.put_artist(artist)
    updated = updated_ats or observed_ats
    for index, (title, observed_at, state, updated_at) in enumerate(
        zip(titles, observed_ats, states, updated, strict=True)
    ):
        release = Release(
            f"release-{index}",
            title,
            "album",
            NOW.date(),
            ReleaseDatePrecision.DAY,
            (artist.local_id,),
            (SourceReference("synthetic", f"provider-release-{index}", None, observed_at),),
            observed_at,
        )
        application.put_release(release)
        signal = Signal(
            f"signal-{index}",
            SignalKind.RELEASE,
            release.local_id,
            "synthetic",
            f"provider-release-{index}",
            f"fingerprint-{index}",
            f"material-{index}",
            Explanation((ExplanationReason(ExplanationReasonKind.NEW_RELEASE, title),)),
            observed_at,
        )
        application.put_signal(signal)
        application.put_inbox_entry(
            InboxEntry(
                f"inbox-{index}",
                SignalKind.RELEASE,
                release.local_id,  # each release defaults to its own subject
                signal.local_id,
                state,
                observed_at,
                updated_at,
            )
        )


def _application(tmp_path: Path) -> MusicFriendApplication:
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    return MusicFriendApplication(catalog)


def _run(argv: list[str], application: MusicFriendApplication) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = cli.run_cli(
        argv,
        stdout=stdout,
        stderr=stderr,
        application=application,
        config_store=_ConfigStore(),
        secret_prompt=lambda _message: "",
    )
    return result, stdout.getvalue(), stderr.getvalue()


def test_inbox_duplicates_dry_run_writes_nothing(tmp_path: Path) -> None:
    """AC: dry-run by default, no network request, lists the cross-subject pair with
    tier/titles/both inbox states plus the (currently always-empty) open-conflicts list,
    and mutates nothing."""
    application = _application(tmp_path)
    try:
        _seed_duplicate_pair(application)
        release_0_before = application.get_release("release-0")
        release_1_before = application.get_release("release-1")

        result, stdout, stderr = _run(["data", "inbox", "duplicates", "--json"], application)
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["conflicts"] == []
        assert len(payload["candidates"]) == 1
        candidate = payload["candidates"][0]
        assert candidate["tier"] == "title_key"
        assert candidate["keep"]["release"] == "release-0"
        assert candidate["other"]["release"] == "release-1"
        assert candidate["keep"]["title"] == "I Won't Stop"
        assert candidate["other"]["title"] == "I Won’t Stop"
        assert candidate["keep"]["inbox_state"] == "unread"
        assert candidate["other"]["inbox_state"] == "unread"

        # Writes nothing: both releases keep their original (distinct) subjects, and both
        # inbox rows are untouched.
        release_0_after = application.get_release("release-0")
        release_1_after = application.get_release("release-1")
        assert release_0_after.subject_local_id == release_0_before.subject_local_id
        assert release_1_after.subject_local_id == release_1_before.subject_local_id
        assert release_0_after.subject_local_id != release_1_after.subject_local_id
        assert application.get_inbox_entry("inbox-0").state is InboxState.UNREAD
        assert application.get_inbox_entry("inbox-1").state is InboxState.UNREAD
    finally:
        application.close()


def test_inbox_duplicates_merge_is_idempotent_and_audited(tmp_path: Path) -> None:
    """AC: --merge --yes re-points the later release's subject, collapses the inbox
    entries most-decided-wins with the head-signal rule, writes subject_merges and both
    snapshot roles with winner_updated_at_after; a second run lists nothing."""
    application = _application(tmp_path)
    try:
        # release-1 is "later" (observed 5 minutes after release-0) but its inbox entry is
        # SAVED (decided) while release-0's stays UNREAD, so the winner (most-decided) is
        # inbox-1 even though its release's subject is the one that gets re-pointed away.
        _seed_duplicate_pair(
            application,
            states=(InboxState.UNREAD, InboxState.SAVED),
        )
        release_0 = application.get_release("release-0")
        release_1 = application.get_release("release-1")
        keep_subject = release_0.subject_local_id
        other_subject = release_1.subject_local_id
        assert keep_subject != other_subject

        result, stdout, stderr = _run(
            ["data", "inbox", "duplicates", "--merge", "--yes", "--json"], application
        )
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert len(payload["merged"]) == 1
        merge_id = payload["merged"][0]

        # Both releases now share the earlier release's subject.
        assert application.get_release("release-0").subject_local_id == keep_subject
        assert application.get_release("release-1").subject_local_id == keep_subject

        # subject_merges recorded the move.
        catalog = application._catalog
        moves = catalog.list_subject_merges(merge_id)
        assert (("release-1", other_subject, keep_subject)) in moves

        # Both snapshot roles were written, sharing one winner_updated_at_after (the CAS token).
        snapshots = catalog.list_inbox_entry_snapshots(merge_id)
        roles = {snapshot.role for snapshot in snapshots}
        assert roles == {"winner_before", "loser"}
        tokens = {snapshot.winner_updated_at_after for snapshot in snapshots}
        assert len(tokens) == 1
        winner_snapshot = next(s for s in snapshots if s.role == "winner_before")
        assert winner_snapshot.winner_local_id == "inbox-1"
        assert winner_snapshot.local_id == "inbox-1"
        loser_snapshot = next(s for s in snapshots if s.role == "loser")
        assert loser_snapshot.local_id == "inbox-0"

        # Exactly one inbox entry remains: the most-decided winner, now on the keeper subject.
        surviving = application.get_inbox_entry("inbox-1")
        assert surviving is not None
        assert surviving.state is InboxState.SAVED
        assert surviving.subject_local_id == keep_subject
        assert application.get_inbox_entry("inbox-0") is None

        # Idempotent: a second dry-run call lists nothing, since both releases now share a
        # subject.
        second_result, second_stdout, _ = _run(
            ["data", "inbox", "duplicates", "--json"], application
        )
        assert second_result == 0
        assert json.loads(second_stdout)["candidates"] == []
    finally:
        application.close()


def test_inbox_unmerge_is_compare_and_swap(tmp_path: Path) -> None:
    """AC: unmerge restores winner-before state, head signal, timestamps, and re-points
    releases when the winner's updated_at still equals winner_updated_at_after; when the
    user changed the winner after the merge, it skips that winner, leaves the newer decision
    intact, and prints the reason."""
    # Case 1: nothing touched the winner after the merge -- unmerge restores everything.
    application = _application(tmp_path / "restorable")
    try:
        _seed_duplicate_pair(application, states=(InboxState.UNREAD, InboxState.SAVED))
        release_0 = application.get_release("release-0")
        release_1 = application.get_release("release-1")
        keep_subject = release_0.subject_local_id
        other_subject = release_1.subject_local_id

        merge_result, merge_stdout, _ = _run(
            ["data", "inbox", "duplicates", "--merge", "--yes", "--json"], application
        )
        assert merge_result == 0
        merge_id = json.loads(merge_stdout)["merged"][0]

        unmerge_result, unmerge_stdout, unmerge_stderr = _run(
            ["data", "inbox", "unmerge", merge_id, "--yes", "--json"], application
        )
        assert unmerge_result == 0
        assert unmerge_stderr == ""
        payload = json.loads(unmerge_stdout)
        assert payload["restored"] is True

        # Releases are back on their original, distinct subjects.
        assert application.get_release("release-0").subject_local_id == keep_subject
        assert application.get_release("release-1").subject_local_id == other_subject

        # Both inbox rows exist again with their pre-merge state, signal, and timestamps.
        restored_winner = application.get_inbox_entry("inbox-1")
        restored_loser = application.get_inbox_entry("inbox-0")
        assert restored_winner is not None and restored_winner.state is InboxState.SAVED
        assert restored_winner.subject_local_id == other_subject
        assert restored_winner.latest_signal_local_id == "signal-1"
        assert restored_loser is not None and restored_loser.state is InboxState.UNREAD
        assert restored_loser.subject_local_id == keep_subject
        assert restored_loser.latest_signal_local_id == "signal-0"
    finally:
        application.close()

    # Case 2: the user makes a new decision on the winner after the merge -- unmerge must
    # refuse, leaving the newer decision (and the merged state) intact.
    application = _application(tmp_path / "cas-conflict")
    try:
        _seed_duplicate_pair(application, states=(InboxState.UNREAD, InboxState.SAVED))
        merge_result, merge_stdout, _ = _run(
            ["data", "inbox", "duplicates", "--merge", "--yes", "--json"], application
        )
        assert merge_result == 0
        merge_id = json.loads(merge_stdout)["merged"][0]

        surviving_before = application.get_inbox_entry("inbox-1")
        assert surviving_before is not None
        # The user dismisses the merged item -- a new decision made after the merge.
        update_inbox_state(
            application,
            "inbox-1",
            InboxState.DISMISSED,
            updated_at=NOW + timedelta(hours=1),
        )

        unmerge_result, unmerge_stdout, unmerge_stderr = _run(
            ["data", "inbox", "unmerge", merge_id, "--yes", "--json"], application
        )
        assert unmerge_result == 0
        assert unmerge_stderr == ""
        payload = json.loads(unmerge_stdout)
        assert payload["restored"] is False
        assert "inbox-1" in payload["message"]
        assert "changed" in payload["message"].lower()

        # The newer decision (dismissed) is untouched, and the merge is not reverted.
        still_dismissed = application.get_inbox_entry("inbox-1")
        assert still_dismissed is not None
        assert still_dismissed.state is InboxState.DISMISSED
        assert application.get_inbox_entry("inbox-0") is None
        assert (
            application.get_release("release-0").subject_local_id
            == application.get_release("release-1").subject_local_id
        )
    finally:
        application.close()


def test_inbox_unmerge_reports_an_unknown_merge_id(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        result, stdout, stderr = _run(
            ["data", "inbox", "unmerge", "merge:does-not-exist", "--yes", "--json"], application
        )
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["restored"] is False
        assert "No merge found" in payload["message"]
    finally:
        application.close()


def test_inbox_duplicates_rejects_unexpected_arguments(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        result, _stdout, stderr = _run(["data", "inbox", "duplicates", "--bogus"], application)
        assert result == 2
        assert stderr == cli._USAGE
    finally:
        application.close()


def test_dedupe_inbox_command_is_retired(tmp_path: Path) -> None:
    """AC: dedupe-inbox is removed -- the CLI now treats it as an unrecognized command."""
    application = _application(tmp_path)
    try:
        result, _stdout, stderr = _run(["data", "dedupe-inbox"], application)
        assert result == 2
        assert stderr == cli._USAGE
        assert "dedupe-inbox" not in cli._USAGE
    finally:
        application.close()


_V13_DUPLICATE_INBOX_FIXTURE = (
    Path(__file__).parent.parent / "store" / "fixtures" / "v13_duplicate_inbox.sql"
)


def _load_v13_fixture(path: Path) -> None:
    """Load the real pre-014 fixture (also used by tests/store/test_migrations.py) so
    Catalog.open runs migration 014 against genuine migration input, not a hand-built shape.

    The fixture carries no ``artists``/``release_artists`` rows -- it exists purely to exercise
    the migration's inbox-collapse SQL, and pre-014 ``releases`` never needed an artist. A real
    catalog can never reach that shape (``Release.__post_init__`` requires a non-empty
    ``artist_refs``, enforced before ``put_release`` ever writes a row), so a CLI command that
    reads releases back out -- as ``data inbox duplicates`` does -- cannot run against the
    fixture verbatim. This adds one artist and links it to each fixture release, which is the
    minimum needed to make the fixture a valid catalog; it changes nothing the migration or its
    snapshots read or write.
    """
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_V13_DUPLICATE_INBOX_FIXTURE.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO artists (local_id, display_name, identity_confidence, observed_at) "
            "VALUES ('artist-fixture', 'Fixture Artist', 'source_only', "
            "'2026-01-01T00:00:00+00:00')"
        )
        connection.executemany(
            "INSERT INTO release_artists (release_id, artist_id, position) VALUES (?, ?, 0)",
            [
                ("release-a", "artist-fixture"),
                ("release-b", "artist-fixture"),
                ("release-d", "artist-fixture"),
            ],
        )


def test_cli_against_rows_migration_014_actually_produced(tmp_path: Path) -> None:
    """Integration test against migration 014's real output, not a hand-mirrored shape.

    Loads the genuine pre-014 fixture (three same-subject collapse shapes: (a) saved beats
    unread, (b) a later saved beats an earlier dismissed, (c) two unread entries where the
    more-recently-updated one wins -- plus (d) a lone entry the migration never touches),
    opens it so migration 014 actually runs and writes its own inbox_entry_snapshots, then
    drives the CLI: `data inbox duplicates` (dry run, expects no cross-subject candidates --
    the fixture's releases have distinct titles), `--merge --yes` (nothing to merge), and
    `data inbox unmerge 014 --yes` -- exercising the CAS-per-winner path this merge_id
    requires, since migration 014 shares one merge_id across three independent winners.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _load_v13_fixture(catalog_path)

    catalog = Catalog.open(catalog_path)
    application = MusicFriendApplication(catalog)
    try:
        # Migration 014 ran: confirm its documented collapse (mirrors
        # tests/store/test_migrations.py::test_014_collapses_duplicate_inbox_entries_most_decided_wins).
        winner_a = application.get_inbox_entry("entry-a-saved")
        assert winner_a is not None
        assert winner_a.state is InboxState.SAVED
        assert winner_a.latest_signal_local_id == "signal-a2"
        assert application.get_inbox_entry("entry-a-unread") is None

        winner_b = application.get_inbox_entry("entry-b-saved")
        assert winner_b is not None
        assert winner_b.state is InboxState.SAVED
        assert winner_b.latest_signal_local_id == "signal-b2"
        assert winner_b.created_at == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        assert application.get_inbox_entry("entry-b-dismissed") is None

        winner_c = application.get_inbox_entry("entry-c2")
        assert winner_c is not None
        assert winner_c.state is InboxState.UNREAD
        assert winner_c.latest_signal_local_id == "signal-c2"
        assert winner_c.created_at == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        assert application.get_inbox_entry("entry-c1") is None

        untouched_d = application.get_inbox_entry("entry-d")
        assert untouched_d is not None
        assert untouched_d.state is InboxState.SAVED

        # `data inbox duplicates` against real post-migration state: the fixture's three
        # releases (Case A/B/D Release) have distinct titles, so no cross-subject candidate
        # exists -- a genuine negative control, not an assumption.
        dry_result, dry_stdout, dry_stderr = _run(
            ["data", "inbox", "duplicates", "--json"], application
        )
        assert dry_result == 0, dry_stderr
        assert dry_stderr == ""
        assert json.loads(dry_stdout)["candidates"] == []

        merge_result, merge_stdout, _ = _run(
            ["data", "inbox", "duplicates", "--merge", "--yes", "--json"], application
        )
        assert merge_result == 0
        assert json.loads(merge_stdout)["merged"] == []

        # A user decision lands on winner (a) *after* migration 014 ran -- this must not be
        # clobbered by unmerging (b) and (c), even though all three share merge_id '014'.
        update_inbox_state(
            application,
            "entry-a-saved",
            InboxState.DISMISSED,
            updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

        unmerge_result, unmerge_stdout, unmerge_stderr = _run(
            ["data", "inbox", "unmerge", "014", "--yes", "--json"], application
        )
        assert unmerge_result == 0
        assert unmerge_stderr == ""
        payload = json.loads(unmerge_stdout)
        assert payload["restored"] is True
        assert "entry-a-saved" in payload["message"]
        assert "changed" in payload["message"].lower()

        # (a): the newer decision is untouched -- not reverted to its pre-migration state.
        still_dismissed = application.get_inbox_entry("entry-a-saved")
        assert still_dismissed is not None
        assert still_dismissed.state is InboxState.DISMISSED
        assert still_dismissed.latest_signal_local_id == "signal-a2"
        assert application.get_inbox_entry("entry-a-unread") is None

        # (b) and (c) restore to the *original fixture* rows exactly -- local_id, signal,
        # state, and both timestamps -- literal values transcribed from the fixture's own
        # INSERT statements (tests/store/fixtures/v13_duplicate_inbox.sql), which migration
        # 014 changed on collapse (created_at moved to the group minimum) and unmerge must
        # move back.
        restored_b = application.get_inbox_entry("entry-b-saved")
        assert restored_b is not None
        assert restored_b.state is InboxState.SAVED
        assert restored_b.latest_signal_local_id == "signal-b2"
        assert restored_b.created_at == datetime(2026, 1, 3, 1, tzinfo=timezone.utc)
        assert restored_b.updated_at == datetime(2026, 1, 3, 4, tzinfo=timezone.utc)
        # The loser is reported by its snapshot, never re-inserted (design: a migration
        # collapse has no subject_merges row, so a second row on the same subject would
        # violate UNIQUE(kind, subject_local_id)).
        assert application.get_inbox_entry("entry-b-dismissed") is None

        restored_c = application.get_inbox_entry("entry-c2")
        assert restored_c is not None
        assert restored_c.state is InboxState.UNREAD
        assert restored_c.latest_signal_local_id == "signal-c2"
        assert restored_c.created_at == datetime(2026, 1, 4, 1, tzinfo=timezone.utc)
        assert restored_c.updated_at == datetime(2026, 1, 4, 5, tzinfo=timezone.utc)
        assert application.get_inbox_entry("entry-c1") is None

        # (d) was never part of any collapse or snapshot: untouched throughout.
        final_d = application.get_inbox_entry("entry-d")
        assert final_d is not None
        assert final_d.state is InboxState.SAVED
        assert final_d.latest_signal_local_id == "signal-d1"
    finally:
        application.close()
