"""Validated import and bounded summaries for Spotify extended streaming history."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from music_friend.store.catalog import Catalog

_AUDIO_MEMBER = re.compile(
    r"Spotify Extended Streaming History/Streaming_History_Audio_\d{4}(?:_\d+)?\.json\Z"
)
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_RECORDS = 2_000_000
_BRIEF_MILLISECONDS = 30_000


@dataclass(frozen=True, slots=True)
class SpotifyHistoryImportResult:
    imported: int
    duplicates: int
    non_music: int
    member_count: int
    first_played_at: str | None
    last_played_at: str | None


@dataclass(frozen=True, slots=True)
class HistoryRanking:
    name: str
    play_count: int
    milliseconds_played: int


@dataclass(frozen=True, slots=True)
class HistorySummary:
    since: str | None
    until: str | None
    first_played_at: str | None
    last_played_at: str | None
    play_count: int
    milliseconds_played: int
    skipped_count: int
    brief_count: int
    top_artists: tuple[HistoryRanking, ...]
    top_tracks: tuple[HistoryRanking, ...]


def _text(
    value: object, field: str, *, nullable: bool = False, allow_empty: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if (
        type(value) is not str
        or (not value and not allow_empty)
        or len(value) > 4096
        or "\x00" in value
    ):
        raise ValueError(f"invalid {field}")
    return value


def _boolean(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"invalid {field}")
    return int(value)


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    assert text is not None
    if not text.endswith("Z"):
        raise ValueError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    selected: list[zipfile.ZipInfo] = []
    total = 0
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        mode = item.external_attr >> 16
        if item.flag_bits & 1 or path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid Spotify history archive")
        if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError("invalid Spotify history archive")
        if _AUDIO_MEMBER.fullmatch(item.filename):
            if item.file_size > _MAX_MEMBER_BYTES:
                raise ValueError("invalid Spotify history archive")
            total += item.file_size
            selected.append(item)
    if not selected or total > _MAX_TOTAL_BYTES:
        raise ValueError("invalid Spotify history archive")
    return tuple(sorted(selected, key=lambda item: item.filename))


def _event(
    record: object,
    occurrences: Counter[str],
    archive_digest: str,
    imported_at: str,
) -> tuple[object, ...] | None:
    if type(record) is not dict:
        raise ValueError("invalid record")
    track_uri = record.get("spotify_track_uri")
    if track_uri is None:
        return None
    played_at = _timestamp(record.get("ts"), "ts")
    milliseconds = record.get("ms_played")
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError("invalid ms_played")
    fields = (
        played_at,
        milliseconds,
        _text(track_uri, "spotify_track_uri"),
        _text(record.get("master_metadata_track_name"), "track name"),
        _text(record.get("master_metadata_album_artist_name"), "artist name"),
        _text(record.get("master_metadata_album_album_name"), "album name", nullable=True),
        _text(record.get("reason_start"), "reason_start", nullable=True, allow_empty=True),
        _text(record.get("reason_end"), "reason_end", nullable=True, allow_empty=True),
        _boolean(record.get("shuffle"), "shuffle"),
        _boolean(record.get("skipped"), "skipped"),
        _boolean(record.get("offline"), "offline"),
        _boolean(record.get("incognito_mode"), "incognito_mode"),
    )
    material = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    occurrence = occurrences[material]
    occurrences[material] += 1
    identity = json.dumps((fields, occurrence), ensure_ascii=False, separators=(",", ":"))
    event_id = hashlib.sha256(("spotify-history-v1\0" + identity).encode()).hexdigest()
    return (event_id, "spotify-history", *fields, archive_digest, imported_at)


def import_spotify_history(
    catalog: Catalog, source: Path, *, dry_run: bool = False
) -> SpotifyHistoryImportResult:
    """Validate a Spotify ZIP completely, then atomically add its music plays."""
    try:
        path = Path(source)
        metadata = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_ARCHIVE_BYTES
        ):
            raise ValueError
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows: list[tuple[object, ...]] = []
        non_music = 0
        occurrences: Counter[str] = Counter()
        with zipfile.ZipFile(path) as archive:
            members = _members(archive)
            for member in members:
                with archive.open(member) as stream:
                    decoded = json.load(stream)
                if type(decoded) is not list:
                    raise ValueError
                for record in decoded:
                    if len(rows) + non_music >= _MAX_RECORDS:
                        raise ValueError
                    row = _event(record, occurrences, digest, imported_at)
                    if row is None:
                        non_music += 1
                    else:
                        rows.append(row)
        connection = catalog._require_connection()
        before = connection.total_changes
        if dry_run:
            existing = {
                str(item[0])
                for item in connection.execute("SELECT event_id FROM listening_history")
            }
            imported = sum(str(row[0]) not in existing for row in rows)
        else:
            with catalog.transaction():
                connection.executemany(
                    """
                INSERT OR IGNORE INTO listening_history (
                    event_id, source, played_at, milliseconds_played, track_uri, track_name,
                    artist_name, album_name, reason_start, reason_end, shuffle, skipped, offline,
                    incognito, archive_digest, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    rows,
                )
            imported = connection.total_changes - before
        dates = [str(row[2]) for row in rows]
        return SpotifyHistoryImportResult(
            imported=imported,
            duplicates=len(rows) - imported,
            non_music=non_music,
            member_count=len(members),
            first_played_at=min(dates, key=lambda value: value.replace("Z", "+00:00"))
            if dates
            else None,
            last_played_at=max(dates, key=lambda value: value.replace("Z", "+00:00"))
            if dates
            else None,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise ValueError("invalid Spotify history archive") from None


def _range_timestamp(value: object, field: str) -> datetime:
    """Parse a caller-supplied ``since``/``until`` bound.

    Unlike ``_timestamp`` (which only accepts the ``Z``-suffixed format Spotify's
    export always uses), this accepts any RFC 3339 offset -- positive, negative,
    or ``Z`` -- and normalizes the result to UTC. A naive timestamp (no offset and
    no ``Z``) is rejected with a message that says an offset is required, and an
    impossible calendar date (e.g. 2026-02-30) is rejected as an invalid date-time,
    both surfaced by ``datetime.fromisoformat`` itself.
    """
    text = _text(value, field)
    assert text is not None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{field} must be a valid RFC 3339 date-time") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset or 'Z' suffix")
    return parsed.astimezone(timezone.utc)


def summarize_history(
    catalog: Catalog, *, since: str | None = None, until: str | None = None, limit: int = 10
) -> HistorySummary:
    """Summarize imported evidence in the half-open UTC interval ``[since, until)``.

    A zero-length range (``since == until``) is rejected as ``invalid_arguments``
    rather than returning an empty summary, for the same reason a reversed range
    is rejected: both describe a range the caller almost certainly did not intend,
    and a loud error is more useful than a silent empty result.
    """
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be from 1 through 50")
    normalized_since = None if since is None else _range_timestamp(since, "since")
    normalized_until = None if until is None else _range_timestamp(until, "until")
    if normalized_since is not None and normalized_until is not None:
        if normalized_since > normalized_until:
            raise ValueError("since must not be after until")
        if normalized_since == normalized_until:
            raise ValueError("since and until must not be equal; the range would be empty")
    since_text = (
        None if normalized_since is None else normalized_since.isoformat().replace("+00:00", "Z")
    )
    until_text = (
        None if normalized_until is None else normalized_until.isoformat().replace("+00:00", "Z")
    )
    clauses: list[str] = []
    arguments: list[object] = []
    if since_text is not None:
        clauses.append("replace(played_at, 'Z', '+00:00') >= ?")
        arguments.append(since_text.replace("Z", "+00:00"))
    if until_text is not None:
        clauses.append("replace(played_at, 'Z', '+00:00') < ?")
        arguments.append(until_text.replace("Z", "+00:00"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    connection = catalog._require_connection()
    aggregate = connection.execute(
        f"""SELECT MIN(replace(played_at, 'Z', '+00:00')), MAX(replace(played_at, 'Z', '+00:00')), COUNT(*), COALESCE(SUM(milliseconds_played), 0),
                   COALESCE(SUM(CASE WHEN skipped = 1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN milliseconds_played < ? THEN 1 ELSE 0 END), 0)
            FROM listening_history{where}""",
        [_BRIEF_MILLISECONDS, *arguments],
    ).fetchone()

    def rankings(group: str, name: str) -> tuple[HistoryRanking, ...]:
        rows = connection.execute(
            f"""SELECT {name}, COUNT(*), SUM(milliseconds_played) FROM listening_history{where}
                GROUP BY {group} ORDER BY COUNT(*) DESC, SUM(milliseconds_played) DESC, {name} COLLATE NOCASE, {name}
                LIMIT ?""",
            [*arguments, limit],
        ).fetchall()
        return tuple(HistoryRanking(str(row[0]), int(row[1]), int(row[2])) for row in rows)

    return HistorySummary(
        since=since_text,
        until=until_text,
        first_played_at=None if aggregate[0] is None else str(aggregate[0]).replace("+00:00", "Z"),
        last_played_at=None if aggregate[1] is None else str(aggregate[1]).replace("+00:00", "Z"),
        play_count=int(aggregate[2]),
        milliseconds_played=int(aggregate[3]),
        skipped_count=int(aggregate[4]),
        brief_count=int(aggregate[5]),
        top_artists=rankings("artist_name", "artist_name"),
        top_tracks=rankings("track_uri, track_name", "track_name"),
    )


__all__ = [
    "HistoryRanking",
    "HistorySummary",
    "SpotifyHistoryImportResult",
    "import_spotify_history",
    "summarize_history",
]
