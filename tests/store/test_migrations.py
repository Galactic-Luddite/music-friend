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
    InboxState,
    Interest,
    InterestKind,
    InterestStatus,
    Observation,
    Release,
    ReleaseDatePrecision,
    SignalKind,
    SourceReference,
)
from music_friend.errors import CatalogUnavailableError
from music_friend.store import Catalog
from music_friend.store.migrations import (
    Migration,
    _statements,
    apply_migrations,
    bundled_migrations,
)

V1_FIXTURE = Path(__file__).parent / "fixtures" / "v1_catalog.sql"
V13_DUPLICATE_INBOX_FIXTURE = Path(__file__).parent / "fixtures" / "v13_duplicate_inbox.sql"
OFFSET = timezone(timedelta(hours=5, minutes=45))
ALL_MIGRATION_VERSIONS = [(migration.version,) for migration in bundled_migrations()]
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


def _create_v13_duplicate_inbox_catalog(path: Path) -> None:
    path.parent.mkdir(mode=0o700)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.executescript(V13_DUPLICATE_INBOX_FIXTURE.read_text(encoding="utf-8"))


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
        "artist_identity_mappings",
    } <= _tables(connection)
    assert (
        connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        == ALL_MIGRATION_VERSIONS
    )
    catalog.close()


def test_migrations_are_idempotent(catalog_path: Path) -> None:
    Catalog.open(catalog_path).close()

    reopened = Catalog.open(catalog_path)

    assert (
        reopened._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        == ALL_MIGRATION_VERSIONS
    )
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
        assert (
            connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            == ALL_MIGRATION_VERSIONS
        )


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
        assert (
            connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            == ALL_MIGRATION_VERSIONS
        )


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
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
            (16,),
        ]


def test_populated_v1_catalog_upgrades_to_v3_without_data_loss(catalog_path: Path) -> None:
    _create_v1_catalog(catalog_path)
    with closing(sqlite3.connect(catalog_path)) as before:
        assert before.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]

    with Catalog.open(catalog_path) as catalog:
        connection = catalog._connection
        assert connection is not None
        assert (
            connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            == ALL_MIGRATION_VERSIONS
        )

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
                subject_local_id="legacy-release",
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
        assert (
            reopened.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            == ALL_MIGRATION_VERSIONS
        )


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


def test_014_collapses_duplicate_inbox_entries_most_decided_wins(catalog_path: Path) -> None:
    """Verifies AC: migration 014 collapses the fixture's four duplicate-inbox shapes.

    (a) two signals/two entries (saved + unread) -> saved wins (decided beats unread).
    (b) dismissed then a later saved entry -> the later saved entry wins.
    (c) one event, two unread entries -> the most-recently-updated one wins.
    (d) one release, a single entry -> unchanged, no collapse.
    """
    _create_v13_duplicate_inbox_catalog(catalog_path)

    with Catalog.open(catalog_path) as catalog:
        connection = catalog._connection
        assert connection is not None
        assert (
            connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            == ALL_MIGRATION_VERSIONS
        )

        rows = {
            str(row[0]): row
            for row in connection.execute(
                """
                SELECT local_id, kind, subject_local_id, latest_signal_local_id, state,
                       created_at, updated_at
                FROM inbox_entries
                ORDER BY local_id
                """
            ).fetchall()
        }
        assert set(rows) == {"entry-a-saved", "entry-b-saved", "entry-c2", "entry-d"}

        # (a) saved beats unread; latest_signal is the group's greatest observed_at signal.
        a = rows["entry-a-saved"]
        assert a[1] == "release" and a[2] == "release-a"
        assert a[3] == "signal-a2"
        assert a[4] == "saved"
        assert a[5] == "2026-01-01T01:00:00+00:00"
        assert a[6] == "2026-01-02T02:00:00+00:00"

        # (b) between two decided entries, the most recently updated (the later saved) wins.
        b = rows["entry-b-saved"]
        assert b[2] == "release-b"
        assert b[3] == "signal-b2"
        assert b[4] == "saved"
        assert b[5] == "2026-01-01T01:00:00+00:00"
        assert b[6] == "2026-01-03T04:00:00+00:00"

        # (c) both unread; the most recently updated one wins and stays unread.
        c = rows["entry-c2"]
        assert c[1] == "event" and c[2] == "event-c"
        assert c[3] == "signal-c2"
        assert c[4] == "unread"
        assert c[5] == "2026-01-01T01:00:00+00:00"
        assert c[6] == "2026-01-04T05:00:00+00:00"

        # (d) a single entry: unchanged.
        d = rows["entry-d"]
        assert d[2] == "release-d"
        assert d[3] == "signal-d1"
        assert d[4] == "saved"
        assert d[5] == "2026-01-01T01:00:00+00:00"
        assert d[6] == "2026-01-01T02:00:00+00:00"

        entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, "release-a")
        assert entry is not None
        assert entry.local_id == "entry-a-saved"


def test_014_snapshots_every_touched_row_and_preserves_signals(catalog_path: Path) -> None:
    """Verifies AC: one winner_before + one loser row per collapsed group; (d) writes none;
    no ``signals`` row is deleted or modified."""
    _create_v13_duplicate_inbox_catalog(catalog_path)
    with closing(sqlite3.connect(catalog_path)) as before:
        before_signals = before.execute(
            "SELECT local_id, kind, record_local_id, provider, provider_native_id, fingerprint, "
            "material_version, explanation_json, observed_at FROM signals ORDER BY local_id"
        ).fetchall()

    with Catalog.open(catalog_path) as catalog:
        connection = catalog._connection
        assert connection is not None

        after_signals = connection.execute(
            "SELECT local_id, kind, record_local_id, provider, provider_native_id, fingerprint, "
            "material_version, explanation_json, observed_at FROM signals ORDER BY local_id"
        ).fetchall()
        assert after_signals == before_signals

        snapshots = connection.execute(
            "SELECT merge_id, role, local_id, winner_local_id, winner_updated_at_after "
            "FROM inbox_entry_snapshots ORDER BY local_id"
        ).fetchall()
        by_local_id = {row[2]: row for row in snapshots}

        # (a): one winner_before (entry-a-saved) + one loser (entry-a-unread).
        assert by_local_id["entry-a-saved"] == (
            "014",
            "winner_before",
            "entry-a-saved",
            "entry-a-saved",
            "2026-01-02T02:00:00+00:00",
        )
        assert by_local_id["entry-a-unread"] == (
            "014",
            "loser",
            "entry-a-unread",
            "entry-a-saved",
            "2026-01-02T02:00:00+00:00",
        )

        # (b): one winner_before (entry-b-saved) + one loser (entry-b-dismissed).
        assert by_local_id["entry-b-saved"][1] == "winner_before"
        assert by_local_id["entry-b-dismissed"][1] == "loser"
        assert by_local_id["entry-b-dismissed"][3] == "entry-b-saved"

        # (c): one winner_before (entry-c2) + one loser (entry-c1).
        assert by_local_id["entry-c2"][1] == "winner_before"
        assert by_local_id["entry-c1"][1] == "loser"
        assert by_local_id["entry-c1"][3] == "entry-c2"

        # (d): a single entry, no collapse, no snapshot rows at all.
        assert "entry-d" not in by_local_id

        assert len(snapshots) == 6


def test_014_failure_rolls_back(catalog_path: Path) -> None:
    """Verifies AC: a forced failure after the collapse leaves schema_migrations at 13 and
    inbox_entries unchanged (pattern of test_failed_migration_rolls_back_schema_and_version)."""
    _create_v13_duplicate_inbox_catalog(catalog_path)
    with closing(sqlite3.connect(catalog_path)) as before:
        before_entries = before.execute(
            "SELECT local_id, signal_local_id, state, created_at, updated_at "
            "FROM inbox_entries ORDER BY local_id"
        ).fetchall()

    migration_014_sql = next(
        migration.sql for migration in bundled_migrations() if migration.version == 14
    )
    broken = Migration(
        version=14,
        sql=migration_014_sql.replace(
            "DROP TABLE inbox_entries;",
            "INSERT INTO missing_table VALUES (1);\nDROP TABLE inbox_entries;",
        ),
    )
    assert broken.sql != migration_014_sql

    with closing(sqlite3.connect(catalog_path, isolation_level=None)) as connection:
        earlier = tuple(migration for migration in bundled_migrations() if migration.version <= 13)
        apply_migrations(connection, earlier)

        with pytest.raises(sqlite3.DatabaseError):
            apply_migrations(connection, (broken,))

        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(v,) for v in range(1, 14)]
        assert (
            connection.execute(
                "SELECT local_id, signal_local_id, state, created_at, updated_at "
                "FROM inbox_entries ORDER BY local_id"
            ).fetchall()
            == before_entries
        )
        assert "inbox_entry_snapshots" not in _tables(connection)


def test_014_upgrade_preserves_all_data_for_unmerge_on_realistic_fixture(
    catalog_path: Path,
) -> None:
    """Additive to the issue's written AC: proves the snapshot data is complete/correct enough
    to reconstruct the exact pre-migration rows (the data an unmerge CLI, issue D, would need),
    on a fixture mirroring realistic same-source repeats, cross-source pairs, repair-created
    duplicates, and mixed unread/saved/dismissed states -- with zero data loss through the
    migration."""
    catalog_path.parent.mkdir(mode=0o700)
    explanation = '{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}'
    with closing(sqlite3.connect(catalog_path)) as connection:
        with connection:
            connection.executescript(V13_DUPLICATE_INBOX_FIXTURE.read_text(encoding="utf-8"))

        # Realistic additions layered on top of the base fixture: a cross-source pair
        # (spotify + musicbrainz) for a brand new release, mirroring a repair-created
        # duplicate discovered from two sources, mixed unread/dismissed states.
        with connection:
            connection.execute(
                "INSERT INTO releases (local_id, title, release_type, release_date, "
                "date_precision, observed_at) VALUES "
                "('release-e', 'Case E Release', 'album', '2026-01-01', 'day', "
                "'2026-01-01T00:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO signals (local_id, kind, record_local_id, provider, "
                "provider_native_id, fingerprint, material_version, explanation_json, "
                "observed_at) VALUES ('signal-e1', 'release', 'release-e', 'spotify', "
                "'e-native-1', 'fp-e1', 'material-v1', ?, '2026-01-01T00:00:00+00:00')",
                (explanation,),
            )
            connection.execute(
                "INSERT INTO signals (local_id, kind, record_local_id, provider, "
                "provider_native_id, fingerprint, material_version, explanation_json, "
                "observed_at) VALUES ('signal-e2', 'release', 'release-e', 'musicbrainz', "
                "'e-native-mb-1', 'fp-e2', 'material-v1', ?, '2026-01-01T00:05:00+00:00')",
                (explanation,),
            )
            connection.execute(
                "INSERT INTO inbox_entries (local_id, signal_local_id, state, created_at, "
                "updated_at) VALUES ('entry-e-spotify', 'signal-e1', 'unread', "
                "'2026-01-01T01:00:00+00:00', '2026-01-01T01:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO inbox_entries (local_id, signal_local_id, state, created_at, "
                "updated_at) VALUES ('entry-e-musicbrainz', 'signal-e2', 'dismissed', "
                "'2026-01-01T01:05:00+00:00', '2026-01-01T06:00:00+00:00')"
            )

    with closing(sqlite3.connect(catalog_path)) as before:
        before_entries = {
            str(row[0]): row
            for row in before.execute(
                "SELECT local_id, signal_local_id, state, created_at, updated_at FROM inbox_entries"
            ).fetchall()
        }
        before_signals = {
            str(row[0]): row
            for row in before.execute(
                "SELECT local_id, kind, record_local_id, provider, provider_native_id, "
                "fingerprint, material_version, explanation_json, observed_at FROM signals"
            ).fetchall()
        }

    with Catalog.open(catalog_path) as catalog:
        connection = catalog._connection
        assert connection is not None

        # Zero data loss: every original signal is still present and byte-identical.
        after_signals = {
            str(row[0]): row
            for row in connection.execute(
                "SELECT local_id, kind, record_local_id, provider, provider_native_id, "
                "fingerprint, material_version, explanation_json, observed_at FROM signals"
            ).fetchall()
        }
        assert after_signals == before_signals

        snapshots = connection.execute(
            "SELECT merge_id, role, local_id, kind, subject_local_id, signal_local_id, "
            "state, created_at, updated_at, winner_local_id, winner_updated_at_after "
            "FROM inbox_entry_snapshots"
        ).fetchall()
        columns = [
            "merge_id",
            "role",
            "local_id",
            "kind",
            "subject_local_id",
            "signal_local_id",
            "state",
            "created_at",
            "updated_at",
            "winner_local_id",
            "winner_updated_at_after",
        ]
        by_local_id = {row[2]: dict(zip(columns, row, strict=True)) for row in snapshots}

        # Reconstruct each collapsed group's ORIGINAL pre-merge rows purely from
        # inbox_entry_snapshots and assert they match the fixture's original rows exactly --
        # the same data an unmerge (issue D) would restore.
        collapsed_local_ids = {
            "entry-a-saved",
            "entry-a-unread",
            "entry-b-dismissed",
            "entry-b-saved",
            "entry-c1",
            "entry-c2",
            "entry-e-spotify",
            "entry-e-musicbrainz",
        }
        assert set(by_local_id) == collapsed_local_ids

        for local_id in collapsed_local_ids:
            snapshot = by_local_id[local_id]
            original = before_entries[local_id]
            assert snapshot["local_id"] == original[0]
            assert snapshot["signal_local_id"] == original[1]
            assert snapshot["state"] == original[2]
            assert snapshot["created_at"] == original[3]
            assert snapshot["updated_at"] == original[4]

        # entry-e-musicbrainz (dismissed, updated later) beat entry-e-spotify (unread).
        winner_e = by_local_id["entry-e-musicbrainz"]
        assert winner_e["role"] == "winner_before"
        assert winner_e["winner_local_id"] == "entry-e-musicbrainz"
        loser_e = by_local_id["entry-e-spotify"]
        assert loser_e["role"] == "loser"
        assert loser_e["winner_local_id"] == "entry-e-musicbrainz"

        collapsed_entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, "release-e")
        assert collapsed_entry is not None
        assert collapsed_entry.local_id == "entry-e-musicbrainz"
        assert collapsed_entry.state is InboxState.DISMISSED
        assert collapsed_entry.latest_signal_local_id == "signal-e2"
