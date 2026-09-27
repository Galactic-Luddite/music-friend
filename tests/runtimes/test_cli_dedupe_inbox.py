"""Behavior tests for `music-friend data dedupe-inbox` (issue #56 part 2)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
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


def _seeded_application(
    tmp_path: Path,
    *,
    titles: tuple[str, ...],
    release_date: object = NOW.date(),
    artist_local_id: str = "artist-1",
) -> MusicFriendApplication:
    """A real Catalog-backed application with one inbox entry per title, all
    sharing the same artist and release date."""
    catalog = Catalog.open(tmp_path / "catalog.sqlite3")
    application = MusicFriendApplication(catalog)
    artist = _artist(artist_local_id)
    application.put_artist(artist)
    for index, title in enumerate(titles):
        release = Release(
            f"release-{index}",
            title,
            "album",
            release_date,
            ReleaseDatePrecision.DAY,
            (artist.local_id,),
            (SourceReference("synthetic", f"provider-release-{index}", None, NOW),),
            NOW,
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
            NOW,
        )
        application.put_signal(signal)
        application.put_inbox_entry(
            InboxEntry(f"inbox-{index}", signal.local_id, InboxState.UNREAD, NOW, NOW)
        )
    return application


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


def test_dedupe_inbox_dry_run_lists_a_typographic_duplicate_without_mutating(
    tmp_path: Path,
) -> None:
    """AC: dry-run by default -- a curly-vs-straight-apostrophe duplicate pair,
    same artist and date, is listed but neither row is mutated."""
    application = _seeded_application(tmp_path, titles=("I Won't Stop", "I Won’t Stop"))
    try:
        result, stdout, stderr = _run(["data", "dedupe-inbox", "--json"], application)
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["applied"] is False
        assert len(payload["candidates"]) == 1
        assert payload["candidates"][0] == {"keep": "inbox-0", "dismiss": "inbox-1"}

        # Dry run: neither row was mutated.
        assert application.get_inbox_entry("inbox-0").state is InboxState.UNREAD
        assert application.get_inbox_entry("inbox-1").state is InboxState.UNREAD
    finally:
        application.close()


def test_dedupe_inbox_apply_dismisses_the_extra_copy_reversibly(tmp_path: Path) -> None:
    """AC: only with --apply does the extra copy become `dismissed` -- the row is
    never deleted, so it can be reversed the same way any other dismiss decision
    can (`inbox show`, `update_inbox_item`)."""
    application = _seeded_application(tmp_path, titles=("I Won't Stop", "I Won’t Stop"))
    try:
        result, stdout, stderr = _run(["data", "dedupe-inbox", "--apply", "--json"], application)
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["applied"] is True
        assert payload["candidates"] == [{"keep": "inbox-0", "dismiss": "inbox-1"}]

        kept = application.get_inbox_entry("inbox-0")
        dismissed = application.get_inbox_entry("inbox-1")
        assert kept is not None and kept.state is InboxState.UNREAD
        # Reversible: the row still exists, only its state changed.
        assert dismissed is not None and dismissed.state is InboxState.DISMISSED
    finally:
        application.close()


def test_dedupe_inbox_leaves_titles_with_a_real_difference_untouched(tmp_path: Path) -> None:
    """Negative: titles differing by a real character ("Stop" vs. "Stops") never
    appear as a candidate pair."""
    application = _seeded_application(tmp_path, titles=("Stop", "Stops"))
    try:
        result, stdout, stderr = _run(["data", "dedupe-inbox", "--json"], application)
        assert result == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["candidates"] == []
        assert payload["applied"] is False
    finally:
        application.close()


def test_dedupe_inbox_text_output_reports_dry_run_and_applied_states(
    tmp_path: Path,
) -> None:
    """AC: plain-text output (no --json) states whether it was a dry run or an
    applied dismissal, and reports "no candidates" when there is nothing to do."""
    application = _seeded_application(tmp_path / "solo", titles=("Solo Release",))
    try:
        result, stdout, _stderr = _run(["data", "dedupe-inbox"], application)
        assert result == 0
        assert stdout == "Music Friend found no candidate duplicate inbox items.\n"
        # (print() adds exactly one trailing newline beyond the returned text)
    finally:
        application.close()

    application = _seeded_application(
        tmp_path / "duplicate", titles=("I Won't Stop", "I Won’t Stop")
    )
    try:
        dry_result, dry_stdout, _ = _run(["data", "dedupe-inbox"], application)
        assert dry_result == 0
        assert "Would dismiss" in dry_stdout
        assert "keep inbox-0 dismiss inbox-1" in dry_stdout
    finally:
        application.close()


def test_dedupe_inbox_rejects_unexpected_arguments(tmp_path: Path) -> None:
    application = _seeded_application(tmp_path, titles=("Solo Release",))
    try:
        result, _stdout, stderr = _run(["data", "dedupe-inbox", "--bogus"], application)
        assert result == 2
        assert stderr == cli._USAGE
    finally:
        application.close()
