"""Transactional schema migration support."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib import resources
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Migration:
    """One ordered, immutable database migration."""

    version: int
    sql: str


def bundled_migrations() -> tuple[Migration, ...]:
    """Load the migration scripts shipped as package data."""
    schema = resources.files("music_friend.store").joinpath("schema")
    return (
        Migration(1, schema.joinpath("001_initial.sql").read_text(encoding="utf-8")),
        Migration(
            2,
            schema.joinpath("002_relational_integrity.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            3,
            schema.joinpath("003_application_state.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            4,
            schema.joinpath("004_release_discovery.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            5,
            schema.joinpath("005_release_check_continuations.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            6,
            schema.joinpath("006_event_discovery.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            7,
            schema.joinpath("007_source_limits.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            8,
            schema.joinpath("008_listening_history.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            9,
            schema.joinpath("009_source_limit_pacing.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            10,
            schema.joinpath("010_catalog_sync_cursors.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            11,
            schema.joinpath("011_artist_identity_mappings.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            12,
            schema.joinpath("012_source_reference_confidence.sql").read_text(encoding="utf-8"),
        ),
        Migration(
            13,
            schema.joinpath("013_cross_source_release_variant_lookup.sql").read_text(
                encoding="utf-8"
            ),
        ),
    )


def _statements(sql: str) -> Iterable[str]:
    pending = ""
    for character in sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                yield statement
    if pending.strip():
        # SQLite accepts comment-only statements and rejects genuinely incomplete SQL.
        yield pending.strip()


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", "schema_migrations"),
    ).fetchone()
    if exists is None:
        return set()
    return {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}


def apply_migrations(
    connection: sqlite3.Connection, migrations: tuple[Migration, ...] | None = None
) -> None:
    """Apply each unapplied migration atomically and record it after success."""
    selected = bundled_migrations() if migrations is None else migrations
    for migration in sorted(selected, key=lambda item: item.version):
        connection.execute("BEGIN IMMEDIATE")
        try:
            if migration.version not in _applied_versions(connection):
                for statement in _statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (migration.version,)
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


__all__ = ["Migration", "apply_migrations", "bundled_migrations"]
