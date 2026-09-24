from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from music_friend.domain import (
    Artist,
    Event,
    IdentityConfidence,
    Interest,
    InterestKind,
    InterestStatus,
    Observation,
    Release,
    ReleaseDatePrecision,
    SourceReference,
)
from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.store.migrations import Migration, _statements, apply_migrations

V1_FIXTURE = Path(__file__).parent / "fixtures" / "v1_catalog.sql"
OFFSET = timezone(timedelta(hours=5, minutes=45))
OBSERVED = datetime(2026, 8, 31, 23, 17, 41, 123456, tzinfo=OFFSET)
LATER = datetime(2026, 9, 1, 1, 2, 3, 654321, tzinfo=timezone(timedelta(hours=-7)))


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _create_v1_catalog(path: Path) -> None:
    path.parent.mkdir(mode=0o700)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript(V1_FIXTURE.read_text(encoding="utf-8"))


def _legacy_source(
    source: str, native_id: str, url_suffix: str, observed_at: datetime = OBSERVED
) -> SourceReference:
    return SourceReference(
        source=source,
        native_id=native_id,
        canonical_url=f"https://example.test/legacy/{url_suffix}",
        observed_at=observed_at,
    )


def test_initial_migration_creates_complete_normalized_schema(catalog_path: Path) -> None:
    catalog = Catalog.open(catalog_path)
    connection = catalog._connection

    assert {
        "artists",
        "releases",
        "release_artists",
        "events",
        "event_artists",
        "event_source_links",
        "source_references",
        "record_sources",
        "interests",
        "observations",
        "check_times",
        "schema_migrations",
    } <= _tables(connection)
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]
    catalog.close()


def test_migrations_are_idempotent(catalog_path: Path) -> None:
    Catalog.open(catalog_path).close()

    reopened = Catalog.open(catalog_path)

    assert reopened._connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]
    reopened.close()


def test_failed_migration_rolls_back_schema_and_version() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        broken = Migration(
            version=7,
            sql="CREATE TABLE transient_record (id TEXT); INSERT INTO missing_table VALUES (1);",
        )

        with pytest.raises(sqlite3.DatabaseError):
            apply_migrations(connection, (broken,))

        assert "transient_record" not in _tables(connection)
        assert "schema_migrations" not in _tables(connection)


def test_statement_splitter_separates_statements_sharing_one_line() -> None:
    sql = "CREATE TABLE first_record (id INTEGER); INSERT INTO first_record VALUES (1);"

    assert list(_statements(sql)) == [
        "CREATE TABLE first_record (id INTEGER);",
        "INSERT INTO first_record VALUES (1);",
    ]


def test_statement_splitter_preserves_semicolons_inside_quoted_values() -> None:
    sql = "INSERT INTO notes VALUES ('alpha; beta'); SELECT \"gamma; delta\";"

    assert list(_statements(sql)) == [
        "INSERT INTO notes VALUES ('alpha; beta');",
        'SELECT "gamma; delta";',
    ]


def test_statement_splitter_preserves_semicolons_inside_comments() -> None:
    sql = (
        "-- a semicolon; inside a line comment\n"
        "CREATE TABLE notes (value TEXT); /* block; comment */ "
        "INSERT INTO notes VALUES ('kept');"
    )

    assert list(_statements(sql)) == [
        "-- a semicolon; inside a line comment\nCREATE TABLE notes (value TEXT);",
        "/* block; comment */ INSERT INTO notes VALUES ('kept');",
    ]


def test_statement_splitter_keeps_trigger_body_together() -> None:
    sql = (
        "CREATE TABLE source (value INTEGER); "
        "CREATE TABLE audit (value INTEGER); "
        "CREATE TRIGGER audit_source AFTER INSERT ON source BEGIN "
        "INSERT INTO audit VALUES (NEW.value); "
        "INSERT INTO audit VALUES (NEW.value + 1); END; "
        "INSERT INTO source VALUES (7);"
    )

    statements = list(_statements(sql))

    assert len(statements) == 4
    assert statements[2] == (
        "CREATE TRIGGER audit_source AFTER INSERT ON source BEGIN "
        "INSERT INTO audit VALUES (NEW.value); "
        "INSERT INTO audit VALUES (NEW.value + 1); END;"
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in statements:
            connection.execute(statement)
        assert connection.execute("SELECT value FROM audit ORDER BY value").fetchall() == [
            (7,),
            (8,),
        ]


def test_statement_splitter_returns_incomplete_trailing_sql_for_sqlite_to_reject() -> None:
    sql = "CREATE TABLE complete (id INTEGER); CREATE TABLE incomplete ("

    assert list(_statements(sql)) == [
        "CREATE TABLE complete (id INTEGER);",
        "CREATE TABLE incomplete (",
    ]


def test_failed_same_line_migration_preserves_prior_version_atomically() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        initial = Migration(
            version=1,
            sql="CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);",
        )
        broken = Migration(
            version=2,
            sql="CREATE TABLE transient_record (id TEXT); INSERT INTO missing_table VALUES (1);",
        )
        apply_migrations(connection, (initial,))

        with pytest.raises(sqlite3.DatabaseError):
            apply_migrations(connection, (initial, broken))

        assert "transient_record" not in _tables(connection)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]


def test_open_closes_connection_when_migration_fails(
    catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[sqlite3.Connection] = []

    def fail(connection: sqlite3.Connection) -> None:
        captured.append(connection)
        raise sqlite3.DatabaseError("migration failed at a private path")

    monkeypatch.setattr("music_friend.store.catalog.migrations.apply_migrations", fail)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert len(captured) == 1
    assert not catalog_path.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("SELECT 1")


class _BeginBarrierConnection:
    def __init__(self, delegate: sqlite3.Connection, barrier: threading.Barrier) -> None:
        self.delegate = delegate
        self.barrier = barrier

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        if sql == "BEGIN IMMEDIATE":
            self.barrier.wait(timeout=5)
        return self.delegate.execute(sql, parameters)

    def commit(self) -> None:
        self.delegate.commit()

    def rollback(self) -> None:
        self.delegate.rollback()


def test_concurrent_migration_callers_serialize_version_check(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def migrate() -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            apply_migrations(_BeginBarrierConnection(connection, barrier))  # type: ignore[arg-type]
        except BaseException as error:
            failures.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]


def test_concurrent_catalog_openers_apply_initial_migration_once(catalog_path: Path) -> None:
    barrier = threading.Barrier(4)
    failures: list[BaseException] = []

    def open_catalog() -> None:
        try:
            barrier.wait(timeout=5)
            Catalog.open(catalog_path).close()
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=open_catalog) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    with closing(sqlite3.connect(catalog_path)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]


class _CommitFailureConnection:
    def __init__(self) -> None:
        self.rollback_count = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> _CommitFailureConnection:
        return self

    def fetchone(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())

    def commit(self) -> None:
        raise sqlite3.OperationalError("synthetic commit failure")

    def rollback(self) -> None:
        self.rollback_count += 1


def test_commit_failure_rolls_back_migration_transaction() -> None:
    connection = _CommitFailureConnection()
    migration = Migration(
        version=1,
        sql="CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);",
    )

    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        apply_migrations(connection, (migration,))  # type: ignore[arg-type]

    assert connection.rollback_count == 1


def test_migration_accepts_valid_trailing_sql_comment() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        migration = Migration(
            version=1,
            sql=(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);\n"
                "-- migration intentionally ends with a comment\n"
            ),
        )

        apply_migrations(connection, (migration,))

        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]


def test_existing_database_is_never_deleted_when_migration_fails(
    catalog_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Catalog.open(catalog_path).close()

    def fail(connection: sqlite3.Connection) -> None:
        raise sqlite3.DatabaseError("synthetic existing migration failure")

    monkeypatch.setattr("music_friend.store.catalog.migrations.apply_migrations", fail)

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    assert catalog_path.is_file()
    with closing(sqlite3.connect(catalog_path)) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
        ]


def test_populated_v1_catalog_upgrades_to_v3_without_data_loss(catalog_path: Path) -> None:
    _create_v1_catalog(catalog_path)
    with closing(sqlite3.connect(catalog_path)) as before:
        assert before.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]

    with Catalog.open(catalog_path) as catalog:
        connection = catalog._connection
        assert connection is not None
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]

        artist = catalog.get_artist("legacy-artist-1")
        assert artist is not None
        assert asdict(artist) == asdict(
            Artist(
                local_id="legacy-artist-1",
                display_name="Legacy Artist One",
                source_refs=(
                    _legacy_source("legacy-source-a", "artist-one", "artist-one"),
                    _legacy_source("legacy-source-b", "artist-one", "artist-one-b", LATER),
                ),
                identity_confidence=IdentityConfidence.USER_CONFIRMED,
                observed_at=OBSERVED,
            )
        )
        release = catalog.get_release("legacy-release")
        assert release is not None
        assert asdict(release) == asdict(
            Release(
                local_id="legacy-release",
                title="Legacy Release",
                release_type="album",
                release_date=date(2026, 8, 31),
                date_precision=ReleaseDatePrecision.DAY,
                artist_refs=("legacy-artist-2", "legacy-artist-1"),
                source_refs=(_legacy_source("legacy-source-a", "release", "release"),),
                observed_at=OBSERVED,
            )
        )
        event = catalog.get_event("legacy-event")
        assert event is not None
        assert asdict(event) == asdict(
            Event(
                local_id="legacy-event",
                title="Legacy Event",
                artist_refs=("legacy-artist-1", "legacy-artist-2"),
                venue_name="Legacy Venue",
                locality="Legacy Locality",
                starts_at=LATER,
                time_precision="second",
                source_links=(
                    "https://example.test/legacy/two",
                    "https://example.test/legacy/one",
                ),
                source_refs=(_legacy_source("legacy-source-a", "event", "event"),),
                observed_at=OBSERVED,
            )
        )
        interest = catalog.get_interest("legacy-interest")
        assert interest is not None
        assert asdict(interest) == asdict(
            Interest(
                local_id="legacy-interest",
                kind=InterestKind.ARTIST,
                target_local_id="legacy-artist-1",
                status=InterestStatus.ACTIVE,
                created_by="legacy-user",
                created_at=OBSERVED,
                updated_at=LATER,
            )
        )
        observation = catalog.get_observation("legacy-observation")
        assert observation is not None
        assert asdict(observation) == asdict(
            Observation(
                local_id="legacy-observation",
                source="legacy-source-a",
                native_id="artist-one",
                record_kind="artist",
                record_local_id="legacy-artist-1",
                fact_name="tour_status",
                observed_at=LATER,
            )
        )
        assert catalog.get_check_time("legacy-source-a") == OBSERVED
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO interests
                    (local_id, kind, target_local_id, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "dangling-after-upgrade",
                    "artist",
                    "missing",
                    "active",
                    "test",
                    OBSERVED.isoformat(),
                    OBSERVED.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO record_sources
                    (record_kind, record_local_id, source_reference_id, position)
                VALUES (?, ?, ?, ?)
                """,
                ("release", "missing-release", 13, 0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO observations
                    (local_id, source_reference_id, source, native_id, record_kind,
                     record_local_id, fact_name, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mismatched-after-upgrade",
                    10,
                    "legacy-source-b",
                    "artist-one",
                    "artist",
                    "legacy-artist-1",
                    "status",
                    OBSERVED.isoformat(),
                ),
            )

    Catalog.open(catalog_path).close()
    with closing(sqlite3.connect(catalog_path)) as reopened:
        assert reopened.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,)]


@pytest.mark.parametrize(
    "corruption_sql",
    (
        """
        INSERT INTO observations
            (local_id, source, native_id, record_kind, record_local_id, fact_name, observed_at)
        VALUES ('corrupt-observation', 'wrong-source', 'artist-one', 'artist',
                'legacy-artist-1', 'status', '2026-09-01T00:00:00+00:00')
        """,
        """
        INSERT INTO record_sources
            (record_kind, record_local_id, source_reference_id, position)
        VALUES ('artist', 'missing-artist', 10, 0)
        """,
        """
        INSERT INTO interests
            (local_id, kind, target_local_id, status, created_by, created_at, updated_at)
        VALUES ('corrupt-interest', 'event', 'missing-event', 'active', 'test',
                '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')
        """,
    ),
)
def test_corrupt_v1_upgrade_rolls_back_without_changes(
    catalog_path: Path, corruption_sql: str
) -> None:
    _create_v1_catalog(catalog_path)
    with closing(sqlite3.connect(catalog_path)) as connection:
        with connection:
            connection.execute(corruption_sql)
        before_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        before_observations = connection.execute(
            "SELECT * FROM observations ORDER BY local_id"
        ).fetchall()
        before_record_sources = connection.execute(
            "SELECT * FROM record_sources ORDER BY record_kind, record_local_id, position"
        ).fetchall()
        before_interests = connection.execute(
            "SELECT * FROM interests ORDER BY local_id"
        ).fetchall()

    with pytest.raises(CatalogUnavailableError):
        Catalog.open(catalog_path)

    with closing(sqlite3.connect(catalog_path)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
            ).fetchall()
            == before_schema
        )
        assert (
            connection.execute("SELECT * FROM observations ORDER BY local_id").fetchall()
            == before_observations
        )
        assert (
            connection.execute(
                "SELECT * FROM record_sources ORDER BY record_kind, record_local_id, position"
            ).fetchall()
            == before_record_sources
        )
        assert (
            connection.execute("SELECT * FROM interests ORDER BY local_id").fetchall()
            == before_interests
        )
