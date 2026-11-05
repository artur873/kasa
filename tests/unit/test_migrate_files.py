from __future__ import annotations

import pytest

from kasa.core import migrate


def test_repository_migrations_are_well_formed():
    migrations = migrate.discover()
    versions = [m.version for m in migrations]
    assert versions == sorted(versions) == list(range(1, len(versions) + 1))
    assert all(len(m.checksum) == 64 for m in migrations)


def test_discover_sorts_by_version_and_ignores_other_files(tmp_path):
    (tmp_path / "0010_later.sql").write_text("SELECT 1;")
    (tmp_path / "0002_early.sql").write_text("SELECT 1;")
    (tmp_path / "README.txt").write_text("notes")
    assert [m.label for m in migrate.discover(tmp_path)] == ["0002_early", "0010_later"]


@pytest.mark.parametrize("names", [["1_short.sql"], ["0001_Upper.sql"], ["0001_a.sql", "0001_b.sql"]])
def test_discover_rejects_bad_files(tmp_path, names):
    for name in names:
        (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(migrate.MigrationError):
        migrate.discover(tmp_path)


def test_verify_detects_missing_and_edited_migrations(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    [migration] = migrate.discover(tmp_path)
    record = migrate.AppliedMigration(1, "a", migration.checksum, None)
    assert migrate.verify([migration], {1: record}) == []
    edited = migrate.AppliedMigration(1, "a", "0" * 64, None)
    with pytest.raises(migrate.MigrationError, match="zmieniona"):
        migrate.verify([migration], {1: edited})
    with pytest.raises(migrate.MigrationError, match="nie ma jej"):
        migrate.verify([], {1: record})
