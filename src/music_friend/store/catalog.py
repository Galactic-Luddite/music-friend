"""Secure SQLite-backed catalog.

On POSIX, parent traversal, creation, permission tightening, final-file opening, and cleanup are
anchored to no-follow directory descriptors. Python's standard ``sqlite3`` API cannot open an
existing file descriptor, so SQLite must briefly reopen the validated pathname. The parent and
database descriptors remain held through that call, and descriptor-relative plus pathname
device/inode comparisons run before any PRAGMA or migration. This closes avoidable check/use gaps
and detects a one-way parent or final-path swap. The threat model excludes a malicious process
already running as the same operating-system account, which could swap a path away and back between
individual system calls.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import TracebackType
from urllib.parse import quote

from music_friend.domain import (
    AffinityEvidence,
    AffinityEvidenceKind,
    AffinityScore,
    Artist,
    CatalogSyncCursor,
    Event,
    EventDiscovery,
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
    ReleaseCheckContinuation,
    ReleaseCheckCursor,
    ReleaseDatePrecision,
    ReleaseDiscovery,
    Signal,
    SignalKind,
    SourceCapability,
    SourceCursor,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
    WatchlistAction,
    WatchlistEntry,
    WatchlistInclusionReason,
    WatchlistOverride,
)
from music_friend.domain.affinity import score_affinity
from music_friend.errors import CatalogUnavailableError
from music_friend.store import migrations

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by the documented fallback platform
    fcntl = None  # type: ignore[assignment]

_BUSY_TIMEOUT_MS = 5_000
_MAX_QUERY_LIMIT = 500


class _UnsafeCatalogPath(Exception):
    pass


def _bounded_limit(limit: object) -> int:
    if type(limit) is not int or not 1 <= limit <= _MAX_QUERY_LIMIT:
        raise ValueError("limit must be an integer from 1 through 500")
    return limit


_MATCH_PUNCTUATION = re.compile(r"[-&]+")
_MATCH_WHITESPACE = re.compile(r"\s+")


def _normalize_for_match(text: str) -> str:
    """Fold text for accent- and stylization-insensitive substring matching.

    Applies a Unicode NFKD decomposition (splitting stylized/compatibility forms such as ``Ÿ``
    into a base letter plus combining marks, and folding compatibility variants toward their
    common form), strips the resulting combining marks, case-folds, and collapses ``-``/``&`` and
    surrounding whitespace to a single space so hyphenated or ampersand-joined names compare the
    same as their spaced-out equivalents. Both the query and stored display names are normalized
    through this function before comparison; display names returned to callers are never altered.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    folded = without_marks.casefold()
    despunctuated = _MATCH_PUNCTUATION.sub(" ", folded)
    return _MATCH_WHITESPACE.sub(" ", despunctuated).strip()


def _datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _summary_json(summary: RefreshSummary) -> str:
    return json.dumps(
        {
            "version": 1,
            "metrics": [
                {"kind": metric.kind.value, "count": metric.count} for metric in summary.metrics
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _explanation_json(explanation: Explanation) -> str:
    return json.dumps(
        {
            "version": 1,
            "reasons": [
                {"kind": reason.kind.value, "detail": reason.detail}
                for reason in explanation.reasons
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_object(value: str, name: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError(f"stored {name} is invalid") from error
    if type(decoded) is not dict:
        raise ValueError(f"stored {name} is invalid")
    return decoded


def _decode_summary(value: str) -> RefreshSummary:
    decoded = _json_object(value, "refresh summary")
    if set(decoded) != {"version", "metrics"} or decoded["version"] != 1:
        raise ValueError("stored refresh summary is invalid")
    raw_metrics = decoded["metrics"]
    if type(raw_metrics) is not list:
        raise ValueError("stored refresh summary is invalid")
    metrics: list[RefreshMetric] = []
    for raw_metric in raw_metrics:
        if type(raw_metric) is not dict or set(raw_metric) != {"kind", "count"}:
            raise ValueError("stored refresh summary is invalid")
        metrics.append(
            RefreshMetric(
                RefreshMetricKind(raw_metric["kind"]),
                raw_metric["count"],
            )
        )
    return RefreshSummary(tuple(metrics))


def _decode_explanation(value: str) -> Explanation:
    decoded = _json_object(value, "signal explanation")
    if set(decoded) != {"version", "reasons"} or decoded["version"] != 1:
        raise ValueError("stored signal explanation is invalid")
    raw_reasons = decoded["reasons"]
    if type(raw_reasons) is not list:
        raise ValueError("stored signal explanation is invalid")
    reasons: list[ExplanationReason] = []
    for raw_reason in raw_reasons:
        if type(raw_reason) is not dict or set(raw_reason) != {"kind", "detail"}:
            raise ValueError("stored signal explanation is invalid")
        detail = raw_reason["detail"]
        if detail is not None and type(detail) is not str:
            raise ValueError("stored signal explanation is invalid")
        reasons.append(
            ExplanationReason(
                ExplanationReasonKind(raw_reason["kind"]),
                detail,
            )
        )
    return Explanation(tuple(reasons))


def _absolute_without_resolving(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _validate_path_shape(path: Path) -> None:
    if ".." in path.parts or not path.name:
        raise _UnsafeCatalogPath


def _open_parent_posix(path: Path) -> int:
    """Traverse to the parent using only no-follow directory descriptors."""
    _validate_path_shape(path)
    parent = path.parent
    components = parent.parts[1:]
    if not components:
        raise _UnsafeCatalogPath
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(path.anchor, directory_flags)
    try:
        for position, component in enumerate(components):
            created = False
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                else:
                    created = True
                    # A caller-controlled umask can remove owner search permission,
                    # which must be restored before the new directory can be opened.
                    # This remains anchored and refuses a replacement symlink.
                    os.chmod(
                        component,
                        0o700,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            try:
                metadata = os.fstat(next_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise _UnsafeCatalogPath
                if created or position == len(components) - 1:
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


def _validate_and_prepare_parent_fallback(path: Path) -> None:
    """Best-effort non-POSIX fallback where descriptor traversal is unavailable."""
    _validate_path_shape(path)
    parent = path.parent
    current = Path(path.anchor)
    for component in parent.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise _UnsafeCatalogPath
            os.chmod(current, 0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _UnsafeCatalogPath
    os.chmod(parent, 0o700)


def _cleanup_created_posix(parent_fd: int, expected: os.stat_result) -> None:
    """Unlink the one owned regular entry matching a newly created inode."""
    try:
        names = os.listdir(parent_fd)
    except OSError:
        return
    for name in names:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            continue
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_uid == os.geteuid()
            and current.st_nlink == 1
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        ):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            return


def _prepare_database_posix(parent_fd: int, name: str) -> tuple[int, os.stat_result, bool]:
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    created = False
    descriptor = -1
    created_identity: os.stat_result | None = None
    opened: os.stat_result | None = None
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise _UnsafeCatalogPath from None
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            else:
                created = True
                created_identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise _UnsafeCatalogPath
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _UnsafeCatalogPath
        if created and opened.st_uid != os.geteuid():
            raise _UnsafeCatalogPath
        if (
            created
            and created_identity is not None
            and (
                opened.st_dev,
                opened.st_ino,
            )
            != (created_identity.st_dev, created_identity.st_ino)
        ):
            raise _UnsafeCatalogPath
        if not created and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _UnsafeCatalogPath
        os.fchmod(descriptor, 0o600)
        return descriptor, opened, created
    except BaseException:
        if descriptor >= 0:
            if created:
                expected = opened if opened is not None else created_identity
                if expected is not None:
                    _cleanup_created_posix(parent_fd, expected)
            os.close(descriptor)
        raise


def _lock_parent_posix(descriptor: int) -> None:
    if fcntl is None:
        raise _UnsafeCatalogPath
    deadline = time.monotonic() + (_BUSY_TIMEOUT_MS / 1_000)
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("catalog open lock timed out") from None
            time.sleep(0.01)


def _prepare_database_fallback(path: Path) -> tuple[os.stat_result, bool]:
    created = False
    created_identity: os.stat_result | None = None
    opened: os.stat_result | None = None
    descriptor = -1
    try:
        try:
            before = path.lstat()
        except FileNotFoundError:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            created_identity = path.lstat()
        else:
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise _UnsafeCatalogPath
            descriptor = os.open(path, os.O_RDWR)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _UnsafeCatalogPath
        expected = created_identity if created else before
        if expected is None or (opened.st_dev, opened.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise _UnsafeCatalogPath
        os.chmod(path, 0o600)
        return opened, created
    except BaseException:
        if created and created_identity is not None:
            _remove_new_file_after_failed_open(path, created_identity)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_new_file_after_failed_open(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
        get_effective_user = getattr(os, "geteuid", None)
        owned = get_effective_user is None or current.st_uid == get_effective_user()
        if (
            stat.S_ISREG(current.st_mode)
            and owned
            and current.st_nlink == 1
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        ):
            path.unlink()
    except OSError:
        pass


class Catalog:
    """A private local catalog containing only canonical domain records."""

    _connection: sqlite3.Connection | None
    _transaction_depth: int
    _database_path: Path
    _database_identity: tuple[int, int]

    def __init__(self) -> None:
        raise TypeError("Catalog instances must be created with Catalog.open(Path(...))")

    @classmethod
    def _from_connection(
        cls,
        connection: sqlite3.Connection,
        database_path: Path,
        database_identity: tuple[int, int],
    ) -> Catalog:
        catalog = object.__new__(cls)
        catalog._connection = connection
        catalog._transaction_depth = 0
        catalog._database_path = database_path
        catalog._database_identity = database_identity
        return catalog

    @classmethod
    def open(cls, path: Path) -> Catalog:
        """Open a securely permissioned SQLite catalog at an explicit path."""
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        database_path = _absolute_without_resolving(path)
        connection: sqlite3.Connection | None = None
        opened: os.stat_result | None = None
        created = False
        parent_fd = -1
        database_fd = -1
        try:
            if os.name == "posix":
                parent_fd = _open_parent_posix(database_path)
                database_fd, opened, created = _prepare_database_posix(
                    parent_fd, database_path.name
                )
                _lock_parent_posix(parent_fd)
            else:
                _validate_and_prepare_parent_fallback(database_path)
                opened, created = _prepare_database_fallback(database_path)
            uri = f"file:{quote(str(database_path), safe='/')}?mode=rw"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            if os.name == "posix":
                held = os.fstat(database_fd)
                anchored = os.stat(database_path.name, dir_fd=parent_fd, follow_symlinks=False)
                reached = database_path.lstat()
                expected = (opened.st_dev, opened.st_ino)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or not stat.S_ISREG(anchored.st_mode)
                    or stat.S_ISLNK(reached.st_mode)
                    or not stat.S_ISREG(reached.st_mode)
                    or (held.st_dev, held.st_ino) != expected
                    or (anchored.st_dev, anchored.st_ino) != expected
                    or (reached.st_dev, reached.st_ino) != expected
                ):
                    raise _UnsafeCatalogPath
            else:
                reached = database_path.lstat()
                if (
                    stat.S_ISLNK(reached.st_mode)
                    or not stat.S_ISREG(reached.st_mode)
                    or (reached.st_dev, reached.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise _UnsafeCatalogPath
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise sqlite3.OperationalError("WAL mode unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            migrations.apply_migrations(connection)
            if database_fd >= 0:
                os.close(database_fd)
                database_fd = -1
            if parent_fd >= 0:
                os.close(parent_fd)
                parent_fd = -1
            return cls._from_connection(
                connection,
                database_path,
                (opened.st_dev, opened.st_ino),
            )
        except (OSError, sqlite3.Error, _UnsafeCatalogPath) as error:
            if connection is not None:
                connection.close()
            if created and opened is not None:
                if parent_fd >= 0:
                    _cleanup_created_posix(parent_fd, opened)
                else:
                    _remove_new_file_after_failed_open(database_path, opened)
            if database_fd >= 0:
                os.close(database_fd)
            if parent_fd >= 0:
                os.close(parent_fd)
            raise CatalogUnavailableError() from error

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise CatalogUnavailableError()
        return self._connection

    def close(self) -> None:
        """Close the catalog; repeated calls are harmless."""
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        connection.close()

    def __enter__(self) -> Catalog:
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes atomically, including writes made by repository methods."""
        connection = self._require_connection()
        outermost = self._transaction_depth == 0
        savepoint = None if outermost else f"music_friend_nested_{self._transaction_depth}"
        if outermost:
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.execute(f"SAVEPOINT {savepoint}")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if outermost:
                connection.rollback()
            else:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                connection.commit()
            else:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _replace_source_refs(
        self,
        record_kind: str,
        record_local_id: str,
        references: tuple[SourceReference, ...],
    ) -> None:
        connection = self._require_connection()
        reference_ids: list[int] = []
        for position, reference in enumerate(references):
            connection.execute(
                """
                INSERT INTO source_references (source, native_id, canonical_url, observed_at, confidence)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source, native_id) DO UPDATE SET
                    canonical_url = excluded.canonical_url,
                    observed_at = excluded.observed_at,
                    confidence = excluded.confidence
                """,
                (
                    reference.source,
                    reference.native_id,
                    reference.canonical_url,
                    _datetime_text(reference.observed_at),
                    reference.confidence.value,
                ),
            )
            source_row = connection.execute(
                "SELECT id FROM source_references WHERE source = ? AND native_id = ?",
                (reference.source, reference.native_id),
            ).fetchone()
            if source_row is None:
                raise sqlite3.IntegrityError("source reference was not persisted")
            reference_ids.append(int(source_row[0]))

        if reference_ids:
            placeholders = ", ".join("?" for _ in reference_ids)
            connection.execute(
                f"""
                DELETE FROM record_sources
                WHERE record_kind = ? AND record_local_id = ?
                  AND source_reference_id NOT IN ({placeholders})
                """,
                (record_kind, record_local_id, *reference_ids),
            )
        else:
            connection.execute(
                "DELETE FROM record_sources WHERE record_kind = ? AND record_local_id = ?",
                (record_kind, record_local_id),
            )
        connection.execute(
            """
            UPDATE record_sources SET position = -position - 1
            WHERE record_kind = ? AND record_local_id = ?
            """,
            (record_kind, record_local_id),
        )
        for position, source_reference_id in enumerate(reference_ids):
            connection.execute(
                """
                INSERT INTO record_sources
                    (record_kind, record_local_id, source_reference_id, position)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (record_kind, record_local_id, source_reference_id)
                DO UPDATE SET position = excluded.position
                """,
                (record_kind, record_local_id, source_reference_id, position),
            )
        connection.execute(
            """
            DELETE FROM source_references
            WHERE NOT EXISTS (
                SELECT 1 FROM record_sources
                WHERE record_sources.source_reference_id = source_references.id
            )
            """
        )

    def _source_refs(self, record_kind: str, record_local_id: str) -> tuple[SourceReference, ...]:
        rows = (
            self._require_connection()
            .execute(
                """
            SELECT reference.source, reference.native_id, reference.canonical_url,
                   reference.observed_at, reference.confidence
            FROM record_sources AS mapping
            JOIN source_references AS reference ON reference.id = mapping.source_reference_id
            WHERE mapping.record_kind = ? AND mapping.record_local_id = ?
            ORDER BY mapping.position
            """,
                (record_kind, record_local_id),
            )
            .fetchall()
        )
        from music_friend.domain import IdentityConfidence
        return tuple(
            SourceReference(
                source=str(row[0]),
                native_id=str(row[1]),
                canonical_url=None if row[2] is None else str(row[2]),
                observed_at=datetime.fromisoformat(str(row[3])),
                confidence=IdentityConfidence(str(row[4])) if row[4] else IdentityConfidence.SOURCE_ONLY,
            )
            for row in rows
        )

    def put_artist(self, artist: Artist) -> None:
        """Insert or replace one canonical artist."""
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO artists (local_id, display_name, identity_confidence, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    identity_confidence = excluded.identity_confidence,
                    observed_at = excluded.observed_at
                """,
                (
                    artist.local_id,
                    artist.display_name,
                    artist.identity_confidence.value,
                    _datetime_text(artist.observed_at),
                ),
            )
            self._replace_source_refs("artist", artist.local_id, artist.source_refs)

    def get_artist(self, local_id: str) -> Artist | None:
        """Read one canonical artist by local identifier."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, display_name, identity_confidence, observed_at
            FROM artists WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Artist(
            local_id=str(row[0]),
            display_name=str(row[1]),
            source_refs=self._source_refs("artist", str(row[0])),
            identity_confidence=IdentityConfidence(str(row[2])),
            observed_at=datetime.fromisoformat(str(row[3])),
        )

    def search_artists(self, query: str, *, limit: int) -> tuple[Artist, ...]:
        """Search canonical artist names with an explicit bound and stable ordering.

        Matching is case-insensitive and accent/stylization-insensitive: both the query and each
        stored ``display_name`` are folded through :func:`_normalize_for_match` (Unicode NFKD,
        combining marks stripped, case-folded, ``-``/``&`` and repeated whitespace collapsed)
        before comparison. Because SQLite's ``LIKE ... COLLATE NOCASE`` only folds ASCII case and
        cannot express that fold, exact SQL-side narrowing would silently miss accented and
        stylized matches (e.g. a query of ``elodie cafe`` must find a stored ``Élodie Café``), so
        matching runs in Python over every stored display name; the display name itself is never
        altered, only the comparison. A literal ``%`` or ``_`` in the query still matches
        literally, since comparison is now plain substring containment rather than SQL ``LIKE``.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("query must contain 1..256 characters")
        selected_limit = _bounded_limit(limit)
        normalized_query = _normalize_for_match(query)
        rows = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, display_name FROM artists
            ORDER BY display_name COLLATE NOCASE, display_name, local_id
            """
            )
            .fetchall()
        )
        matched_ids: list[str] = []
        if normalized_query:
            for row in rows:
                if normalized_query in _normalize_for_match(str(row[1])):
                    matched_ids.append(str(row[0]))
                    if len(matched_ids) == selected_limit:
                        break
        artists: list[Artist] = []
        for local_id in matched_ids:
            artist = self.get_artist(local_id)
            if artist is None:
                raise sqlite3.IntegrityError("artist disappeared during search")
            artists.append(artist)
        return tuple(artists)

    def put_release(self, release: Release) -> None:
        """Insert or replace one canonical release and its ordered artist links."""
        with self.transaction():
            connection = self._require_connection()
            connection.execute(
                """
                INSERT INTO releases
                    (local_id, title, release_type, release_date, date_precision, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    title = excluded.title,
                    release_type = excluded.release_type,
                    release_date = excluded.release_date,
                    date_precision = excluded.date_precision,
                    observed_at = excluded.observed_at
                """,
                (
                    release.local_id,
                    release.title,
                    release.release_type,
                    release.release_date.isoformat(),
                    release.date_precision.value,
                    _datetime_text(release.observed_at),
                ),
            )
            connection.execute(
                "DELETE FROM release_artists WHERE release_id = ?", (release.local_id,)
            )
            for position, artist_id in enumerate(release.artist_refs):
                connection.execute(
                    "INSERT INTO release_artists (release_id, artist_id, position) VALUES (?, ?, ?)",
                    (release.local_id, artist_id, position),
                )
            self._replace_source_refs("release", release.local_id, release.source_refs)

    def get_release(self, local_id: str) -> Release | None:
        """Read one canonical release by local identifier."""
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT local_id, title, release_type, release_date, date_precision, observed_at
            FROM releases WHERE local_id = ?
            """,
            (local_id,),
        ).fetchone()
        if row is None:
            return None
        artist_refs = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT artist_id FROM release_artists WHERE release_id = ? ORDER BY position",
                (local_id,),
            ).fetchall()
        )
        return Release(
            local_id=str(row[0]),
            title=str(row[1]),
            release_type=str(row[2]),
            release_date=datetime.strptime(str(row[3]), "%Y-%m-%d").date(),
            date_precision=ReleaseDatePrecision(str(row[4])),
            artist_refs=artist_refs,
            source_refs=self._source_refs("release", str(row[0])),
            observed_at=datetime.fromisoformat(str(row[5])),
        )

    def put_event(self, event: Event) -> None:
        """Insert or replace one canonical event and its ordered links."""
        with self.transaction():
            connection = self._require_connection()
            connection.execute(
                """
                INSERT INTO events
                    (local_id, title, venue_name, locality, starts_at, time_precision, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    title = excluded.title,
                    venue_name = excluded.venue_name,
                    locality = excluded.locality,
                    starts_at = excluded.starts_at,
                    time_precision = excluded.time_precision,
                    observed_at = excluded.observed_at
                """,
                (
                    event.local_id,
                    event.title,
                    event.venue_name,
                    event.locality,
                    None if event.starts_at is None else _datetime_text(event.starts_at),
                    event.time_precision,
                    _datetime_text(event.observed_at),
                ),
            )
            connection.execute("DELETE FROM event_artists WHERE event_id = ?", (event.local_id,))
            for position, artist_id in enumerate(event.artist_refs):
                connection.execute(
                    "INSERT INTO event_artists (event_id, artist_id, position) VALUES (?, ?, ?)",
                    (event.local_id, artist_id, position),
                )
            connection.execute(
                "DELETE FROM event_source_links WHERE event_id = ?", (event.local_id,)
            )
            for position, source_link in enumerate(event.source_links):
                connection.execute(
                    """
                    INSERT INTO event_source_links (event_id, source_link, position)
                    VALUES (?, ?, ?)
                    """,
                    (event.local_id, source_link, position),
                )
            self._replace_source_refs("event", event.local_id, event.source_refs)

    def get_event(self, local_id: str) -> Event | None:
        """Read one canonical event by local identifier."""
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT local_id, title, venue_name, locality, starts_at, time_precision, observed_at
            FROM events WHERE local_id = ?
            """,
            (local_id,),
        ).fetchone()
        if row is None:
            return None
        artist_refs = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT artist_id FROM event_artists WHERE event_id = ? ORDER BY position",
                (local_id,),
            ).fetchall()
        )
        source_links = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT source_link FROM event_source_links WHERE event_id = ? ORDER BY position",
                (local_id,),
            ).fetchall()
        )
        return Event(
            local_id=str(row[0]),
            title=str(row[1]),
            artist_refs=artist_refs,
            venue_name=None if row[2] is None else str(row[2]),
            locality=None if row[3] is None else str(row[3]),
            starts_at=None if row[4] is None else datetime.fromisoformat(str(row[4])),
            time_precision=None if row[5] is None else str(row[5]),
            source_links=source_links,
            source_refs=self._source_refs("event", str(row[0])),
            observed_at=datetime.fromisoformat(str(row[6])),
        )

    def _canonical_exists(self, kind: str, local_id: str) -> bool:
        tables = {"artist": "artists", "release": "releases", "event": "events"}
        table = tables.get(kind)
        if table is None:
            return False
        row = (
            self._require_connection()
            .execute(f"SELECT 1 FROM {table} WHERE local_id = ?", (local_id,))
            .fetchone()
        )
        return row is not None

    def put_interest(self, interest: Interest) -> None:
        """Insert or replace one user interest without embedding source credentials."""
        with self.transaction():
            if not self._canonical_exists(interest.kind.value, interest.target_local_id):
                raise sqlite3.IntegrityError("interest target does not exist")
            self._require_connection().execute(
                """
                INSERT INTO interests
                    (local_id, kind, target_local_id, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    kind = excluded.kind,
                    target_local_id = excluded.target_local_id,
                    status = excluded.status,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    interest.local_id,
                    interest.kind.value,
                    interest.target_local_id,
                    interest.status.value,
                    interest.created_by,
                    _datetime_text(interest.created_at),
                    _datetime_text(interest.updated_at),
                ),
            )

    def get_interest(self, local_id: str) -> Interest | None:
        """Read one canonical interest by local identifier."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, kind, target_local_id, status, created_by, created_at, updated_at
            FROM interests WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Interest(
            local_id=str(row[0]),
            kind=InterestKind(str(row[1])),
            target_local_id=str(row[2]),
            status=InterestStatus(str(row[3])),
            created_by=str(row[4]),
            created_at=datetime.fromisoformat(str(row[5])),
            updated_at=datetime.fromisoformat(str(row[6])),
        )

    def put_observation(self, observation: Observation) -> None:
        """Insert or replace one source observation for an existing canonical record."""
        with self.transaction():
            if not self._canonical_exists(observation.record_kind, observation.record_local_id):
                raise sqlite3.IntegrityError("observation target does not exist")
            source_mapping = (
                self._require_connection()
                .execute(
                    """
                SELECT reference.id
                FROM record_sources AS mapping
                JOIN source_references AS reference ON reference.id = mapping.source_reference_id
                WHERE mapping.record_kind = ? AND mapping.record_local_id = ?
                  AND reference.source = ? AND reference.native_id = ?
                """,
                    (
                        observation.record_kind,
                        observation.record_local_id,
                        observation.source,
                        observation.native_id,
                    ),
                )
                .fetchone()
            )
            if source_mapping is None:
                raise sqlite3.IntegrityError("observation source mapping does not exist")
            self._require_connection().execute(
                """
                INSERT INTO observations
                    (local_id, source_reference_id, source, native_id, record_kind,
                     record_local_id, fact_name, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    source_reference_id = excluded.source_reference_id,
                    source = excluded.source,
                    native_id = excluded.native_id,
                    record_kind = excluded.record_kind,
                    record_local_id = excluded.record_local_id,
                    fact_name = excluded.fact_name,
                    observed_at = excluded.observed_at
                """,
                (
                    observation.local_id,
                    int(source_mapping[0]),
                    observation.source,
                    observation.native_id,
                    observation.record_kind,
                    observation.record_local_id,
                    observation.fact_name,
                    _datetime_text(observation.observed_at),
                ),
            )

    def get_observation(self, local_id: str) -> Observation | None:
        """Read one canonical observation by local identifier."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, source, native_id, record_kind, record_local_id,
                   fact_name, observed_at
            FROM observations WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Observation(
            local_id=str(row[0]),
            source=str(row[1]),
            native_id=str(row[2]),
            record_kind=str(row[3]),
            record_local_id=str(row[4]),
            fact_name=str(row[5]),
            observed_at=datetime.fromisoformat(str(row[6])),
        )

    def put_affinity_evidence(self, evidence: AffinityEvidence) -> None:
        """Upsert one bounded affinity fact for an existing canonical artist."""
        with self.transaction():
            if not self._canonical_exists("artist", evidence.artist_local_id):
                raise sqlite3.IntegrityError("affinity evidence artist does not exist")
            self._require_connection().execute(
                """
                INSERT INTO affinity_evidence
                    (local_id, artist_local_id, source, kind, evidence_key, rank, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    artist_local_id = excluded.artist_local_id,
                    source = excluded.source,
                    kind = excluded.kind,
                    evidence_key = excluded.evidence_key,
                    rank = excluded.rank,
                    observed_at = excluded.observed_at
                """,
                (
                    evidence.local_id,
                    evidence.artist_local_id,
                    evidence.source,
                    evidence.kind.value,
                    evidence.evidence_key,
                    evidence.rank,
                    _datetime_text(evidence.observed_at),
                ),
            )

    def get_affinity_evidence(self, local_id: str) -> AffinityEvidence | None:
        """Read one affinity fact by its local identity."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, artist_local_id, source, kind, evidence_key, rank, observed_at
            FROM affinity_evidence WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return AffinityEvidence(
            local_id=str(row[0]),
            artist_local_id=str(row[1]),
            source=str(row[2]),
            kind=AffinityEvidenceKind(str(row[3])),
            evidence_key=str(row[4]),
            rank=None if row[5] is None else int(row[5]),
            observed_at=datetime.fromisoformat(str(row[6])),
        )

    def list_affinity_evidence(
        self, artist_local_id: str, *, limit: int
    ) -> tuple[AffinityEvidence, ...]:
        """List affinity facts for one artist in stable kind/key/identity order."""
        selected_limit = _bounded_limit(limit)
        rows = (
            self._require_connection()
            .execute(
                """
            SELECT local_id FROM affinity_evidence
            WHERE artist_local_id = ?
            ORDER BY kind, evidence_key, local_id
            LIMIT ?
            """,
                (artist_local_id, selected_limit),
            )
            .fetchall()
        )
        records: list[AffinityEvidence] = []
        for row in rows:
            evidence = self.get_affinity_evidence(str(row[0]))
            if evidence is None:
                raise sqlite3.IntegrityError("affinity evidence disappeared during list")
            records.append(evidence)
        return tuple(records)

    def replace_affinity_evidence(
        self,
        source: str,
        kind: AffinityEvidenceKind,
        evidence: tuple[AffinityEvidence, ...],
    ) -> None:
        """Replace one source capability's facts in its own atomic transaction."""
        if not isinstance(source, str) or not source.strip() or len(source) > 4096:
            raise ValueError("source must be a non-empty string")
        if not isinstance(kind, AffinityEvidenceKind):
            raise ValueError("kind must be an AffinityEvidenceKind")
        if not isinstance(evidence, tuple) or len(evidence) > 100_000:
            raise ValueError("evidence must be a bounded tuple")
        if any(
            not isinstance(item, AffinityEvidence) or item.source != source or item.kind is not kind
            for item in evidence
        ):
            raise ValueError("replacement evidence must match source and kind")
        with self.transaction():
            self._require_connection().execute(
                "DELETE FROM affinity_evidence WHERE source = ? AND kind = ?",
                (source, kind.value),
            )
            for item in evidence:
                self.put_affinity_evidence(item)

    def put_watchlist_override(self, override: WatchlistOverride) -> None:
        """Upsert one manual watchlist action for an existing artist."""
        with self.transaction():
            if not self._canonical_exists("artist", override.artist_local_id):
                raise sqlite3.IntegrityError("watchlist artist does not exist")
            self._require_connection().execute(
                """
                INSERT INTO watchlist_overrides (artist_local_id, action, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (artist_local_id) DO UPDATE SET
                    action = excluded.action,
                    updated_at = excluded.updated_at
                """,
                (
                    override.artist_local_id,
                    override.action.value,
                    _datetime_text(override.updated_at),
                ),
            )

    def get_watchlist_override(self, artist_local_id: str) -> WatchlistOverride | None:
        """Read the manual action for one artist, if present."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT artist_local_id, action, updated_at
            FROM watchlist_overrides WHERE artist_local_id = ?
            """,
                (artist_local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return WatchlistOverride(
            artist_local_id=str(row[0]),
            action=WatchlistAction(str(row[1])),
            updated_at=datetime.fromisoformat(str(row[2])),
        )

    def list_watchlist_overrides(self, *, limit: int) -> tuple[WatchlistOverride, ...]:
        """List manual watchlist actions in stable artist identity order."""
        selected_limit = _bounded_limit(limit)
        return tuple(
            WatchlistOverride(
                artist_local_id=str(row[0]),
                action=WatchlistAction(str(row[1])),
                updated_at=datetime.fromisoformat(str(row[2])),
            )
            for row in self._require_connection()
            .execute(
                """
                SELECT artist_local_id, action, updated_at
                FROM watchlist_overrides ORDER BY artist_local_id LIMIT ?
                """,
                (selected_limit,),
            )
            .fetchall()
        )

    def remove_watchlist_override(self, artist_local_id: str) -> None:
        """Remove a manual action so later services can recompute automatic eligibility."""
        with self.transaction():
            self._require_connection().execute(
                "DELETE FROM watchlist_overrides WHERE artist_local_id = ?",
                (artist_local_id,),
            )

    def get_affinity_score(self, artist_local_id: str) -> AffinityScore:
        """Calculate affinity-v1 from all stored evidence for one artist."""
        rows = (
            self._require_connection()
            .execute(
                """
            SELECT local_id FROM affinity_evidence
            WHERE artist_local_id = ?
            ORDER BY kind, evidence_key, local_id
            """,
                (artist_local_id,),
            )
            .fetchall()
        )
        evidence: list[AffinityEvidence] = []
        for row in rows:
            item = self.get_affinity_evidence(str(row[0]))
            if item is None:
                raise sqlite3.IntegrityError("affinity evidence disappeared during score")
            evidence.append(item)
        return score_affinity(tuple(evidence))

    def list_watchlist(self, *, limit: int) -> tuple[WatchlistEntry, ...]:
        """List the computed watchlist with stable manual and automatic groups."""
        selected_limit = _bounded_limit(limit)
        return self._watchlist_entries()[:selected_limit]

    def explain_watchlist(self, artist_local_id: str) -> WatchlistEntry | None:
        """Return one included artist's reason and complete affinity explanation."""
        for entry in self._watchlist_entries():
            if entry.artist.local_id == artist_local_id:
                return entry
        return None

    def _watchlist_entries(self) -> tuple[WatchlistEntry, ...]:
        artist_ids = tuple(
            str(row[0])
            for row in self._require_connection()
            .execute("SELECT local_id FROM artists ORDER BY local_id")
            .fetchall()
        )
        artists: dict[str, Artist] = {}
        scores: dict[str, AffinityScore] = {}
        for artist_id in artist_ids:
            artist = self.get_artist(artist_id)
            if artist is None:
                raise sqlite3.IntegrityError("artist disappeared during watchlist calculation")
            artists[artist_id] = artist
            scores[artist_id] = self.get_affinity_score(artist_id)
        overrides = {
            str(row[0]): WatchlistAction(str(row[1]))
            for row in self._require_connection()
            .execute("SELECT artist_local_id, action FROM watchlist_overrides")
            .fetchall()
        }

        eligible = [
            artist_id
            for artist_id in artist_ids
            if scores[artist_id].total_points > 0
            and overrides.get(artist_id) is not WatchlistAction.MUTE
        ]
        eligible.sort(
            key=lambda artist_id: self._affinity_order(artists[artist_id], scores[artist_id])
        )
        automatic_ids = frozenset(eligible[:50])
        included_ids = automatic_ids | frozenset(
            artist_id
            for artist_id, action in overrides.items()
            if action in {WatchlistAction.ADD, WatchlistAction.PIN}
        )
        entries = [
            WatchlistEntry(
                artist=artists[artist_id],
                inclusion_reason=self._inclusion_reason(artist_id, overrides),
                affinity=scores[artist_id],
            )
            for artist_id in included_ids
        ]
        group_order = {
            WatchlistInclusionReason.PINNED: 0,
            WatchlistInclusionReason.MANUALLY_ADDED: 1,
            WatchlistInclusionReason.AUTOMATIC: 2,
        }
        entries.sort(
            key=lambda entry: (
                group_order[entry.inclusion_reason],
                *self._affinity_order(entry.artist, entry.affinity),
            )
        )
        return tuple(entries)

    @staticmethod
    def _affinity_order(artist: Artist, score: AffinityScore) -> tuple[int, str, str]:
        return (-score.total_points, artist.display_name.casefold(), artist.local_id)

    @staticmethod
    def _inclusion_reason(
        artist_local_id: str, overrides: dict[str, WatchlistAction]
    ) -> WatchlistInclusionReason:
        action = overrides.get(artist_local_id)
        if action is WatchlistAction.PIN:
            return WatchlistInclusionReason.PINNED
        if action is WatchlistAction.ADD:
            return WatchlistInclusionReason.MANUALLY_ADDED
        return WatchlistInclusionReason.AUTOMATIC

    def put_local_preference(self, preference: LocalPreference) -> None:
        """Upsert one closed, noncredential local preference."""
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO local_preferences (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (preference.key.value, preference.value, _datetime_text(preference.updated_at)),
            )

    def get_local_preference(self, key: LocalPreferenceKey) -> LocalPreference | None:
        """Read one local preference."""
        if not isinstance(key, LocalPreferenceKey):
            raise ValueError("key must be a LocalPreferenceKey")
        row = (
            self._require_connection()
            .execute(
                "SELECT key, value, updated_at FROM local_preferences WHERE key = ?",
                (key.value,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return LocalPreference(
            key=LocalPreferenceKey(str(row[0])),
            value=str(row[1]),
            updated_at=datetime.fromisoformat(str(row[2])),
        )

    def list_local_preferences(self, *, limit: int) -> tuple[LocalPreference, ...]:
        """List closed local preferences in stable key order."""
        selected_limit = _bounded_limit(limit)
        return tuple(
            LocalPreference(
                key=LocalPreferenceKey(str(row[0])),
                value=str(row[1]),
                updated_at=datetime.fromisoformat(str(row[2])),
            )
            for row in self._require_connection()
            .execute(
                """
                SELECT key, value, updated_at FROM local_preferences
                ORDER BY key LIMIT ?
                """,
                (selected_limit,),
            )
            .fetchall()
        )

    def remove_local_preference(self, key: LocalPreferenceKey) -> None:
        """Delete one optional local preference."""
        if not isinstance(key, LocalPreferenceKey):
            raise ValueError("key must be a LocalPreferenceKey")
        with self.transaction():
            self._require_connection().execute(
                "DELETE FROM local_preferences WHERE key = ?", (key.value,)
            )

    def put_refresh_run(self, run: RefreshRun) -> None:
        """Upsert one redacted refresh lifecycle record."""
        encoded_summary = _summary_json(run.summary)
        if len(encoded_summary) > 4096:
            raise ValueError("refresh summary is too large")
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO refresh_runs
                    (local_id, source, kind, status, started_at, finished_at, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    source = excluded.source,
                    kind = excluded.kind,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    summary_json = excluded.summary_json
                """,
                (
                    run.local_id,
                    run.source,
                    run.kind.value,
                    run.status.value,
                    _datetime_text(run.started_at),
                    None if run.finished_at is None else _datetime_text(run.finished_at),
                    encoded_summary,
                ),
            )

    def get_refresh_run(self, local_id: str) -> RefreshRun | None:
        """Read one refresh lifecycle record."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, source, kind, status, started_at, finished_at, summary_json
            FROM refresh_runs WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return RefreshRun(
            local_id=str(row[0]),
            source=str(row[1]),
            kind=RefreshKind(str(row[2])),
            status=RefreshStatus(str(row[3])),
            started_at=datetime.fromisoformat(str(row[4])),
            finished_at=None if row[5] is None else datetime.fromisoformat(str(row[5])),
            summary=_decode_summary(str(row[6])),
        )

    def list_refresh_runs(self, *, limit: int) -> tuple[RefreshRun, ...]:
        """List recent refresh records in stable newest-first order."""
        selected_limit = _bounded_limit(limit)
        records: list[RefreshRun] = []
        for row in (
            self._require_connection()
            .execute(
                """
            SELECT local_id FROM refresh_runs
            ORDER BY started_at DESC, local_id LIMIT ?
            """,
                (selected_limit,),
            )
            .fetchall()
        ):
            run = self.get_refresh_run(str(row[0]))
            if run is None:
                raise sqlite3.IntegrityError("refresh run disappeared during list")
            records.append(run)
        return tuple(records)

    def put_source_cursor(self, cursor: SourceCursor) -> None:
        """Upsert one opaque source-and-capability continuation cursor."""
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO source_cursors (source, capability, cursor, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source, capability) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.source,
                    cursor.capability.value,
                    cursor.cursor,
                    _datetime_text(cursor.updated_at),
                ),
            )

    def get_source_cursor(self, source: str, capability: SourceCapability) -> SourceCursor | None:
        """Read one source-and-capability cursor."""
        if not isinstance(capability, SourceCapability):
            raise ValueError("capability must be a SourceCapability")
        row = (
            self._require_connection()
            .execute(
                """
            SELECT source, capability, cursor, updated_at FROM source_cursors
            WHERE source = ? AND capability = ?
            """,
                (source, capability.value),
            )
            .fetchone()
        )
        if row is None:
            return None
        return SourceCursor(
            source=str(row[0]),
            capability=SourceCapability(str(row[1])),
            cursor=str(row[2]),
            updated_at=datetime.fromisoformat(str(row[3])),
        )

    def list_source_cursors(self, source: str, *, limit: int) -> tuple[SourceCursor, ...]:
        """List one source's cursors in stable capability order."""
        selected_limit = _bounded_limit(limit)
        return tuple(
            SourceCursor(
                source=str(row[0]),
                capability=SourceCapability(str(row[1])),
                cursor=str(row[2]),
                updated_at=datetime.fromisoformat(str(row[3])),
            )
            for row in self._require_connection()
            .execute(
                """
                SELECT source, capability, cursor, updated_at FROM source_cursors
                WHERE source = ? ORDER BY capability LIMIT ?
                """,
                (source, selected_limit),
            )
            .fetchall()
        )

    def remove_source_cursor(self, source: str, capability: SourceCapability) -> None:
        """Delete one completed or invalidated source cursor."""
        if not isinstance(capability, SourceCapability):
            raise ValueError("capability must be a SourceCapability")
        with self.transaction():
            self._require_connection().execute(
                "DELETE FROM source_cursors WHERE source = ? AND capability = ?",
                (source, capability.value),
            )

    def put_source_limit(self, observation: SourceLimitObservation) -> None:
        """Upsert the latest provider-neutral source limit observation."""
        if not isinstance(observation, SourceLimitObservation):
            raise ValueError("observation must be a SourceLimitObservation")
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO source_limits (
                    source, state, observed_at, retry_at, retry_is_exact, consecutive_limits,
                    window_calls
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source) DO UPDATE SET
                    state = excluded.state,
                    observed_at = excluded.observed_at,
                    retry_at = excluded.retry_at,
                    retry_is_exact = excluded.retry_is_exact,
                    consecutive_limits = excluded.consecutive_limits,
                    window_calls = excluded.window_calls
                """,
                (
                    observation.source,
                    observation.state.value,
                    _datetime_text(observation.observed_at),
                    None if observation.retry_at is None else _datetime_text(observation.retry_at),
                    int(observation.retry_is_exact),
                    observation.consecutive_limits,
                    observation.window_calls,
                ),
            )

    def get_source_limit(self, source: str) -> SourceLimitObservation | None:
        """Read the latest limit observation for one source."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT source, state, observed_at, retry_at, retry_is_exact, consecutive_limits,
                   window_calls
            FROM source_limits WHERE source = ?
            """,
                (source,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return SourceLimitObservation(
            source=str(row[0]),
            state=SourceLimitState(str(row[1])),
            observed_at=datetime.fromisoformat(str(row[2])),
            retry_at=None if row[3] is None else datetime.fromisoformat(str(row[3])),
            retry_is_exact=bool(row[4]),
            consecutive_limits=int(row[5]),
            window_calls=int(row[6]),
        )

    def put_release_check_cursor(self, cursor: ReleaseCheckCursor) -> None:
        """Record one artist's completed release-check boundary."""
        with self.transaction():
            if not self._canonical_exists("artist", cursor.artist_local_id):
                raise sqlite3.IntegrityError("release check artist does not exist")
            self._require_connection().execute(
                """
                INSERT INTO release_check_cursors (source, artist_local_id, last_successful_at)
                VALUES (?, ?, ?)
                ON CONFLICT (source, artist_local_id) DO UPDATE SET
                    last_successful_at = excluded.last_successful_at
                """,
                (
                    cursor.source,
                    cursor.artist_local_id,
                    _datetime_text(cursor.last_successful_at),
                ),
            )

    def get_release_check_cursor(
        self, source: str, artist_local_id: str
    ) -> ReleaseCheckCursor | None:
        """Read one artist's last fully successful release-check boundary."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT source, artist_local_id, last_successful_at
                FROM release_check_cursors WHERE source = ? AND artist_local_id = ?
                """,
                (source, artist_local_id),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ReleaseCheckCursor(
            str(row[0]),
            str(row[1]),
            datetime.fromisoformat(str(row[2])),
        )

    def put_release_check_continuation(self, continuation: ReleaseCheckContinuation) -> None:
        """Persist one opaque page continuation without advancing the success cursor."""
        with self.transaction():
            if not self._canonical_exists("artist", continuation.artist_local_id):
                raise sqlite3.IntegrityError("release continuation artist does not exist")
            self._require_connection().execute(
                """
                INSERT INTO release_check_continuations (source, artist_local_id, cursor, since)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source, artist_local_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    since = excluded.since
                """,
                (
                    continuation.source,
                    continuation.artist_local_id,
                    continuation.cursor,
                    _datetime_text(continuation.since),
                ),
            )

    def get_release_check_continuation(
        self, source: str, artist_local_id: str
    ) -> ReleaseCheckContinuation | None:
        """Read one artist's resumable opaque release-page continuation."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT source, artist_local_id, cursor, since
                FROM release_check_continuations WHERE source = ? AND artist_local_id = ?
                """,
                (source, artist_local_id),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ReleaseCheckContinuation(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            datetime.fromisoformat(str(row[3])),
        )

    def remove_release_check_continuation(self, source: str, artist_local_id: str) -> None:
        """Remove only a continuation that completed with a successful full pass."""
        with self.transaction():
            self._require_connection().execute(
                """
                DELETE FROM release_check_continuations
                WHERE source = ? AND artist_local_id = ?
                """,
                (source, artist_local_id),
            )

    def put_catalog_sync_cursor(self, cursor: CatalogSyncCursor) -> None:
        """Record one capability's completed catalog-sync boundary."""
        if type(cursor) is not CatalogSyncCursor:
            raise ValueError("cursor must be a CatalogSyncCursor")
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO catalog_sync_cursors (source, capability, last_successful_at)
                VALUES (?, ?, ?)
                ON CONFLICT (source, capability) DO UPDATE SET
                    last_successful_at = excluded.last_successful_at
                """,
                (
                    cursor.source,
                    cursor.capability,
                    _datetime_text(cursor.last_successful_at),
                ),
            )

    def get_catalog_sync_cursor(self, source: str, capability: str) -> CatalogSyncCursor | None:
        """Read one capability's last fully successful sync boundary."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT source, capability, last_successful_at
                FROM catalog_sync_cursors WHERE source = ? AND capability = ?
                """,
                (source, capability),
            )
            .fetchone()
        )
        if row is None:
            return None
        return CatalogSyncCursor(
            str(row[0]),
            str(row[1]),
            datetime.fromisoformat(str(row[2])),
        )

    def put_release_discovery(self, discovery: ReleaseDiscovery) -> None:
        """Persist bounded release first/last-seen and material identity state."""
        with self.transaction():
            if not self._canonical_exists("release", discovery.release_local_id):
                raise sqlite3.IntegrityError("release discovery target does not exist")
            source_row = (
                self._require_connection()
                .execute(
                    """
                SELECT 1
                FROM record_sources AS mapping
                JOIN source_references AS reference ON reference.id = mapping.source_reference_id
                WHERE mapping.record_kind = 'release' AND mapping.record_local_id = ?
                  AND reference.source = ? AND reference.native_id = ?
                """,
                    (
                        discovery.release_local_id,
                        discovery.source,
                        discovery.provider_native_id,
                    ),
                )
                .fetchone()
            )
            if source_row is None:
                raise sqlite3.IntegrityError("release discovery source mapping does not exist")
            self._require_connection().execute(
                """
                INSERT INTO release_discoveries
                    (release_local_id, source, provider_native_id, normalized_title, release_date,
                     material_identity, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (release_local_id) DO UPDATE SET
                    source = excluded.source,
                    provider_native_id = excluded.provider_native_id,
                    normalized_title = excluded.normalized_title,
                    release_date = excluded.release_date,
                    material_identity = excluded.material_identity,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    discovery.release_local_id,
                    discovery.source,
                    discovery.provider_native_id,
                    discovery.normalized_title,
                    discovery.release_date.isoformat(),
                    discovery.material_identity,
                    _datetime_text(discovery.first_seen_at),
                    _datetime_text(discovery.last_seen_at),
                ),
            )

    def get_release_discovery(self, release_local_id: str) -> ReleaseDiscovery | None:
        """Read bounded discovery state for one canonical release."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT release_local_id, source, provider_native_id, normalized_title, release_date,
                       material_identity, first_seen_at, last_seen_at
                FROM release_discoveries WHERE release_local_id = ?
                """,
                (release_local_id,),
            )
            .fetchone()
        )
        return None if row is None else self._release_discovery_from_row(row)

    def get_release_discovery_by_provider(
        self, source: str, provider_native_id: str
    ) -> ReleaseDiscovery | None:
        """Read release discovery state by its source-native idempotency identity."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT release_local_id, source, provider_native_id, normalized_title, release_date,
                       material_identity, first_seen_at, last_seen_at
                FROM release_discoveries WHERE source = ? AND provider_native_id = ?
                """,
                (source, provider_native_id),
            )
            .fetchone()
        )
        return None if row is None else self._release_discovery_from_row(row)

    def list_release_discoveries_without_current_signal(
        self, *, limit: int
    ) -> tuple[ReleaseDiscovery, ...]:
        """List oldest releases whose latest discovered version lacks a signal."""
        selected_limit = _bounded_limit(limit)
        rows = (
            self._require_connection()
            .execute(
                """
                SELECT discovery.release_local_id, discovery.source, discovery.provider_native_id,
                       discovery.normalized_title, discovery.release_date, discovery.material_identity,
                       discovery.first_seen_at, discovery.last_seen_at
                FROM release_discoveries AS discovery
                WHERE NOT EXISTS (
                    SELECT 1 FROM signals
                    WHERE signals.kind = 'release'
                      AND signals.record_local_id = discovery.release_local_id
                      AND signals.observed_at = discovery.last_seen_at
                )
                ORDER BY discovery.first_seen_at ASC, discovery.release_local_id ASC
                LIMIT ?
                """,
                (selected_limit,),
            )
            .fetchall()
        )
        return tuple(self._release_discovery_from_row(row) for row in rows)

    def find_release_discovery_variant(
        self,
        source: str,
        artist_local_id: str,
        normalized_title: str,
        release_date: date,
    ) -> ReleaseDiscovery | None:
        """Find an obvious same-artist/title/date release variant without a new candidate."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT discovery.release_local_id, discovery.source, discovery.provider_native_id,
                       discovery.normalized_title, discovery.release_date, discovery.material_identity,
                       discovery.first_seen_at, discovery.last_seen_at
                FROM release_discoveries AS discovery
                JOIN release_artists AS artist ON artist.release_id = discovery.release_local_id
                WHERE discovery.source = ? AND artist.artist_id = ?
                  AND discovery.normalized_title = ? AND discovery.release_date = ?
                ORDER BY discovery.release_local_id
                LIMIT 1
                """,
                (source, artist_local_id, normalized_title, release_date.isoformat()),
            )
            .fetchone()
        )
        return None if row is None else self._release_discovery_from_row(row)

    @staticmethod
    def _release_discovery_from_row(row: tuple[object, ...]) -> ReleaseDiscovery:
        return ReleaseDiscovery(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            date.fromisoformat(str(row[4])),
            str(row[5]),
            datetime.fromisoformat(str(row[6])),
            datetime.fromisoformat(str(row[7])),
        )

    def get_event_check_expiry(self, source: str, artist_local_id: str) -> datetime | None:
        """Read one artist's local event-discovery cache boundary."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT expires_at FROM event_check_caches
                WHERE source = ? AND artist_local_id = ?
                """,
                (source, artist_local_id),
            )
            .fetchone()
        )
        return None if row is None else datetime.fromisoformat(str(row[0]))

    def put_event_check_expiry(
        self, source: str, artist_local_id: str, expires_at: datetime
    ) -> None:
        """Record a bounded local cache entry after a successful artist event check."""
        if not self._canonical_exists("artist", artist_local_id):
            raise sqlite3.IntegrityError("event check artist does not exist")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO event_check_caches (source, artist_local_id, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (source, artist_local_id) DO UPDATE SET
                    expires_at = excluded.expires_at
                """,
                (source, artist_local_id, _datetime_text(expires_at)),
            )

    def put_event_discovery(self, discovery: EventDiscovery) -> None:
        """Persist minimal event-discovery identity and cache metadata without raw payloads."""
        with self.transaction():
            if not self._canonical_exists("event", discovery.event_local_id):
                raise sqlite3.IntegrityError("event discovery target does not exist")
            if not self._canonical_exists("artist", discovery.artist_local_id):
                raise sqlite3.IntegrityError("event discovery artist does not exist")
            source_row = (
                self._require_connection()
                .execute(
                    """
                    SELECT 1
                    FROM record_sources AS mapping
                    JOIN source_references AS reference ON reference.id = mapping.source_reference_id
                    WHERE mapping.record_kind = 'event' AND mapping.record_local_id = ?
                      AND reference.source = ? AND reference.native_id = ?
                    """,
                    (
                        discovery.event_local_id,
                        discovery.source,
                        discovery.provider_native_id,
                    ),
                )
                .fetchone()
            )
            if source_row is None:
                raise sqlite3.IntegrityError("event discovery source mapping does not exist")
            self._require_connection().execute(
                """
                INSERT INTO event_discoveries
                    (event_local_id, source, provider_native_id, artist_local_id, variant_identity,
                     material_identity, attribution, first_seen_at, last_seen_at, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_local_id) DO UPDATE SET
                    source = excluded.source,
                    provider_native_id = excluded.provider_native_id,
                    artist_local_id = excluded.artist_local_id,
                    variant_identity = excluded.variant_identity,
                    material_identity = excluded.material_identity,
                    attribution = excluded.attribution,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    discovery.event_local_id,
                    discovery.source,
                    discovery.provider_native_id,
                    discovery.artist_local_id,
                    discovery.variant_identity,
                    discovery.material_identity,
                    discovery.attribution,
                    _datetime_text(discovery.first_seen_at),
                    _datetime_text(discovery.last_seen_at),
                    _datetime_text(discovery.fetched_at),
                    _datetime_text(discovery.expires_at),
                ),
            )

    def get_event_discovery_by_provider(
        self, source: str, provider_native_id: str
    ) -> EventDiscovery | None:
        """Read event-discovery state by immutable provider identity."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT event_local_id, source, provider_native_id, artist_local_id, variant_identity,
                       material_identity, attribution, first_seen_at, last_seen_at, fetched_at, expires_at
                FROM event_discoveries WHERE source = ? AND provider_native_id = ?
                """,
                (source, provider_native_id),
            )
            .fetchone()
        )
        return None if row is None else self._event_discovery_from_row(row)

    def list_event_discoveries_without_current_signal(
        self, *, limit: int
    ) -> tuple[EventDiscovery, ...]:
        """List oldest events whose latest discovered version lacks a signal."""
        selected_limit = _bounded_limit(limit)
        rows = (
            self._require_connection()
            .execute(
                """
                SELECT discovery.event_local_id, discovery.source, discovery.provider_native_id,
                       discovery.artist_local_id, discovery.variant_identity, discovery.material_identity,
                       discovery.attribution, discovery.first_seen_at, discovery.last_seen_at,
                       discovery.fetched_at, discovery.expires_at
                FROM event_discoveries AS discovery
                WHERE NOT EXISTS (
                    SELECT 1 FROM signals
                    WHERE signals.kind = 'event'
                      AND signals.record_local_id = discovery.event_local_id
                      AND signals.observed_at = discovery.last_seen_at
                )
                ORDER BY discovery.first_seen_at ASC, discovery.event_local_id ASC
                LIMIT ?
                """,
                (selected_limit,),
            )
            .fetchall()
        )
        return tuple(self._event_discovery_from_row(row) for row in rows)

    def find_event_discovery_variant(
        self, source: str, artist_local_id: str, variant_identity: str
    ) -> EventDiscovery | None:
        """Find one obvious same artist/name/start/venue event variant."""
        row = (
            self._require_connection()
            .execute(
                """
                SELECT event_local_id, source, provider_native_id, artist_local_id, variant_identity,
                       material_identity, attribution, first_seen_at, last_seen_at, fetched_at, expires_at
                FROM event_discoveries
                WHERE source = ? AND artist_local_id = ? AND variant_identity = ?
                ORDER BY event_local_id
                LIMIT 1
                """,
                (source, artist_local_id, variant_identity),
            )
            .fetchone()
        )
        return None if row is None else self._event_discovery_from_row(row)

    @staticmethod
    def _event_discovery_from_row(row: tuple[object, ...]) -> EventDiscovery:
        return EventDiscovery(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            None if row[6] is None else str(row[6]),
            datetime.fromisoformat(str(row[7])),
            datetime.fromisoformat(str(row[8])),
            datetime.fromisoformat(str(row[9])),
            datetime.fromisoformat(str(row[10])),
        )

    def put_signal(self, signal: Signal) -> None:
        """Insert one immutable provider-material identity or accept its exact repeat."""
        with self.transaction():
            if not self._canonical_exists(signal.kind.value, signal.record_local_id):
                raise sqlite3.IntegrityError("signal target does not exist")
            encoded_explanation = _explanation_json(signal.explanation)
            if len(encoded_explanation) > 8192:
                raise ValueError("signal explanation is too large")
            connection = self._require_connection()
            connection.execute(
                """
                INSERT INTO signals
                    (local_id, kind, record_local_id, provider, provider_native_id,
                     fingerprint, material_version, explanation_json, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, kind, provider_native_id, material_version) DO NOTHING
                """,
                (
                    signal.local_id,
                    signal.kind.value,
                    signal.record_local_id,
                    signal.provider,
                    signal.provider_native_id,
                    signal.fingerprint,
                    signal.material_version,
                    encoded_explanation,
                    _datetime_text(signal.observed_at),
                ),
            )
            existing = connection.execute(
                """
                SELECT record_local_id, fingerprint, explanation_json
                FROM signals
                WHERE provider = ? AND kind = ? AND provider_native_id = ?
                  AND material_version = ?
                """,
                (
                    signal.provider,
                    signal.kind.value,
                    signal.provider_native_id,
                    signal.material_version,
                ),
            ).fetchone()
            expected_material = (
                signal.record_local_id,
                signal.fingerprint,
                encoded_explanation,
            )
            if existing is None or tuple(str(value) for value in existing) != expected_material:
                raise sqlite3.IntegrityError(
                    "signal material identity conflicts with stored signal"
                )

    def get_signal(self, local_id: str) -> Signal | None:
        """Read one normalized signal by local identity."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, kind, record_local_id, provider, provider_native_id,
                   fingerprint, material_version, explanation_json, observed_at
            FROM signals WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Signal(
            local_id=str(row[0]),
            kind=SignalKind(str(row[1])),
            record_local_id=str(row[2]),
            provider=str(row[3]),
            provider_native_id=str(row[4]),
            fingerprint=str(row[5]),
            material_version=str(row[6]),
            explanation=_decode_explanation(str(row[7])),
            observed_at=datetime.fromisoformat(str(row[8])),
        )

    def find_signal(
        self,
        provider: str,
        kind: SignalKind,
        provider_native_id: str,
        material_version: str,
    ) -> Signal | None:
        """Find the idempotency identity for one provider material version."""
        if not isinstance(kind, SignalKind):
            raise ValueError("kind must be a SignalKind")
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id FROM signals
            WHERE provider = ? AND kind = ? AND provider_native_id = ?
              AND material_version = ?
            """,
                (provider, kind.value, provider_native_id, material_version),
            )
            .fetchone()
        )
        return None if row is None else self.get_signal(str(row[0]))

    def list_signals(self, kind: SignalKind | None, *, limit: int) -> tuple[Signal, ...]:
        """List signals in stable newest-first order, optionally by closed kind."""
        selected_limit = _bounded_limit(limit)
        if kind is not None and not isinstance(kind, SignalKind):
            raise ValueError("kind must be a SignalKind or None")
        if kind is None:
            rows = (
                self._require_connection()
                .execute(
                    """
                SELECT local_id FROM signals
                ORDER BY observed_at DESC, local_id LIMIT ?
                """,
                    (selected_limit,),
                )
                .fetchall()
            )
        else:
            rows = (
                self._require_connection()
                .execute(
                    """
                SELECT local_id FROM signals WHERE kind = ?
                ORDER BY observed_at DESC, local_id LIMIT ?
                """,
                    (kind.value, selected_limit),
                )
                .fetchall()
            )
        records: list[Signal] = []
        for row in rows:
            signal = self.get_signal(str(row[0]))
            if signal is None:
                raise sqlite3.IntegrityError("signal disappeared during list")
            records.append(signal)
        return tuple(records)

    def list_signals_without_inbox_entries(self, *, limit: int) -> tuple[Signal, ...]:
        """List oldest signals missing an inbox entry so bounded retries always make progress."""
        selected_limit = _bounded_limit(limit)
        rows = (
            self._require_connection()
            .execute(
                """
                SELECT signals.local_id
                FROM signals
                LEFT JOIN inbox_entries ON inbox_entries.signal_local_id = signals.local_id
                WHERE inbox_entries.signal_local_id IS NULL
                ORDER BY signals.observed_at ASC, signals.local_id ASC
                LIMIT ?
                """,
                (selected_limit,),
            )
            .fetchall()
        )
        records: list[Signal] = []
        for row in rows:
            signal = self.get_signal(str(row[0]))
            if signal is None:
                raise sqlite3.IntegrityError("signal disappeared during missing-inbox list")
            records.append(signal)
        return tuple(records)

    def put_inbox_entry(self, entry: InboxEntry) -> None:
        """Upsert one local inbox state record for an existing signal."""
        with self.transaction():
            if self.get_signal(entry.signal_local_id) is None:
                raise sqlite3.IntegrityError("inbox signal does not exist")
            self._require_connection().execute(
                """
                INSERT INTO inbox_entries
                    (local_id, signal_local_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (local_id) DO UPDATE SET
                    signal_local_id = excluded.signal_local_id,
                    state = excluded.state,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    entry.local_id,
                    entry.signal_local_id,
                    entry.state.value,
                    _datetime_text(entry.created_at),
                    _datetime_text(entry.updated_at),
                ),
            )

    def get_inbox_entry(self, local_id: str) -> InboxEntry | None:
        """Read one inbox entry by local identity."""
        row = (
            self._require_connection()
            .execute(
                """
            SELECT local_id, signal_local_id, state, created_at, updated_at
            FROM inbox_entries WHERE local_id = ?
            """,
                (local_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return InboxEntry(
            local_id=str(row[0]),
            signal_local_id=str(row[1]),
            state=InboxState(str(row[2])),
            created_at=datetime.fromisoformat(str(row[3])),
            updated_at=datetime.fromisoformat(str(row[4])),
        )

    def list_inbox_entries(self, state: InboxState | None, *, limit: int) -> tuple[InboxEntry, ...]:
        """List inbox entries in stable newest-first order, optionally by state."""
        selected_limit = _bounded_limit(limit)
        if state is not None and not isinstance(state, InboxState):
            raise ValueError("state must be an InboxState or None")
        if state is None:
            rows = (
                self._require_connection()
                .execute(
                    """
                SELECT local_id FROM inbox_entries
                ORDER BY updated_at DESC, local_id LIMIT ?
                """,
                    (selected_limit,),
                )
                .fetchall()
            )
        else:
            rows = (
                self._require_connection()
                .execute(
                    """
                SELECT local_id FROM inbox_entries WHERE state = ?
                ORDER BY updated_at DESC, local_id LIMIT ?
                """,
                    (state.value, selected_limit),
                )
                .fetchall()
            )
        records: list[InboxEntry] = []
        for row in rows:
            entry = self.get_inbox_entry(str(row[0]))
            if entry is None:
                raise sqlite3.IntegrityError("inbox entry disappeared during list")
            records.append(entry)
        return tuple(records)

    def set_check_time(self, source_name: str, checked_at: datetime) -> None:
        """Record the last successful source check normalized to UTC."""
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        with self.transaction():
            self._require_connection().execute(
                """
                INSERT INTO check_times (source, checked_at) VALUES (?, ?)
                ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at
                """,
                (source_name, _datetime_text(checked_at)),
            )

    def get_check_time(self, source_name: str) -> datetime | None:
        """Return the last successful check time for a source."""
        row = (
            self._require_connection()
            .execute("SELECT checked_at FROM check_times WHERE source = ?", (source_name,))
            .fetchone()
        )
        return None if row is None else datetime.fromisoformat(str(row[0]))

    def disconnect_source(self, source_name: str) -> None:
        """Remove one source's provider-owned state, preserving user-owned records."""
        with self.transaction():
            connection = self._require_connection()
            connection.execute("DELETE FROM affinity_evidence WHERE source = ?", (source_name,))
            connection.execute("DELETE FROM source_cursors WHERE source = ?", (source_name,))
            connection.execute("DELETE FROM source_limits WHERE source = ?", (source_name,))
            connection.execute("DELETE FROM refresh_runs WHERE source = ?", (source_name,))
            connection.execute(
                """
                DELETE FROM signals
                WHERE provider = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM inbox_entries
                      WHERE signal_local_id = signals.local_id
                        AND state IN ('saved', 'dismissed')
                  )
                """,
                (source_name,),
            )
            connection.execute("DELETE FROM observations WHERE source = ?", (source_name,))
            connection.execute(
                """
                DELETE FROM record_sources
                WHERE source_reference_id IN (
                    SELECT id FROM source_references WHERE source = ?
                )
                """,
                (source_name,),
            )
            connection.execute("DELETE FROM check_times WHERE source = ?", (source_name,))
            connection.execute(
                """
                DELETE FROM source_references
                WHERE source = ? AND NOT EXISTS (
                    SELECT 1 FROM record_sources
                    WHERE record_sources.source_reference_id = source_references.id
                )
                """,
                (source_name,),
            )


__all__ = ["Catalog"]
