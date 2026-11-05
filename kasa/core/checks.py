"""Health checks behind ``python -m kasa check``.

Phase 1 checks the skeleton: connection, migrations, extensions, companies,
the guards that enforce the four shelves, and provenance columns. Truth checks
(balances, match coverage, allocations, KSeF completeness, MPP) join later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from kasa.core import migrate
from kasa.core.db import connect
from kasa.core.extension import Extension, ExtensionError, discover_extensions

L0_TABLES = ("l0_raw",)
L1_TABLES = (
    "l1_document",
    "l1_document_line",
    "l1_document_observation",
    "l1_bank_account",
    "l1_bank_transaction",
    "l1_fx_rate",
    "l1_title_semantics",
    "l1_internal_record",
)
L2_TABLES = ("l2_suggestion",)
L3_TABLES = (
    "l3_rule",
    "l3_decision_log",
    "l3_cost_object",
    "l3_match",
    "l3_allocation",
    "l3_category_decision",
    "l3_transaction_status",
)
SUBJECT_TABLES = ("l2_suggestion", "l3_decision_log", "l3_allocation", "l3_category_decision")

PROVENANCE_COLUMNS: dict[str, str] = {
    "company_id": "uuid",
    "source": "text",
    "method": "provenance_method",
    "confidence": "numeric",
    "evidence": "jsonb",
    "schema_version": "integer",
    "prompt_version": "text",
    "created_at": "timestamp with time zone",
    "created_by": "text",
}


def expected_triggers() -> set[tuple[str, str]]:
    """(table, trigger) pairs that must exist and be enabled."""
    expected = {("l0_raw", "l0_raw_append_only"), ("l0_raw", "l0_raw_no_truncate")}
    for table in (*L1_TABLES, *L2_TABLES):
        expected |= {(table, f"{table}_no_delete"), (table, f"{table}_no_truncate")}
    for table in L3_TABLES:
        expected |= {(table, f"{table}_guard"), (table, f"{table}_no_truncate")}
    for table in SUBJECT_TABLES:
        expected.add((table, f"{table}_subject_guard"))
    expected |= {
        ("l3_decision_log", "l3_decision_log_append_only"),
        ("l3_allocation", "l3_allocation_target_guard"),
        ("company", "company_unassigned"),
    }
    return expected


def missing_triggers(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.relname, t.tgname
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE NOT t.tgisinternal AND t.tgenabled <> 'D'
        """
    ).fetchall()
    present = {(r[0], r[1]) for r in rows}
    return sorted(f"{table}.{trigger}" for table, trigger in expected_triggers() - present)


def provenance_gaps(conn: psycopg.Connection) -> list[str]:
    """Provenance columns missing (or of a wrong type) on L1-L3 tables."""
    rows = conn.execute(
        """
        SELECT table_name, column_name,
               CASE WHEN data_type = 'USER-DEFINED' THEN udt_name ELSE data_type END
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name ~ '^l[123]_'
        """
    ).fetchall()
    columns: dict[str, dict[str, str]] = {}
    for table, column, data_type in rows:
        columns.setdefault(table, {})[column] = data_type
    gaps = []
    for table in sorted(columns):
        for column, data_type in PROVENANCE_COLUMNS.items():
            actual = columns[table].get(column)
            if actual != data_type:
                gaps.append(f"{table}.{column} ({actual or 'brak'}, oczekiwano {data_type})")
    return gaps


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(url: str | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []

    def record(name: str, fn: Callable[[], str]) -> bool:
        try:
            detail = fn()
        except (psycopg.Error, migrate.MigrationError, ExtensionError, AssertionError, LookupError) as exc:
            results.append(CheckResult(name, False, str(exc).strip()))
            return False
        results.append(CheckResult(name, True, detail))
        return True

    extensions: dict[str, Extension] = {}

    def check_extensions() -> str:
        extensions.update(discover_extensions())
        return ", ".join(f"{e.slug} ({e.cost_object_kind})" for e in extensions.values()) or "brak rozszerzeń"

    record("Rozszerzenia", check_extensions)

    try:
        conn = connect(url, autocommit=True)
    except psycopg.Error as exc:
        results.append(CheckResult("Połączenie z bazą", False, str(exc).strip()))
        return results

    with conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        results.append(CheckResult("Połączenie z bazą", True, f"PostgreSQL {version}"))

        def check_migrations() -> str:
            done = migrate.applied(conn)
            pending = migrate.verify(migrate.discover(), done)
            assert not pending, "oczekujące migracje: " + ", ".join(m.label for m in pending)
            return f"zastosowane: {len(done)}, oczekujące: 0"

        if not record("Migracje", check_migrations):
            return results

        def check_guards() -> str:
            missing = missing_triggers(conn)
            assert not missing, "brak lub wyłączone: " + ", ".join(missing)
            return f"{len(expected_triggers())} triggerów aktywnych"

        record("Strażnicy półek", check_guards)

        def check_provenance() -> str:
            gaps = provenance_gaps(conn)
            assert not gaps, "braki: " + ", ".join(gaps)
            return "wszystkie tabele L1-L3 mają kolumny pochodzenia"

        record("Pochodzenie", check_provenance)

        def check_companies() -> str:
            rows = conn.execute(
                """
                SELECT c.slug, c.extension_slug,
                       (SELECT count(*) FROM l3_cost_object co
                         WHERE co.company_id = c.id AND co.system_code = 'UNASSIGNED') AS unassigned
                  FROM company c ORDER BY c.slug
                """
            ).fetchall()
            problems = []
            for slug, extension_slug, unassigned in rows:
                if extensions and extension_slug not in extensions:
                    problems.append(f"{slug}: brak rozszerzenia {extension_slug!r}")
                if unassigned != 1:
                    problems.append(f"{slug}: UNASSIGNED x{unassigned}")
            assert not problems, "; ".join(problems)
            if not rows:
                return "brak firm (uruchom: python -m kasa init-db)"
            return ", ".join(row[0] for row in rows) + " - każda ma UNASSIGNED"

        record("Firmy", check_companies)

    return results
