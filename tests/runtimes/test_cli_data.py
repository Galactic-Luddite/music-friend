"""Behavior tests for `music-friend data inbox duplicates`/`unmerge` (issue #64).

Replaces the retired `tests/runtimes/test_cli_dedupe_inbox.py` (PR #59's `data dedupe-inbox`).
"""

from __future__ import annotations

import io
import json
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
