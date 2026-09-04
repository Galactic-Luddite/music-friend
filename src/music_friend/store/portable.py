"""Portable, credential-free catalog lifecycle operations."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    Artist,
    Event,
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxEntry,
    InboxState,
    Interest,
    InterestKind,
    InterestStatus,
    LocalPreference,
    LocalPreferenceKey,
    Observation,
    RefreshKind,
    RefreshMetric,
    RefreshMetricKind,
    RefreshRun,
    RefreshStatus,
    RefreshSummary,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
    WatchlistAction,
    WatchlistOverride,
)
from music_friend.domain.text import _canonical_source_text
from music_friend.store.catalog import Catalog

_FORMAT = "music-friend-catalog"
_VERSION = 4
_SUPPORTED_VERSIONS = frozenset({1, 2, 3, 4})
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_RECORDS = 2_000_000
_PortableCanonical = Artist | Release | Event
_TEXT_LIMIT = 4096
_SOURCE_MAPPING_ID_LIMIT = (_TEXT_LIMIT * 3) + 35
_PORTABLE_DENYLIST = (
    "token",
    "secret",
    "credential",
    "password",
    "raw_payload",
    "raw_provider",
    "email_body",
    "calendar_body",
)


def _source_cursor_id(source: str, capability: str) -> str:
    material = "\0".join(("music-friend-portable-source-cursor-v1", source, capability)).encode()
    return f"source-cursor:{sha256(material).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    record_count: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    path: Path
    record_count: int


@dataclass(frozen=True, slots=True)
class PurgeResult:
    mapping_count: int
    observation_count: int
    check_time_count: int
    affinity_evidence_count: int = 0
    source_cursor_count: int = 0
    signal_count: int = 0
    inbox_entry_count: int = 0
    refresh_run_count: int = 0


class _StrictDecoder(json.JSONDecoder):
    def __init__(self) -> None:
        super().__init__(
            object_pairs_hook=self._object,
            parse_constant=self._reject_constant,
        )

    @staticmethod
    def _reject_constant(value: str) -> NoReturn:
        raise ValueError(f"invalid JSON constant: {value}")

    def _object(self, pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result


def _count_records_array(text: str, max_records: int) -> None:
    """Count top-level record elements lexically before allocating decoded objects."""

    def skip_space(position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    def skip_string(position: int) -> tuple[str, int]:
        value, end = json.JSONDecoder().raw_decode(text, position)
        if not isinstance(value, str):
            raise ValueError("portable catalog string token is invalid")
        return value, end

    stack: list[str] = []
    count = 0
    position = skip_space(0)
    while position < len(text):
        character = text[position]
        if character == '"':
            value, end = skip_string(position)
            after = skip_space(end)
            if stack == ["{"] and value == "records" and after < len(text) and text[after] == ":":
                array_start = skip_space(after + 1)
                if array_start >= len(text) or text[array_start] != "[":
                    position = end
                    continue
                position = array_start + 1
                nesting = 0
                expecting_value = True
                while position < len(text):
                    character = text[position]
                    if character in " \t\r\n":
                        position += 1
                        continue
                    if character == '"':
                        if expecting_value and nesting == 0:
                            count += 1
                            if count > max_records:
                                raise ValueError("portable catalog record limit exceeded")
                            expecting_value = False
                        _, position = skip_string(position)
                        continue
                    if character == "]" and nesting == 0:
                        position += 1
                        break
                    if expecting_value and nesting == 0 and character != ",":
                        count += 1
                        if count > max_records:
                            raise ValueError("portable catalog record limit exceeded")
                        expecting_value = False
                    if character in "[{":
                        nesting += 1
                    elif character in "]}":
                        nesting -= 1
                    elif character == "," and nesting == 0:
                        expecting_value = True
                    position += 1
                continue
            position = end
            continue
        if character in "[{":
            stack.append(character)
        elif character in "]}" and stack:
            stack.pop()
        position += 1


def _connection(catalog: Catalog) -> sqlite3.Connection:
    return catalog._require_connection()


def _iso(value: object) -> str:
    return str(value)


def _export_records(catalog: Catalog) -> list[dict[str, object]]:
    connection = _connection(catalog)
    records: list[dict[str, object]] = []
    for local_id, display_name, confidence, observed_at in connection.execute(
        "SELECT local_id, display_name, identity_confidence, observed_at FROM artists"
    ):
        records.append(
            {
                "kind": "artist",
                "local_id": str(local_id),
                "display_name": str(display_name),
                "identity_confidence": str(confidence),
                "observed_at": _iso(observed_at),
            }
        )
    for local_id, title, release_type, release_date, precision, observed_at in connection.execute(
        "SELECT local_id, title, release_type, release_date, date_precision, observed_at FROM releases"
    ):
        artist_refs = [
            str(row[0])
            for row in connection.execute(
                "SELECT artist_id FROM release_artists WHERE release_id = ? ORDER BY position",
                (local_id,),
            )
        ]
        records.append(
            {
                "kind": "release",
                "local_id": str(local_id),
                "title": str(title),
                "release_type": str(release_type),
                "release_date": str(release_date),
                "date_precision": str(precision),
                "artist_refs": artist_refs,
                "observed_at": _iso(observed_at),
            }
        )
    for row in connection.execute(
        "SELECT local_id, title, venue_name, locality, starts_at, time_precision, observed_at FROM events"
    ):
        local_id = str(row[0])
        artist_refs = [
            str(item[0])
            for item in connection.execute(
                "SELECT artist_id FROM event_artists WHERE event_id = ? ORDER BY position",
                (local_id,),
            )
        ]
        source_links = [
            str(item[0])
            for item in connection.execute(
                "SELECT source_link FROM event_source_links WHERE event_id = ? ORDER BY position",
                (local_id,),
            )
        ]
        records.append(
            {
                "kind": "event",
                "local_id": local_id,
                "title": str(row[1]),
                "artist_refs": artist_refs,
                "venue_name": row[2],
                "locality": row[3],
                "starts_at": row[4],
                "time_precision": row[5],
                "source_links": source_links,
                "observed_at": _iso(row[6]),
            }
        )
    for row in connection.execute(
        """SELECT event_id, source, played_at, milliseconds_played, track_uri, track_name,
                  artist_name, album_name, reason_start, reason_end, shuffle, skipped, offline,
                  incognito, archive_digest, imported_at
           FROM listening_history"""
    ):
        records.append(
            {
                "kind": "listening_history",
                "local_id": str(row[0]),
                "source": str(row[1]),
                "played_at": str(row[2]),
                "milliseconds_played": int(row[3]),
                "track_uri": str(row[4]),
                "track_name": str(row[5]),
                "artist_name": str(row[6]),
                "album_name": row[7],
                "reason_start": row[8],
                "reason_end": row[9],
                "shuffle": row[10],
                "skipped": row[11],
                "offline": row[12],
                "incognito": row[13],
                "archive_digest": str(row[14]),
                "imported_at": str(row[15]),
            }
        )
    for row in connection.execute(
        "SELECT local_id, kind, target_local_id, status, created_by, created_at, updated_at FROM interests"
    ):
        records.append(
            dict(
                zip(
                    (
                        "local_id",
                        "target_kind",
                        "target_local_id",
                        "status",
                        "created_by",
                        "created_at",
                        "updated_at",
                    ),
                    (str(item) for item in row),
                ),
                kind="interest",
            )
        )
    for row in connection.execute(
        "SELECT local_id, source, native_id, record_kind, record_local_id, fact_name, observed_at FROM observations"
    ):
        records.append(
            dict(
                zip(
                    (
                        "local_id",
                        "source",
                        "native_id",
                        "record_kind",
                        "record_local_id",
                        "fact_name",
                        "observed_at",
                    ),
                    (str(item) for item in row),
                ),
                kind="observation",
            )
        )
    for source, checked_at in connection.execute("SELECT source, checked_at FROM check_times"):
        records.append(
            {
                "kind": "check_time",
                "local_id": str(source),
                "source": str(source),
                "checked_at": str(checked_at),
            }
        )
    for row in connection.execute(
        """
        SELECT local_id, artist_local_id, source, kind, evidence_key, rank, observed_at
        FROM affinity_evidence
        """
    ):
        records.append(
            {
                "kind": "affinity_evidence",
                "local_id": str(row[0]),
                "artist_local_id": str(row[1]),
                "source": str(row[2]),
                "evidence_kind": str(row[3]),
                "evidence_key": str(row[4]),
                "rank": row[5],
                "observed_at": str(row[6]),
            }
        )
    for row in connection.execute(
        "SELECT artist_local_id, action, updated_at FROM watchlist_overrides"
    ):
        records.append(
            {
                "kind": "watchlist_override",
                "local_id": str(row[0]),
                "artist_local_id": str(row[0]),
                "action": str(row[1]),
                "updated_at": str(row[2]),
            }
        )
    for row in connection.execute("SELECT key, value, updated_at FROM local_preferences"):
        records.append(
            {
                "kind": "local_preference",
                "local_id": str(row[0]),
                "preference_key": str(row[0]),
                "value": str(row[1]),
                "updated_at": str(row[2]),
            }
        )
    for row in connection.execute(
        """
        SELECT local_id, source, kind, status, started_at, finished_at, summary_json
        FROM refresh_runs
        """
    ):
        records.append(
            {
                "kind": "refresh_run",
                "local_id": str(row[0]),
                "source": str(row[1]),
                "refresh_kind": str(row[2]),
                "status": str(row[3]),
                "started_at": str(row[4]),
                "finished_at": row[5],
                "summary": _StrictDecoder().decode(str(row[6])),
            }
        )
    for row in connection.execute(
        "SELECT source, capability, cursor, updated_at FROM source_cursors"
    ):
        source, capability = str(row[0]), str(row[1])
        records.append(
            {
                "kind": "source_cursor",
                "local_id": _source_cursor_id(source, capability),
                "source": source,
                "capability": capability,
                "cursor": str(row[2]),
                "updated_at": str(row[3]),
            }
        )
    for row in connection.execute(
        """
        SELECT source, state, observed_at, retry_at, retry_is_exact, consecutive_limits
        FROM source_limits
        """
    ):
        records.append(
            {
                "kind": "source_limit",
                "local_id": str(row[0]),
                "source": str(row[0]),
                "state": str(row[1]),
                "observed_at": str(row[2]),
                "retry_at": row[3],
                "retry_is_exact": bool(row[4]),
                "consecutive_limits": int(row[5]),
            }
        )
    for row in connection.execute(
        """
        SELECT local_id, kind, record_local_id, provider, provider_native_id,
               fingerprint, material_version, explanation_json, observed_at
        FROM signals
        """
    ):
        records.append(
            {
                "kind": "signal",
                "local_id": str(row[0]),
                "signal_kind": str(row[1]),
                "record_local_id": str(row[2]),
                "provider": str(row[3]),
                "provider_native_id": str(row[4]),
                "fingerprint": str(row[5]),
                "material_version": str(row[6]),
                "explanation": _StrictDecoder().decode(str(row[7])),
                "observed_at": str(row[8]),
            }
        )
    for row in connection.execute(
        "SELECT local_id, signal_local_id, state, created_at, updated_at FROM inbox_entries"
    ):
        records.append(
            {
                "kind": "inbox_entry",
                "local_id": str(row[0]),
                "signal_local_id": str(row[1]),
                "state": str(row[2]),
                "created_at": str(row[3]),
                "updated_at": str(row[4]),
            }
        )
    for row in connection.execute(
        """
        SELECT mapping.record_kind, mapping.record_local_id, reference.source,
               reference.native_id, reference.canonical_url, reference.observed_at,
               mapping.position
        FROM record_sources AS mapping
        JOIN source_references AS reference ON reference.id = mapping.source_reference_id
        """
    ):
        record_kind, record_local_id, source, native_id = (str(item) for item in row[:4])
        records.append(
            {
                "kind": "source_mapping",
                "local_id": f"{record_kind}:{record_local_id}:{source}:{native_id}",
                "record_kind": record_kind,
                "record_local_id": record_local_id,
                "source": source,
                "native_id": native_id,
                "canonical_url": row[4],
                "observed_at": str(row[5]),
                "position": int(row[6]),
            }
        )
    records.sort(key=lambda record: (str(record["kind"]), str(record["local_id"])))
    return records


def _prepare_parent(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for component in absolute.parent.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("unsafe export parent")


def _open_parent_posix(path: Path, *, create_missing: bool) -> int:
    absolute = path if path.is_absolute() else Path.cwd() / path
    if ".." in absolute.parts or not absolute.name:
        raise OSError("unsafe portable path")
    components = absolute.parent.parts[1:]
    if not components:
        raise OSError("unsafe portable parent")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(absolute.anchor, flags)
    try:
        for component in components:
            created = False
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    created = True
                    os.chmod(
                        component,
                        0o700,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                next_fd = os.open(component, flags, dir_fd=current_fd)
            try:
                metadata = os.fstat(next_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("unsafe portable parent")
                if created:
                    os.fchmod(next_fd, 0o700)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_export_parent_posix(path: Path) -> int:
    return _open_parent_posix(path, create_missing=True)


def _open_existing_parent_posix(path: Path) -> int:
    return _open_parent_posix(path, create_missing=False)


def _reject_credential_like_content(value: object, *, text_limit: int = _TEXT_LIMIT) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_credential_like_content(key)
            _reject_credential_like_content(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_credential_like_content(item)
        return
    if isinstance(value, str):
        folded = value.casefold()
        if any(denied in folded for denied in _PORTABLE_DENYLIST):
            raise ValueError("portable catalog contains credential-like content")
        if len(value) > text_limit:
            raise ValueError("portable catalog value exceeds portable text limit")


def _validate_export_content(payload: dict[str, object]) -> None:
    for key in ("format", "exported_at"):
        _reject_credential_like_content(payload[key])
    records = payload["records"]
    if not isinstance(records, list):
        raise AssertionError("export records changed type")
    for record in records:
        if not isinstance(record, dict):
            raise AssertionError("export record changed type")
        for key, value in record.items():
            _reject_credential_like_content(key)
            limit = (
                _SOURCE_MAPPING_ID_LIMIT
                if key == "local_id" and record.get("kind") == "source_mapping"
                else _TEXT_LIMIT
            )
            _reject_credential_like_content(value, text_limit=limit)


def export_catalog(
    catalog: Catalog,
    destination: Path,
    *,
    replace: bool = False,
    exported_at: datetime | None = None,
) -> ExportResult:
    """Write a deterministic portable catalog to an explicit local path."""
    if not isinstance(destination, Path) or ".." in destination.parts or not destination.name:
        raise ValueError("destination must be a safe pathlib.Path")
    timestamp = exported_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("exported_at must be timezone-aware")
    with catalog.transaction():
        records = _export_records(catalog)
    payload = {
        "format": _FORMAT,
        "version": _VERSION,
        "exported_at": timestamp.isoformat(),
        "records": records,
    }
    _validate_export_content(payload)
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    absolute = destination if destination.is_absolute() else Path.cwd() / destination
    if os.name == "posix":
        parent_fd = _open_export_parent_posix(absolute)
    else:
        _prepare_parent(absolute)
        parent_fd = os.open(absolute.parent, os.O_RDONLY)
    temporary_name = f".{absolute.name}.{secrets.token_hex(8)}.tmp"
    temporary_fd = -1
    installed = False
    try:
        try:
            existing = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise OSError("unsafe export target")
        if existing is not None and not replace:
            raise FileExistsError(destination)
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb", closefd=True) as stream:
            temporary_fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(
                temporary_name,
                absolute.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            os.link(
                temporary_name,
                absolute.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent_fd)
        installed = True
        os.fsync(parent_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if not installed:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
    return ExportResult(path=destination, record_count=len(records))


def _read_portable(path: Path, max_bytes: int, max_records: int) -> dict[str, object]:
    if max_bytes <= 0 or max_records <= 0:
        raise ValueError("import limits must be positive")
    if not isinstance(path, Path) or ".." in path.parts or not path.name:
        raise ValueError("source must be a safe pathlib.Path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("portable source is not a regular file")
        if metadata.st_size > max_bytes:
            raise ValueError("portable catalog byte limit exceeded")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes or os.read(descriptor, 1):
            raise ValueError("portable catalog byte limit exceeded")
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("portable catalog must be UTF-8") from error
    _count_records_array(text, max_records)
    decoded = _StrictDecoder().decode(text)
    if not isinstance(decoded, dict):
        raise ValueError("portable catalog root must be an object")
    return decoded


def _require_keys(record: dict[str, object], expected: frozenset[str]) -> None:
    if frozenset(record) != expected:
        raise ValueError("portable record has invalid fields")


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _source_text(value: object, *, maximum: int = 4096) -> str:
    text = _text(value, "portable text", maximum=maximum)
    if _canonical_source_text(text) != text:
        raise ValueError("portable text is invalid")
    return text


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        # Python 3.10 does not accept the ISO 8601 UTC designator even though
        # newer supported versions do. Normalize it to the equivalent offset.
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00" if text.endswith("Z") else text)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError(f"{name} is invalid")
    return tuple(_text(item, name) for item in value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} is invalid")
    return value


def _refresh_summary(value: object) -> RefreshSummary:
    if type(value) is not dict or set(value) != {"version", "metrics"}:
        raise ValueError("refresh summary is invalid")
    if value["version"] != 1 or type(value["metrics"]) is not list:
        raise ValueError("refresh summary is invalid")
    raw_metrics = value["metrics"]
    if len(raw_metrics) > len(RefreshMetricKind):
        raise ValueError("refresh summary is invalid")
    metrics: list[RefreshMetric] = []
    for raw_metric in raw_metrics:
        if type(raw_metric) is not dict or set(raw_metric) != {"kind", "count"}:
            raise ValueError("refresh summary is invalid")
        metrics.append(
            RefreshMetric(
                kind=RefreshMetricKind(_text(raw_metric["kind"], "metric kind", maximum=32)),
                count=_integer(raw_metric["count"], "metric count"),
            )
        )
    return RefreshSummary(tuple(metrics))


def _explanation(value: object) -> Explanation:
    if type(value) is not dict or set(value) != {"version", "reasons"}:
        raise ValueError("signal explanation is invalid")
    if value["version"] != 1 or type(value["reasons"]) is not list:
        raise ValueError("signal explanation is invalid")
    raw_reasons = value["reasons"]
    if not 1 <= len(raw_reasons) <= 16:
        raise ValueError("signal explanation is invalid")
    reasons: list[ExplanationReason] = []
    for raw_reason in raw_reasons:
        if type(raw_reason) is not dict or set(raw_reason) != {"kind", "detail"}:
            raise ValueError("signal explanation is invalid")
        reasons.append(
            ExplanationReason(
                kind=ExplanationReasonKind(
                    _text(raw_reason["kind"], "explanation kind", maximum=32)
                ),
                detail=(
                    None if raw_reason["detail"] is None else _source_text(raw_reason["detail"])
                ),
            )
        )
    return Explanation(tuple(reasons))


def _validate_document(
    payload: dict[str, object],
) -> tuple[list[_PortableCanonical], list[dict[str, object]]]:
    if frozenset(payload) != frozenset({"format", "version", "exported_at", "records"}):
        raise ValueError("portable catalog has invalid top-level fields")
    if (
        payload["format"] != _FORMAT
        or type(payload["version"]) is not int
        or payload["version"] not in _SUPPORTED_VERSIONS
    ):
        raise ValueError("unsupported portable catalog format")
    version = int(payload["version"])
    _datetime(payload["exported_at"], "exported_at")
    raw_records = payload["records"]
    if not isinstance(raw_records, list):
        raise ValueError("records must be an array")
    records: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValueError("portable record must be an object")
        kind = _text(raw_record.get("kind"), "kind", maximum=32)
        local_id = _source_text(
            raw_record.get("local_id"),
            maximum=(_SOURCE_MAPPING_ID_LIMIT if kind == "source_mapping" else _TEXT_LIMIT),
        )
        identity = (kind, local_id)
        if identity in identities:
            raise ValueError("duplicate portable record identity")
        identities.add(identity)
        records.append(raw_record)

    mappings: dict[tuple[str, str], list[tuple[int, SourceReference]]] = {}
    for record in records:
        if record["kind"] != "source_mapping":
            continue
        _require_keys(
            record,
            frozenset(
                {
                    "kind",
                    "local_id",
                    "record_kind",
                    "record_local_id",
                    "source",
                    "native_id",
                    "canonical_url",
                    "observed_at",
                    "position",
                }
            ),
        )
        target = (
            _text(record["record_kind"], "record_kind", maximum=32),
            _text(record["record_local_id"], "record_local_id"),
        )
        expected_local_id = ":".join(
            (target[0], target[1], str(record["source"]), str(record["native_id"]))
        )
        if record["local_id"] != expected_local_id:
            raise ValueError("source mapping identity does not match fields")
        reference = SourceReference(
            source=_text(record["source"], "source"),
            native_id=_text(record["native_id"], "native_id"),
            canonical_url=(
                None if record["canonical_url"] is None else _source_text(record["canonical_url"])
            ),
            observed_at=_datetime(record["observed_at"], "observed_at"),
        )
        mappings.setdefault(target, []).append(
            (_integer(record["position"], "position"), reference)
        )

    canonical: list[_PortableCanonical] = []
    deferred: list[dict[str, object]] = []
    for record in records:
        kind = str(record["kind"])
        deferred_kinds = {"source_mapping", "interest", "observation", "check_time"}
        if version >= 2:
            deferred_kinds.update(
                {
                    "affinity_evidence",
                    "watchlist_override",
                    "local_preference",
                    "refresh_run",
                    "source_cursor",
                    "signal",
                    "inbox_entry",
                }
            )
        if version >= 3:
            deferred_kinds.add("source_limit")
        if version >= 4:
            deferred_kinds.add("listening_history")
        if kind in deferred_kinds:
            if kind != "source_mapping":
                deferred.append(record)
            continue
        local_id = str(record["local_id"])
        positioned = sorted(mappings.get((kind, local_id), []), key=lambda item: item[0])
        if [position for position, _ in positioned] != list(range(len(positioned))):
            raise ValueError("source mapping positions must be contiguous")
        source_refs = tuple(reference for _, reference in positioned)
        if kind == "artist":
            _require_keys(
                record,
                frozenset(
                    {"kind", "local_id", "display_name", "identity_confidence", "observed_at"}
                ),
            )
            canonical.append(
                Artist(
                    local_id=local_id,
                    display_name=_source_text(record["display_name"]),
                    source_refs=source_refs,
                    identity_confidence=IdentityConfidence(
                        _text(record["identity_confidence"], "identity_confidence", maximum=32)
                    ),
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        elif kind == "release":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "title",
                        "release_type",
                        "release_date",
                        "date_precision",
                        "artist_refs",
                        "observed_at",
                    }
                ),
            )
            canonical.append(
                Release(
                    local_id=local_id,
                    title=_source_text(record["title"]),
                    release_type=_source_text(record["release_type"]),
                    release_date=date.fromisoformat(_text(record["release_date"], "release_date")),
                    date_precision=ReleaseDatePrecision(
                        _text(record["date_precision"], "date_precision", maximum=16)
                    ),
                    artist_refs=_string_list(record["artist_refs"], "artist_refs"),
                    source_refs=source_refs,
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        elif kind == "event":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "title",
                        "artist_refs",
                        "venue_name",
                        "locality",
                        "starts_at",
                        "time_precision",
                        "source_links",
                        "observed_at",
                    }
                ),
            )
            canonical.append(
                Event(
                    local_id=local_id,
                    title=_source_text(record["title"]),
                    artist_refs=_string_list(record["artist_refs"], "artist_refs"),
                    venue_name=(
                        None if record["venue_name"] is None else _source_text(record["venue_name"])
                    ),
                    locality=(
                        None if record["locality"] is None else _source_text(record["locality"])
                    ),
                    starts_at=(
                        None
                        if record["starts_at"] is None
                        else _datetime(record["starts_at"], "starts_at")
                    ),
                    time_precision=_optional_text(record["time_precision"], "time_precision"),
                    source_links=tuple(
                        _source_text(link)
                        for link in _string_list(record["source_links"], "source_links")
                    ),
                    source_refs=source_refs,
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        else:
            raise ValueError("unknown portable record kind")
    canonical_identities = {(type(item).__name__.lower(), item.local_id) for item in canonical}
    if set(mappings) - canonical_identities:
        raise ValueError("source mapping target does not exist")
    return canonical, deferred


def _replay(
    catalog: Catalog,
    canonical: list[_PortableCanonical],
    deferred: list[dict[str, object]],
) -> None:
    artists = [item for item in canonical if isinstance(item, Artist)]
    releases = [item for item in canonical if isinstance(item, Release)]
    events = [item for item in canonical if isinstance(item, Event)]
    for artist in artists:
        catalog.put_artist(artist)
    for release in releases:
        catalog.put_release(release)
    for event in events:
        catalog.put_event(event)
    replay_order = {
        "interest": 0,
        "observation": 1,
        "check_time": 2,
        "affinity_evidence": 3,
        "watchlist_override": 4,
        "local_preference": 5,
        "refresh_run": 6,
        "source_cursor": 7,
        "source_limit": 8,
        "signal": 9,
        "inbox_entry": 10,
        "listening_history": 11,
    }
    ordered_deferred = sorted(
        deferred,
        key=lambda item: (replay_order.get(str(item["kind"]), 100), str(item["local_id"])),
    )
    for record in ordered_deferred:
        kind = str(record["kind"])
        if kind == "interest":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "target_kind",
                        "target_local_id",
                        "status",
                        "created_by",
                        "created_at",
                        "updated_at",
                    }
                ),
            )
            catalog.put_interest(
                Interest(
                    local_id=str(record["local_id"]),
                    kind=InterestKind(_text(record["target_kind"], "target_kind", maximum=16)),
                    target_local_id=_text(record["target_local_id"], "target_local_id"),
                    status=InterestStatus(_text(record["status"], "status", maximum=16)),
                    created_by=_text(record["created_by"], "created_by"),
                    created_at=_datetime(record["created_at"], "created_at"),
                    updated_at=_datetime(record["updated_at"], "updated_at"),
                )
            )
        elif kind == "observation":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "source",
                        "native_id",
                        "record_kind",
                        "record_local_id",
                        "fact_name",
                        "observed_at",
                    }
                ),
            )
            catalog.put_observation(
                Observation(
                    local_id=str(record["local_id"]),
                    source=_text(record["source"], "source"),
                    native_id=_text(record["native_id"], "native_id"),
                    record_kind=_text(record["record_kind"], "record_kind", maximum=32),
                    record_local_id=_text(record["record_local_id"], "record_local_id"),
                    fact_name=_text(record["fact_name"], "fact_name", maximum=128),
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        elif kind == "check_time":
            _require_keys(record, frozenset({"kind", "local_id", "source", "checked_at"}))
            source = _text(record["source"], "source")
            if source != record["local_id"]:
                raise ValueError("check time identity does not match source")
            catalog.set_check_time(source, _datetime(record["checked_at"], "checked_at"))
        elif kind == "affinity_evidence":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "artist_local_id",
                        "source",
                        "evidence_kind",
                        "evidence_key",
                        "rank",
                        "observed_at",
                    }
                ),
            )
            raw_rank = record["rank"]
            rank = None if raw_rank is None else _integer(raw_rank, "rank")
            catalog.put_affinity_evidence(
                AffinityEvidence(
                    local_id=str(record["local_id"]),
                    artist_local_id=_text(record["artist_local_id"], "artist_local_id"),
                    source=_text(record["source"], "source"),
                    kind=AffinityEvidenceKind(
                        _text(record["evidence_kind"], "evidence_kind", maximum=32)
                    ),
                    evidence_key=_text(record["evidence_key"], "evidence_key"),
                    rank=rank,
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        elif kind == "watchlist_override":
            _require_keys(
                record,
                frozenset({"kind", "local_id", "artist_local_id", "action", "updated_at"}),
            )
            artist_local_id = _text(record["artist_local_id"], "artist_local_id")
            if artist_local_id != record["local_id"]:
                raise ValueError("watchlist override identity does not match artist")
            catalog.put_watchlist_override(
                WatchlistOverride(
                    artist_local_id=artist_local_id,
                    action=WatchlistAction(_text(record["action"], "action", maximum=16)),
                    updated_at=_datetime(record["updated_at"], "updated_at"),
                )
            )
        elif kind == "local_preference":
            _require_keys(
                record,
                frozenset({"kind", "local_id", "preference_key", "value", "updated_at"}),
            )
            key = LocalPreferenceKey(_text(record["preference_key"], "preference_key", maximum=32))
            if key.value != record["local_id"]:
                raise ValueError("local preference identity does not match key")
            catalog.put_local_preference(
                LocalPreference(
                    key=key,
                    value=_text(record["value"], "value"),
                    updated_at=_datetime(record["updated_at"], "updated_at"),
                )
            )
        elif kind == "refresh_run":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "source",
                        "refresh_kind",
                        "status",
                        "started_at",
                        "finished_at",
                        "summary",
                    }
                ),
            )
            catalog.put_refresh_run(
                RefreshRun(
                    local_id=str(record["local_id"]),
                    source=_text(record["source"], "source"),
                    kind=RefreshKind(_text(record["refresh_kind"], "refresh_kind", maximum=16)),
                    status=RefreshStatus(_text(record["status"], "status", maximum=16)),
                    started_at=_datetime(record["started_at"], "started_at"),
                    finished_at=(
                        None
                        if record["finished_at"] is None
                        else _datetime(record["finished_at"], "finished_at")
                    ),
                    summary=_refresh_summary(record["summary"]),
                )
            )
        elif kind == "source_cursor":
            _require_keys(
                record,
                frozenset({"kind", "local_id", "source", "capability", "cursor", "updated_at"}),
            )
            source = _text(record["source"], "source")
            capability = SourceCapability(_text(record["capability"], "capability", maximum=64))
            if record["local_id"] != _source_cursor_id(source, capability.value):
                raise ValueError("source cursor identity does not match scope")
            catalog.put_source_cursor(
                SourceCursor(
                    source=source,
                    capability=capability,
                    cursor=_text(record["cursor"], "cursor", maximum=2048),
                    updated_at=_datetime(record["updated_at"], "updated_at"),
                )
            )
        elif kind == "source_limit":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "source",
                        "state",
                        "observed_at",
                        "retry_at",
                        "retry_is_exact",
                        "consecutive_limits",
                    }
                ),
            )
            source = _text(record["source"], "source")
            if record["local_id"] != source or type(record["retry_is_exact"]) is not bool:
                raise ValueError("source limit identity or exactness is invalid")
            catalog.put_source_limit(
                SourceLimitObservation(
                    source=source,
                    state=SourceLimitState(_text(record["state"], "state", maximum=32)),
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                    retry_at=(
                        None
                        if record["retry_at"] is None
                        else _datetime(record["retry_at"], "retry_at")
                    ),
                    retry_is_exact=record["retry_is_exact"],
                    consecutive_limits=_integer(record["consecutive_limits"], "consecutive_limits"),
                )
            )
        elif kind == "signal":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "signal_kind",
                        "record_local_id",
                        "provider",
                        "provider_native_id",
                        "fingerprint",
                        "material_version",
                        "explanation",
                        "observed_at",
                    }
                ),
            )
            catalog.put_signal(
                Signal(
                    local_id=str(record["local_id"]),
                    kind=SignalKind(_text(record["signal_kind"], "signal_kind", maximum=16)),
                    record_local_id=_text(record["record_local_id"], "record_local_id"),
                    provider=_text(record["provider"], "provider"),
                    provider_native_id=_text(record["provider_native_id"], "provider_native_id"),
                    fingerprint=_text(record["fingerprint"], "fingerprint", maximum=128),
                    material_version=_text(
                        record["material_version"], "material_version", maximum=128
                    ),
                    explanation=_explanation(record["explanation"]),
                    observed_at=_datetime(record["observed_at"], "observed_at"),
                )
            )
        elif kind == "inbox_entry":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "signal_local_id",
                        "state",
                        "created_at",
                        "updated_at",
                    }
                ),
            )
            catalog.put_inbox_entry(
                InboxEntry(
                    local_id=str(record["local_id"]),
                    signal_local_id=_text(record["signal_local_id"], "signal_local_id"),
                    state=InboxState(_text(record["state"], "state", maximum=16)),
                    created_at=_datetime(record["created_at"], "created_at"),
                    updated_at=_datetime(record["updated_at"], "updated_at"),
                )
            )
        elif kind == "listening_history":
            _require_keys(
                record,
                frozenset(
                    {
                        "kind",
                        "local_id",
                        "source",
                        "played_at",
                        "milliseconds_played",
                        "track_uri",
                        "track_name",
                        "artist_name",
                        "album_name",
                        "reason_start",
                        "reason_end",
                        "shuffle",
                        "skipped",
                        "offline",
                        "incognito",
                        "archive_digest",
                        "imported_at",
                    }
                ),
            )
            boolean_values = [
                record[name] for name in ("shuffle", "skipped", "offline", "incognito")
            ]
            if any(
                value not in (None, 0, 1) or type(value) not in (int, type(None))
                for value in boolean_values
            ):
                raise ValueError("listening history boolean is invalid")
            milliseconds = _integer(record["milliseconds_played"], "milliseconds_played")
            if milliseconds < 0:
                raise ValueError("listening history duration is invalid")
            connection = _connection(catalog)
            connection.execute(
                """INSERT OR IGNORE INTO listening_history (
                    event_id, source, played_at, milliseconds_played, track_uri, track_name,
                    artist_name, album_name, reason_start, reason_end, shuffle, skipped, offline,
                    incognito, archive_digest, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record["local_id"]),
                    _text(record["source"], "source"),
                    _datetime(record["played_at"], "played_at").isoformat(),
                    milliseconds,
                    _text(record["track_uri"], "track_uri"),
                    _text(record["track_name"], "track_name"),
                    _text(record["artist_name"], "artist_name"),
                    _optional_text(record["album_name"], "album_name"),
                    _optional_text(record["reason_start"], "reason_start"),
                    _optional_text(record["reason_end"], "reason_end"),
                    *boolean_values,
                    _text(record["archive_digest"], "archive_digest", maximum=128),
                    _datetime(record["imported_at"], "imported_at").isoformat(),
                ),
            )
        else:
            raise ValueError("unknown portable record kind")


def import_catalog(
    catalog: Catalog,
    source: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> ImportResult:
    """Validate in isolation, then atomically merge one portable catalog."""
    payload = _read_portable(source, max_bytes, max_records)
    canonical, deferred = _validate_document(payload)
    with tempfile.TemporaryDirectory(prefix="music-friend-import-") as temporary:
        staging_path = Path(temporary).resolve(strict=True) / "catalog.sqlite3"
        try:
            with Catalog.open(staging_path) as staging:
                with staging.transaction():
                    _replay(staging, canonical, deferred)
        except sqlite3.IntegrityError as error:
            raise ValueError("portable catalog violates relational integrity") from error
        with catalog.transaction():
            _replay(catalog, canonical, deferred)
    imported_records = payload["records"]
    if not isinstance(imported_records, list):  # validated above; keeps the type boundary explicit
        raise AssertionError("validated records changed type")
    return ImportResult(path=source, record_count=len(imported_records))


def purge_source(catalog: Catalog, source_name: str) -> PurgeResult:
    """Remove one source's mappings, observations, and check time.

    Affected canonical records remain when they have another source, an explicit interest, or a
    retained canonical dependency. Records without any of those retention roots are deleted.
    """
    source = _text(source_name, "source_name")
    connection = _connection(catalog)
    with catalog.transaction():
        connection.execute("DELETE FROM listening_history WHERE source = ?", (source,))
        affected_targets = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT mapping.record_kind, mapping.record_local_id
                FROM record_sources AS mapping
                JOIN source_references AS reference
                  ON reference.id = mapping.source_reference_id
                WHERE reference.source = ?
                """,
                (source,),
            ).fetchall()
        }
        mapping_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM record_sources
                WHERE source_reference_id IN (
                    SELECT id FROM source_references WHERE source = ?
                )
                """,
                (source,),
            ).fetchone()[0]
        )
        observation_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE source = ?", (source,)
            ).fetchone()[0]
        )
        check_time_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM check_times WHERE source = ?", (source,)
            ).fetchone()[0]
        )
        affinity_evidence_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM affinity_evidence WHERE source = ?", (source,)
            ).fetchone()[0]
        )
        source_cursor_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM source_cursors WHERE source = ?", (source,)
            ).fetchone()[0]
        )
        signal_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE provider = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM inbox_entries
                      WHERE signal_local_id = signals.local_id
                        AND state IN ('saved', 'dismissed')
                  )
                """,
                (source,),
            ).fetchone()[0]
        )
        inbox_entry_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM inbox_entries
                WHERE signal_local_id IN (
                    SELECT local_id FROM signals
                    WHERE provider = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM inbox_entries AS retained
                          WHERE retained.signal_local_id = signals.local_id
                            AND retained.state IN ('saved', 'dismissed')
                      )
                )
                """,
                (source,),
            ).fetchone()[0]
        )
        refresh_run_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM refresh_runs WHERE source = ?", (source,)
            ).fetchone()[0]
        )
        catalog.disconnect_source(source)
        for kind in ("release", "event"):
            table = f"{kind}s"
            for target_kind, local_id in affected_targets:
                if target_kind != kind:
                    continue
                connection.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE local_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM record_sources
                          WHERE record_kind = ? AND record_local_id = ?
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM interests
                          WHERE kind = ? AND target_local_id = ?
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM signals
                          WHERE kind = ? AND record_local_id = ?
                      )
                    """,
                    (local_id, kind, local_id, kind, local_id, kind, local_id),
                )
        for kind, local_id in affected_targets:
            if kind != "artist":
                continue
            connection.execute(
                """
                DELETE FROM artists
                WHERE local_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM record_sources
                      WHERE record_kind = 'artist' AND record_local_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM interests
                      WHERE kind = 'artist' AND target_local_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM watchlist_overrides WHERE artist_local_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM affinity_evidence WHERE artist_local_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM release_artists WHERE artist_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM event_artists WHERE artist_id = ?
                  )
                """,
                (local_id, local_id, local_id, local_id, local_id, local_id, local_id),
            )
    return PurgeResult(
        mapping_count=mapping_count,
        observation_count=observation_count,
        check_time_count=check_time_count,
        affinity_evidence_count=affinity_evidence_count,
        source_cursor_count=source_cursor_count,
        signal_count=signal_count,
        inbox_entry_count=inbox_entry_count,
        refresh_run_count=refresh_run_count,
    )


def _validated_entry(parent_fd: int, name: str, expected: tuple[int, int] | None) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("catalog target was substituted")
    if expected is not None and (metadata.st_dev, metadata.st_ino) != expected:
        raise OSError("catalog target was substituted")
    get_effective_user = getattr(os, "geteuid", None)
    if get_effective_user is not None and metadata.st_uid != get_effective_user():
        raise OSError("catalog target is not owned by the current user")
    return True


def delete_catalog(catalog: Catalog) -> None:
    """Close and delete only the catalog that this instance securely opened."""
    path = catalog._database_path
    expected = catalog._database_identity
    catalog.close()
    if os.name == "posix":
        parent_fd = _open_existing_parent_posix(path)
        try:
            if not _validated_entry(parent_fd, path.name, expected):
                raise OSError("catalog target is missing")
            sidecars = (f"{path.name}-wal", f"{path.name}-shm")
            present_sidecars = [
                sidecar for sidecar in sidecars if _validated_entry(parent_fd, sidecar, None)
            ]
            for sidecar in present_sidecars:
                os.unlink(sidecar, dir_fd=parent_fd)
            os.unlink(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("catalog target was substituted")
    if (metadata.st_dev, metadata.st_ino) != expected:
        raise OSError("catalog target was substituted")
    candidates = (Path(f"{path}-wal"), Path(f"{path}-shm"), path)
    present_candidates: list[Path] = []
    for candidate in candidates:
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(candidate_metadata.st_mode) or not stat.S_ISREG(candidate_metadata.st_mode):
            raise OSError("catalog target was substituted")
        present_candidates.append(candidate)
    for candidate in present_candidates:
        candidate.unlink()


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_RECORDS",
    "ExportResult",
    "ImportResult",
    "PurgeResult",
    "delete_catalog",
    "export_catalog",
    "import_catalog",
    "purge_source",
]
