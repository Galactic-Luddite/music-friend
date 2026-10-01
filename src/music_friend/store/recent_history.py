"""Durable, precision-preserving recently-played evidence and sync state."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from music_friend.domain import RecentPlay, canonical_history_timestamp, history_request_after_ms
from music_friend.store.catalog import Catalog


@dataclass(frozen=True, slots=True)
class StoredRecentPlay:
    provider: str
    observation_key: str
    played_at: str
    track_uri: str
    track_name: str
    primary_artist_name: str
    album_name: str
    context_uri: str | None


@dataclass(frozen=True, slots=True)
class HistorySyncState:
    provider: str
    last_attempt_at: datetime
    last_successful_check_at: datetime | None
    needs_repair: bool
    newest_observed_played_at: str | None
    requested_after_ms: int | None
    attempt_outcome: str
    interval_completeness: str
    coverage_reason: str


@dataclass(frozen=True, slots=True)
class IncompleteInterval:
    lower: str | None
    upper: str | None
    reason: str


canonical_timestamp = canonical_history_timestamp
request_after_ms = history_request_after_ms


class RecentHistoryStore:
    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    @property
    def _connection(self) -> sqlite3.Connection:
        return self._catalog._require_connection()

    def begin_attempt(
        self, provider: str, attempted_at: datetime, *, requested_after_ms: int | None
    ) -> None:
        with self._catalog.transaction():
            self._connection.execute(
                """INSERT INTO history_sync_state(provider,last_attempt_at,last_successful_check_at,needs_repair,newest_played_at_seconds,newest_played_at_fraction,requested_after_ms,attempt_outcome,interval_completeness,coverage_reason)
                VALUES(?,?,NULL,1,NULL,NULL,?,'running','unknown','running')
                ON CONFLICT(provider) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,needs_repair=1,requested_after_ms=excluded.requested_after_ms,attempt_outcome='running',coverage_reason='running'""",
                (provider, attempted_at.astimezone(timezone.utc).isoformat(), requested_after_ms),
            )

    def commit_page(
        self,
        provider: str,
        plays: tuple[RecentPlay, ...],
        *,
        attempted_at: datetime,
        requested_after_ms: int | None,
        reason: str,
    ) -> int:
        inserted = 0
        newest: tuple[int, str] | None = None
        with self._catalog.transaction():
            for play in plays:
                seconds, fraction, canonical = canonical_timestamp(play.played_at)
                key = hashlib.sha256((canonical + "\0" + play.track_uri).encode()).hexdigest()
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO recent_play_observations(provider,observation_key,played_at_seconds,played_at_fraction,track_uri,track_name,primary_artist_name,album_name,context_uri,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        provider,
                        key,
                        seconds,
                        fraction,
                        play.track_uri,
                        play.track_name,
                        play.primary_artist_name,
                        play.album_name,
                        play.context_uri,
                        attempted_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
                inserted += cursor.rowcount
                if newest is None or (seconds, fraction.ljust(18, "0")) > (
                    newest[0],
                    newest[1].ljust(18, "0"),
                ):
                    newest = (seconds, fraction)
            if newest is not None:
                state = self.get_state(provider)
                old = (
                    canonical_timestamp(state.newest_observed_played_at)[:2]
                    if state and state.newest_observed_played_at
                    else None
                )
                greatest = (
                    newest
                    if old is None
                    or (newest[0], newest[1].ljust(18, "0")) > (old[0], old[1].ljust(18, "0"))
                    else old
                )
                self._connection.execute(
                    "UPDATE history_sync_state SET newest_played_at_seconds=?,newest_played_at_fraction=?,interval_completeness='incomplete',coverage_reason=? WHERE provider=?",
                    (greatest[0], greatest[1], reason, provider),
                )
                self._connection.execute(
                    "INSERT INTO history_incomplete_intervals(provider,lower_seconds,lower_fraction,upper_seconds,upper_fraction,reason,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        provider,
                        None if requested_after_ms is None else requested_after_ms // 1000,
                        (
                            None
                            if requested_after_ms is None
                            else str(requested_after_ms % 1000).rjust(3, "0").rstrip("0")
                        ),
                        newest[0],
                        newest[1],
                        reason,
                        attempted_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
        return inserted

    def finish_success(
        self, provider: str, checked_at: datetime, *, outcome: str, reason: str
    ) -> None:
        with self._catalog.transaction():
            self._connection.execute(
                "UPDATE history_sync_state SET last_successful_check_at=?,needs_repair=0,attempt_outcome=?,coverage_reason=? WHERE provider=?",
                (checked_at.astimezone(timezone.utc).isoformat(), outcome, reason, provider),
            )

    def finish_partial(self, provider: str, *, outcome: str, reason: str) -> None:
        with self._catalog.transaction():
            self._connection.execute(
                "UPDATE history_sync_state SET needs_repair=1,attempt_outcome=?,interval_completeness='incomplete',coverage_reason=? WHERE provider=?",
                (outcome, reason, provider),
            )

    def get_state(self, provider: str) -> HistorySyncState | None:
        row = self._connection.execute(
            "SELECT provider,last_attempt_at,last_successful_check_at,needs_repair,newest_played_at_seconds,newest_played_at_fraction,requested_after_ms,attempt_outcome,interval_completeness,coverage_reason FROM history_sync_state WHERE provider=?",
            (provider,),
        ).fetchone()
        if row is None:
            return None
        newest = None if row[4] is None else _format_timestamp(int(row[4]), str(row[5] or ""))
        return HistorySyncState(
            str(row[0]),
            datetime.fromisoformat(str(row[1])),
            None if row[2] is None else datetime.fromisoformat(str(row[2])),
            bool(row[3]),
            newest,
            None if row[6] is None else int(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
        )

    def list_observations(self, provider: str) -> tuple[StoredRecentPlay, ...]:
        rows = self._connection.execute(
            "SELECT provider,observation_key,played_at_seconds,played_at_fraction,track_uri,track_name,primary_artist_name,album_name,context_uri FROM recent_play_observations WHERE provider=? ORDER BY played_at_seconds,played_at_fraction",
            (provider,),
        ).fetchall()
        return tuple(
            StoredRecentPlay(
                str(r[0]),
                str(r[1]),
                _format_timestamp(int(r[2]), str(r[3])),
                str(r[4]),
                str(r[5]),
                str(r[6]),
                str(r[7]),
                None if r[8] is None else str(r[8]),
            )
            for r in rows
        )

    def list_intervals(self, provider: str) -> tuple[IncompleteInterval, ...]:
        rows = self._connection.execute(
            "SELECT lower_seconds,lower_fraction,upper_seconds,upper_fraction,reason FROM history_incomplete_intervals WHERE provider=? ORDER BY id",
            (provider,),
        ).fetchall()
        return tuple(
            IncompleteInterval(
                None if r[0] is None else _format_timestamp(int(r[0]), str(r[1] or "")),
                None if r[2] is None else _format_timestamp(int(r[2]), str(r[3] or "")),
                str(r[4]),
            )
            for r in rows
        )

    def archive_bounds(self) -> tuple[str | None, str | None]:
        row = self._connection.execute(
            "SELECT MIN(played_at), MAX(played_at) FROM listening_history"
        ).fetchone()
        if row is None:
            return None, None
        return (
            None if row[0] is None else str(row[0]).replace("+00:00", "Z"),
            None if row[1] is None else str(row[1]).replace("+00:00", "Z"),
        )


def _format_timestamp(seconds: int, fraction: str) -> str:
    value = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return value + (("." + fraction) if fraction else "") + "Z"


__all__ = [
    "HistorySyncState",
    "IncompleteInterval",
    "RecentHistoryStore",
    "StoredRecentPlay",
    "canonical_timestamp",
    "request_after_ms",
]
