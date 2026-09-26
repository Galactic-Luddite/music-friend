from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from music_friend.store import Catalog


def test_schema_has_no_sensitive_or_raw_content_columns(catalog: Catalog) -> None:
    forbidden = {"token", "secret", "credential", "raw_payload", "email_body", "calendar_body"}
    tables = catalog._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
    ).fetchall()

    columns = {
        str(row[1]).lower()
        for (table,) in tables
        for row in catalog._connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }

    assert columns.isdisjoint(forbidden)


class _RecordingConnection:
    def __init__(self, delegate: sqlite3.Connection) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        self.calls.append((sql, parameters))
        return self.delegate.execute(sql, parameters)

    def commit(self) -> None:
        self.delegate.commit()

    def rollback(self) -> None:
        self.delegate.rollback()

    def close(self) -> None:
        self.delegate.close()


def test_user_values_are_bound_separately_from_sql_templates(catalog_path: Path) -> None:
    source = "source'; DROP TABLE check_times; --"
    checked_at = datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc)

    with Catalog.open(catalog_path) as catalog:
        secured = catalog._connection
        assert secured is not None
        connection = _RecordingConnection(secured)
        catalog._connection = cast(Any, connection)
        catalog.set_check_time(source, checked_at)

    data_calls = [(sql, params) for sql, params in connection.calls if params]
    assert len(data_calls) == 1
    sql, parameters = data_calls[0]
    assert source not in sql
    assert checked_at.isoformat() not in sql
    assert parameters == (source, checked_at.isoformat())


def test_built_artifacts_contain_and_execute_initial_migration(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[2]
    output = tmp_path / "wheel"
    raw_roots = os.environ.get("MF_CLEAN_ROOM_WRITE_ROOTS")
    if raw_roots:
        build_root = Path(json.loads(raw_roots)["build"])
        build_output = build_root / tmp_path.name / "wheel"
        build_output.parent.mkdir(parents=True, exist_ok=True)
    else:
        build_output = output
    subprocess.run(
        [str(Path(sys.executable).with_name("hatchling")), "build", "-d", str(build_output)],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if build_output != output:
        output.mkdir()
        for artifact in build_output.iterdir():
            shutil.copy2(artifact, output / artifact.name)
    wheel = next(output.glob("*.whl"))
    source_distribution = next(output.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "music_friend/store/schema/001_initial.sql" in names
        assert "music_friend/store/schema/002_relational_integrity.sql" in names
        assert "music_friend/store/schema/003_application_state.sql" in names
        assert "music_friend/store/schema/004_release_discovery.sql" in names
        assert "music_friend/store/schema/005_release_check_continuations.sql" in names
        assert "music_friend/store/schema/006_event_discovery.sql" in names
        assert "music_friend/store/schema/007_source_limits.sql" in names
        assert "music_friend/store/schema/008_listening_history.sql" in names
    with tarfile.open(source_distribution, "r:gz") as archive:
        names = archive.getnames()
        assert any(
            name.endswith("/src/music_friend/store/schema/001_initial.sql") for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/002_relational_integrity.sql")
            for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/003_application_state.sql")
            for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/004_release_discovery.sql")
            for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/005_release_check_continuations.sql")
            for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/006_event_discovery.sql")
            for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/007_source_limits.sql") for name in names
        )
        assert any(
            name.endswith("/src/music_friend/store/schema/008_listening_history.sql")
            for name in names
        )

    database = tmp_path / "installed" / "catalog.sqlite3"
    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(wheel)!r}); "
                "from music_friend.store import Catalog; "
                f"catalog = Catalog.open(Path({str(database)!r})); "
                "assert catalog._connection.execute("
                "'SELECT version FROM schema_migrations ORDER BY version').fetchall() "
                "== [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]; "
                "catalog.close()"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr


@pytest.mark.parametrize(
    ("record_kind", "table"),
    (("artist", "artists"), ("release", "releases"), ("event", "events")),
)
def test_raw_record_source_cannot_target_missing_canonical_record(
    catalog: Catalog, record_kind: str, table: str
) -> None:
    connection = catalog._connection
    assert connection is not None
    connection.execute(
        """
        INSERT INTO source_references (source, native_id, canonical_url, observed_at)
        VALUES (?, ?, ?, ?)
        """,
        ("raw-source", f"raw-{record_kind}", None, "2026-09-01T00:00:00+00:00"),
    )
    reference_id = connection.execute(
        "SELECT id FROM source_references WHERE source = ? AND native_id = ?",
        ("raw-source", f"raw-{record_kind}"),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO record_sources
                (record_kind, record_local_id, source_reference_id, position)
            VALUES (?, ?, ?, ?)
            """,
            (record_kind, f"missing-{table}", reference_id, 0),
        )


@pytest.mark.parametrize("kind", ("artist", "release", "event"))
def test_raw_interest_cannot_target_missing_canonical_record(catalog: Catalog, kind: str) -> None:
    connection = catalog._connection
    assert connection is not None

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO interests
                (local_id, kind, target_local_id, status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"raw-interest-{kind}",
                kind,
                "missing-target",
                "active",
                "test",
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
            ),
        )


@pytest.mark.parametrize("record_kind", ("artist", "release", "event"))
def test_raw_observation_requires_matching_record_source_mapping(
    catalog: Catalog, record_kind: str
) -> None:
    connection = catalog._connection
    assert connection is not None
    connection.execute(
        """
        INSERT INTO source_references (source, native_id, canonical_url, observed_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            "raw-observation-source",
            f"raw-{record_kind}",
            None,
            "2026-09-01T00:00:00+00:00",
        ),
    )
    reference_id = connection.execute(
        "SELECT id FROM source_references WHERE source = ? AND native_id = ?",
        ("raw-observation-source", f"raw-{record_kind}"),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO observations
                (local_id, source_reference_id, source, native_id, record_kind,
                 record_local_id, fact_name, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "raw-observation",
                reference_id,
                "raw-observation-source",
                f"raw-{record_kind}",
                record_kind,
                f"missing-{record_kind}",
                "status",
                "2026-09-01T00:00:00+00:00",
            ),
        )
