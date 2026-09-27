"""Cross-subject duplicate release detection, merge, and CAS unmerge (issue D).

Implements ``music-friend data inbox duplicates [--merge --yes]`` and
``music-friend data inbox unmerge <merge_id> --yes`` (design doc section 3.6).

**Narrowed scope (issue #64 deviation).** The design doc's ``data inbox duplicates`` describes
listing pairs the full identity ladder (tiers 1-2 and 4) would join, plus recorded
``identity_conflict`` observations, and promoting involved ``PROVISIONAL`` source references to
``USER_CONFIRMED``. At the time this module was written, issue C's identity ladder (tiers 1-2,
``PROVISIONAL``/``USER_CONFIRMED`` confidence, and the ``observations``/``identity_conflict``
table) had not been implemented -- ``IdentityConfidence`` has no ``PROVISIONAL`` member and no
``observations`` table exists. This module therefore detects duplicates the way PR #59's retired
``dedupe-inbox`` did (folded title + exact artist set + exact release date), but operating on
distinct ``releases.subject_local_id`` values rather than raw inbox rows, and reports every pair
under one tier label, ``"title_key"``. It never promotes a source reference (there is nothing yet
to promote) and never closes an ``identity_conflict`` observation (none can exist yet). When
issue C lands, ``find_duplicate_pairs`` should be extended to also consult the tier 1/2 lookups
and to surface open conflicts; the merge/unmerge mechanics below (subject repoint, snapshot,
CAS) do not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from music_friend.domain import InboxEntry, InboxState, Release, Signal, SignalKind
from music_friend.store.catalog import InboxEntrySnapshotRecord
from music_friend.tools.application import MusicFriendApplication
from music_friend.tools.release_discovery import _normalized_title

#: The only detection tier currently implemented; see the module docstring's narrowing note.
TIER_TITLE_KEY = "title_key"


def _subject_of(release: Release) -> str:
    return release.subject_local_id or release.local_id


@dataclass(frozen=True, slots=True)
class DuplicatePair:
    """One cross-subject duplicate candidate, ready for listing or merging."""

    tier: str
    keep_release: Release
    other_release: Release
    keep_state: str | None
    other_state: str | None

    @property
    def keep_subject_local_id(self) -> str:
        return _subject_of(self.keep_release)

    @property
    def other_subject_local_id(self) -> str:
        return _subject_of(self.other_release)


def find_duplicate_pairs(application: MusicFriendApplication) -> tuple[DuplicatePair, ...]:
    """Find release pairs in distinct subjects sharing a folded title, artist set, and date.

    Groups every stored release by ``(folded title, artist set, release date)``; within a group
    of two or more distinct subjects, pairs the earliest-observed release (the "keep" side, the
    one whose subject the merge re-points onto) with each later, still-distinct-subject release.
    A release whose subject already equals the keeper's (already merged, or the migration
    already collapsed it) is never re-paired, so a second call after a merge finds nothing --
    the idempotency the CLI's ``--merge`` AC relies on.
    """
    groups: dict[tuple[str, frozenset[str], str], list[Release]] = {}
    for release in application._catalog.list_releases():
        key = (
            _normalized_title(release.title),
            frozenset(release.artist_refs),
            release.release_date.isoformat(),
        )
        groups.setdefault(key, []).append(release)
    pairs: list[DuplicatePair] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda item: (item.observed_at, item.local_id))
        keep = ordered[0]
        seen_subjects = {_subject_of(keep)}
        for other in ordered[1:]:
            other_subject = _subject_of(other)
            if other_subject in seen_subjects:
                continue
            seen_subjects.add(other_subject)
            keep_entry = application._catalog.get_inbox_entry_for_subject(
                SignalKind.RELEASE, _subject_of(keep)
            )
            other_entry = application._catalog.get_inbox_entry_for_subject(
                SignalKind.RELEASE, other_subject
            )
            pairs.append(
                DuplicatePair(
                    tier=TIER_TITLE_KEY,
                    keep_release=keep,
                    other_release=other,
                    keep_state=keep_entry.state.value if keep_entry is not None else None,
                    other_state=other_entry.state.value if other_entry is not None else None,
                )
            )
    return tuple(pairs)


def _entry_rank_key(entry: InboxEntry) -> tuple[int, float, str]:
    """Lower sorts first: decided beats unread; among decided, most-recent wins; ties by id."""
    decided_rank = 1 if entry.state is InboxState.UNREAD else 0
    return (decided_rank, -entry.updated_at.timestamp(), entry.local_id)


def _rank_entries(a: InboxEntry, b: InboxEntry) -> tuple[InboxEntry, InboxEntry]:
    """Return ``(winner, loser)`` per the migration's most-decided-wins rule."""
    if _entry_rank_key(a) <= _entry_rank_key(b):
        return a, b
    return b, a


def _head_signal_id(signal_a: Signal | None, signal_b: Signal | None) -> str:
    """Greatest ``observed_at`` wins; ties break on the smaller ``local_id``."""
    candidates = [signal for signal in (signal_a, signal_b) if signal is not None]
    if not candidates:
        raise ValueError("at least one signal is required")
    return min(
        candidates, key=lambda signal: (-signal.observed_at.timestamp(), signal.local_id)
    ).local_id


def merge_duplicate_pair(
    application: MusicFriendApplication, pair: DuplicatePair, *, now: datetime
) -> str:
    """Merge one duplicate pair: re-point subjects, collapse inbox entries, snapshot, audit.

    Returns the generated ``merge_id``. Idempotent: if the pair's subjects already match (a
    previous call in the same batch already merged them), this only records nothing further.
    """
    catalog = application._catalog
    merge_id = f"merge:{uuid4()}"
    keep_subject = pair.keep_subject_local_id
    other_subject = pair.other_subject_local_id
    if keep_subject == other_subject:
        return merge_id

    for release_local_id in catalog.list_release_local_ids_for_subject(other_subject):
        catalog.repoint_release_subject(
            release_local_id, keep_subject, merge_id=merge_id, merged_at=now
        )

    keep_entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, keep_subject)
    other_entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, other_subject)

    if keep_entry is None and other_entry is None:
        return merge_id
    if keep_entry is None:
        # Only the losing subject carried an inbox entry: move it onto the keeper's subject.
        assert other_entry is not None
        catalog.delete_inbox_entry(other_entry.local_id)
        catalog.insert_inbox_entry_row(
            InboxEntry(
                other_entry.local_id,
                other_entry.kind,
                keep_subject,
                other_entry.latest_signal_local_id,
                other_entry.state,
                other_entry.created_at,
                now,
            )
        )
        return merge_id
    if other_entry is None:
        # The keeper's entry already lives on the keeper's subject; nothing to collapse.
        return merge_id

    winner, loser = _rank_entries(keep_entry, other_entry)
    winner_signal = catalog.get_signal(winner.latest_signal_local_id)
    loser_signal = catalog.get_signal(loser.latest_signal_local_id)
    head_signal_local_id = _head_signal_id(winner_signal, loser_signal)

    for entry, role in ((winner, "winner_before"), (loser, "loser")):
        catalog.put_inbox_entry_snapshot(
            merge_id=merge_id,
            role=role,
            local_id=entry.local_id,
            kind=entry.kind,
            subject_local_id=entry.subject_local_id,
            signal_local_id=entry.latest_signal_local_id,
            state=entry.state,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            winner_local_id=winner.local_id,
            winner_updated_at_after=now,
            merged_at=now,
        )

    catalog.delete_inbox_entry(loser.local_id)
    catalog.put_inbox_entry(
        InboxEntry(
            winner.local_id,
            winner.kind,
            keep_subject,
            head_signal_local_id,
            winner.state,
            min(winner.created_at, loser.created_at),
            now,
        )
    )
    return merge_id


def merge_all_duplicate_pairs(
    application: MusicFriendApplication, *, now: datetime
) -> tuple[str, ...]:
    """Merge every currently-detected duplicate pair; returns the generated ``merge_id``s."""
    merge_ids: list[str] = []
    for pair in find_duplicate_pairs(application):
        merge_ids.append(merge_duplicate_pair(application, pair, now=now))
    return tuple(merge_ids)


def _restore_snapshot_row(snapshot: InboxEntrySnapshotRecord) -> InboxEntry:
    return InboxEntry(
        snapshot.local_id,
        snapshot.kind,
        snapshot.subject_local_id,
        snapshot.signal_local_id,
        snapshot.state,
        snapshot.created_at,
        snapshot.updated_at,
    )


def unmerge(application: MusicFriendApplication, merge_id: str) -> tuple[bool, str]:
    """Compare-and-swap recovery for one merge. Returns ``(restored, message)``.

    A ``merge_id`` can carry more than one independent winner: migration 014 shares one
    ``merge_id`` ('014') across every same-subject collapse it performed, each with its own
    winner. The CAS check therefore runs per winner, not once for the whole merge_id -- a
    change to one winner's entry since the merge skips only that winner's restore and reports
    why, while every other winner whose entry is untouched still restores. (A CLI ``--merge``
    always produces exactly one winner per merge_id, so this degenerates to the single-winner
    case described in the module docstring.) A merge that only re-pointed subjects (no inbox
    collapse -- one side had no entry) has nothing to CAS on and always restores.
    """
    catalog = application._catalog
    snapshots = catalog.list_inbox_entry_snapshots(merge_id)
    subject_merges = catalog.list_subject_merges(merge_id)
    if not snapshots and not subject_merges:
        return False, f"No merge found with id {merge_id!r}."

    by_winner: dict[str, list[InboxEntrySnapshotRecord]] = {}
    for snapshot in snapshots:
        by_winner.setdefault(snapshot.winner_local_id, []).append(snapshot)

    if not by_winner:
        # Pure subject re-pointing, no inbox collapse to CAS on.
        catalog.revert_subject_merges(merge_id)
        return True, f"Merge {merge_id} was unmerged."

    restored_any = False
    skip_reasons: list[str] = []
    for winner_local_id, winner_snapshots in by_winner.items():
        winner_before = next(s for s in winner_snapshots if s.role == "winner_before")
        recorded_updated_at = winner_before.winner_updated_at_after
        current_winner = catalog.get_inbox_entry(winner_local_id)
        if current_winner is None:
            skip_reasons.append(
                f"winner inbox entry {winner_local_id} no longer exists; skipping restore"
            )
            continue
        if current_winner.updated_at != recorded_updated_at:
            skip_reasons.append(
                f"winner {winner_local_id} was changed since the merge (updated_at "
                f"{current_winner.updated_at.isoformat()} != recorded "
                f"{recorded_updated_at.isoformat()}); skipping restore to keep the newer "
                "decision"
            )
            continue
        for snapshot in winner_snapshots:
            restored = _restore_snapshot_row(snapshot)
            if snapshot.role == "winner_before":
                catalog.put_inbox_entry(restored)
            elif subject_merges:
                # A migration-collapsed loser (merge_id '014') has no subject_merges row, so
                # its subject already has the winner's row and a second insert would violate
                # the UNIQUE(kind, subject_local_id) constraint; report, don't re-insert.
                catalog.insert_inbox_entry_row(restored)
        restored_any = True

    if subject_merges and restored_any:
        # A CLI merge_id has exactly one winner, so any restore here is the whole merge's.
        catalog.revert_subject_merges(merge_id)

    if restored_any and not skip_reasons:
        return True, f"Merge {merge_id} was unmerged."
    if restored_any:
        return True, f"Merge {merge_id} was partially unmerged: " + "; ".join(skip_reasons)
    return False, f"Merge {merge_id}: " + "; ".join(skip_reasons)
