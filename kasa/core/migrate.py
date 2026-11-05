"""Idempotent migration runner.

Migrations are plain SQL files ``migrations/NNNN_name.sql`` applied in version
order, each in its own transaction, and recorded in ``schema_migrations`` with
a SHA-256 checksum. An applied migration is never edited: a changed checksum
is an error, and so is a version recorded in the database but missing on disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from kasa.core.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_FILE_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9][a-z0-9_]*)\.sql$")
# Session-level advisory lock so two runners never interleave.
ADVISORY_LOCK_KEY = 0x6B617361  # "kasa"

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migrations on disk and in the database disagree, or a file is invalid."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime


def discover(directory: Path | None = None) -> list[Migration]:
    """Read migration files from ``directory`` sorted by version."""
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"Brak katalogu migracji: {directory}")
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = MIGRATION_FILE_RE.match(path.name)
        if not match:
            raise MigrationError(f"Niepoprawna nazwa pliku migracji: {path.name} (oczekiwano NNNN_nazwa.sql)")
        version = int(match["version"])
        if version in migrations:
            raise MigrationError(f"Zdublowany numer migracji {version:04d}: {path.name}")
        data = path.read_bytes()
        migrations[version] = Migration(
            version=version,
            name=match["name"],
            path=path,
            sql=data.decode("utf-8"),
            checksum=hashlib.sha256(data).hexdigest(),
        )
    return [migrations[v] for v in sorted(migrations)]


def applied(conn: psycopg.Connection) -> dict[int, AppliedMigration]:
    """Migrations recorded in the database (empty when the table does not exist yet)."""
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
    if not exists:
        return {}
    rows = conn.execute("SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version").fetchall()
    return {row[0]: AppliedMigration(*row) for row in rows}


def verify(migrations: list[Migration], done: dict[int, AppliedMigration]) -> list[Migration]:
    """Check recorded migrations against files and return the pending ones."""
    on_disk = {m.version: m for m in migrations}
    for version, record in done.items():
        migration = on_disk.get(version)
        if migration is None:
            raise MigrationError(
                f"Migracja {version:04d}_{record.name} jest zastosowana w bazie, ale nie ma jej w katalogu migracji"
            )
        if migration.checksum != record.checksum:
            raise MigrationError(
                f"Migracja {migration.label} została zmieniona po zastosowaniu (inna suma kontrolna). "
                "Zastosowanych migracji nie edytujemy - dopisz nową."
            )
    return [m for m in migrations if m.version not in done]


def status(url: str | None = None, directory: Path | None = None) -> tuple[list[AppliedMigration], list[Migration]]:
    """Return (applied, pending) without changing anything."""
    migrations = discover(directory)
    with connect(url, autocommit=True) as conn:
        done = applied(conn)
    return list(done.values()), verify(migrations, done)


def migrate(url: str | None = None, directory: Path | None = None) -> list[Migration]:
    """Apply pending migrations; return the ones applied by this call (empty = nothing to do)."""
    migrations = discover(directory)
    applied_now: list[Migration] = []
    with connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(BOOTSTRAP_SQL)
            pending = verify(migrations, applied(conn))
            for migration in pending:
                try:
                    with conn.transaction():
                        conn.execute(migration.sql)
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                            (migration.version, migration.name, migration.checksum),
                        )
                except psycopg.Error as exc:
                    raise MigrationError(f"Migracja {migration.label} nie powiodła się: {exc}") from exc
                applied_now.append(migration)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    return applied_now
