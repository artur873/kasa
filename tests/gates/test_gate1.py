"""Gate 1 - skeleton (docs/FAZY.md).

A new extension (fixture "testco") works without any change to the core, and
the database refuses to let automation overwrite a human decision.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from pydantic import ValidationError

from kasa.core import migrate
from kasa.core.companies import ensure_company
from kasa.core.cost_objects import approve, create_candidate, unassigned_id
from kasa.core.db import (
    APPEND_ONLY,
    DELETE_FORBIDDEN,
    HUMAN_DECISION_PROTECTED,
    RULE_NOT_AUTO,
)
from kasa.core.extension import EXTENSIONS_PATH_ENV, ExtensionError, discover_extensions

REPO = Path(__file__).resolve().parents[2]

TESTCO_YAML = """\
# Fixture extension: a company whose cost object is a project.
slug: testco
name: Test Co
cost_object:
  kind: project
  label: Projekt
  name_template: "{code}"
  attrs:
    code:   {type: text, required: true, label: Kod projektu}
    budget: {type: decimal, label: Budżet}
categories: [Materiały, Inne]
import_mappings:
  timesheet:
    target: internal_record
    description: Karta czasu pracy
    columns:
      person:          {source: pracownik, required: true}
      record_date:     {source: dzień, type: date, required: true}
      cost_object_ref: {source: projekt, required: true}
      hours:           {source: godziny, type: decimal}
sensitive_words: [poufne]
"""


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, *sqlstates: str):
    """Expect the block to fail in the database with one of the SQLSTATEs (inside a savepoint)."""
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate in sqlstates, f"{excinfo.value.sqlstate}: {excinfo.value}"


@pytest.fixture
def testco_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    extension_dir = tmp_path / "extensions" / "testco"
    extension_dir.mkdir(parents=True)
    (extension_dir / "extension.yaml").write_text(TESTCO_YAML, encoding="utf-8")
    monkeypatch.setenv(EXTENSIONS_PATH_ENV, str(extension_dir.parent))
    return extension_dir.parent


# ---------------------------------------------------------------------------
# (a) a new extension works without a change to the core
# ---------------------------------------------------------------------------


def test_a_new_extension_creates_its_own_cost_object_kind(conn, make, testco_path):
    registry = discover_extensions()
    assert {"odm", "rentease", "testco"} <= registry.keys()
    testco = registry["testco"]
    assert testco.cost_object_kind == "project"
    assert registry["odm"].cost_object_kind != "project"
    assert registry["rentease"].cost_object_kind != "project"

    company, created = ensure_company(conn, testco)
    assert created
    assert ensure_company(conn, testco) == (company, False)
    assert unassigned_id(conn, company.id)  # system row made by the database

    candidate = create_candidate(
        conn, testco, company_id=company.id, attrs={"code": "P-17", "budget": "1500.50"},
        source="test", created_by="pytest",
    )
    approve(conn, candidate, decided_by="artur")

    rows = conn.execute(
        """
        SELECT kind, name, attrs, status, method, system_code
          FROM l3_cost_object WHERE company_id = %s ORDER BY kind
        """,
        (company.id,),
    ).fetchall()
    assert rows == [
        ("project", "P-17", {"code": "P-17", "budget": "1500.50"}, "approved", "human", None),
        ("unassigned", "UNASSIGNED", {}, "approved", "code", "UNASSIGNED"),
    ]

    # The same L3 machinery works for the new kind: a human allocates a transaction to it.
    transaction = make.transaction(company.id)
    decision = make.decision(company.id, "transaction", transaction)
    make.allocation(company.id, transaction, candidate, decision)

    # The schema comes from YAML and is enforced.
    for bad in ({"budget": "10"}, {"code": "P-1", "budget": "dużo"}, {"code": "P-1", "colour": "red"}):
        with pytest.raises(ValidationError):
            create_candidate(conn, testco, company_id=company.id, attrs=bad, source="test", created_by="pytest")
    # Another company's extension cannot be used for this company.
    with pytest.raises(ExtensionError):
        create_candidate(conn, registry["rentease"], company_id=company.id,
                         attrs={"room": "P1", "month": "2026-11"}, source="test", created_by="pytest")

    # Import mapping and sensitive words come from YAML as well.
    mapped = testco.import_mapping("timesheet").map_row(
        {"Pracownik": "P-7f3a", "Dzien": "12.03.2026", "Projekt": "P-17", "Godziny": "7,5"}
    )
    assert mapped.core["cost_object_ref"] == "P-17"
    assert mapped.extra == {"hours": Decimal("7.5")}
    assert testco.is_sensitive("Zwrot - POUFNE")
    assert testco.is_sensitive("alimenty za październik")  # baseline list of the core


def test_a_core_does_not_know_company_concepts():
    # The whole core package, subpackages included (phase 4: cost object discovery, allocation, reports).
    core_files = [
        *sorted((REPO / "kasa" / "core").rglob("*.py")),
        REPO / "kasa" / "__init__.py",
        REPO / "kasa" / "__main__.py",
        REPO / "kasa" / "extensions" / "__init__.py",
        *sorted((REPO / "migrations").glob("*.sql")),
    ]
    assert len(core_files) > 10
    offenders = []
    for path in core_files:
        text = path.read_text(encoding="utf-8").lower()
        if "event" in text:
            offenders.append(f"{path.relative_to(REPO)}: 'event'")
        for word in ("odm", "rentease", "room_month", "testco"):
            if re.search(rf"\b{word}\b", text):
                offenders.append(f"{path.relative_to(REPO)}: {word!r}")
    assert not offenders, offenders


def test_a_cli_lists_and_initialises_new_extension(scratch_database_url, testco_path):
    env = {**os.environ, "KASA_DATABASE_URL": scratch_database_url, EXTENSIONS_PATH_ENV: str(testco_path)}

    def kasa(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "kasa", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=120)

    listing = kasa("extensions", "list")
    assert listing.returncode == 0, listing.stderr
    assert re.search(r"^testco\s+Test Co\s+project\s", listing.stdout, re.M), listing.stdout

    init = kasa("init-db")
    assert init.returncode == 0, init.stderr
    assert "Firma testco" in init.stdout

    check = kasa("check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "Wszystkie kontrole zaliczone" in check.stdout

    with psycopg.connect(scratch_database_url) as conn:
        kinds = conn.execute(
            """
            SELECT c.slug, array_agg(co.kind ORDER BY co.kind)
              FROM company c JOIN l3_cost_object co ON co.company_id = c.id
             GROUP BY c.slug ORDER BY c.slug
            """
        ).fetchall()
    assert kinds == [("odm", ["unassigned"]), ("rentease", ["unassigned"]), ("testco", ["unassigned"])]


# ---------------------------------------------------------------------------
# (b) a human decision cannot be overwritten or deleted by automation
# ---------------------------------------------------------------------------


def test_b_human_allocation_is_protected(conn, make):
    company = make.company()
    transaction = make.transaction(company)
    target = make.unassigned(company)
    decision = make.decision(company, "transaction", transaction)
    allocation = make.allocation(company, transaction, target, decision, method="human")
    auto_rule = make.rule(company, stage="auto")

    # Automation may not take over a human row - not even with a rule in stage auto.
    with db_error(conn, HUMAN_DECISION_PROTECTED):
        conn.execute(
            "UPDATE l3_allocation SET method = 'rule', rule_id = %s, rule_version = 1, share = 0.5 WHERE id = %s",
            (auto_rule, allocation),
        )
    for method in ("code", "model"):
        with db_error(conn, HUMAN_DECISION_PROTECTED):
            conn.execute("UPDATE l3_allocation SET method = %s WHERE id = %s", (method, allocation))

    # Nothing is deleted, by anyone.
    with db_error(conn, DELETE_FORBIDDEN):
        conn.execute("DELETE FROM l3_allocation WHERE id = %s", (allocation,))
    with db_error(conn, DELETE_FORBIDDEN):
        conn.execute("TRUNCATE l3_allocation")

    # A human may change a human decision.
    conn.execute(
        "UPDATE l3_allocation SET share = 0.5, method = 'human', updated_by = 'artur' WHERE id = %s", (allocation,)
    )
    share, method, updated_at = conn.execute(
        "SELECT share, method, updated_at FROM l3_allocation WHERE id = %s", (allocation,)
    ).fetchone()
    assert (share, method) == (Decimal("0.5"), "human")
    assert updated_at is not None

    # Revoking is a new human decision; the row stays.
    revoke = make.decision(company, "transaction", transaction, kind="revoke", reverts=decision)
    conn.execute(
        "UPDATE l3_allocation SET status = 'revoked', revoked_by_decision = %s, method = 'human' WHERE id = %s",
        (revoke, allocation),
    )
    assert conn.execute("SELECT status FROM l3_allocation WHERE id = %s", (allocation,)).fetchone() == ("revoked",)


def test_b_every_l3_table_refuses_delete_and_protects_human_rows(conn, make):
    company = make.company()
    transaction = make.transaction(company)
    document = make.document(company)
    target = make.unassigned(company)
    rule = make.rule(company, stage="auto")
    decision = make.decision(company, "transaction", transaction, kind="match")
    cost_object = conn.execute(
        """
        INSERT INTO l3_cost_object (company_id, kind, name, status, source, method, created_by)
        VALUES (%s, 'project', 'X', 'approved', 'test', 'human', 'artur') RETURNING id
        """,
        (company,),
    ).fetchone()[0]
    match = conn.execute(
        """
        INSERT INTO l3_match (company_id, transaction_id, document_id, amount_minor, decision_id,
                              source, method, created_by)
        VALUES (%s, %s, %s, 1230000, %s, 'test', 'human', 'artur') RETURNING id
        """,
        (company, transaction, document, decision),
    ).fetchone()[0]
    allocation = make.allocation(company, transaction, target, decision)
    category = conn.execute(
        """
        INSERT INTO l3_category_decision (company_id, subject_type, subject_id, category, decision_id,
                                          source, method, created_by)
        VALUES (%s, 'transaction', %s, 'ZUS', %s, 'test', 'human', 'artur') RETURNING id
        """,
        (company, transaction, decision),
    ).fetchone()[0]
    status = conn.execute(
        """
        INSERT INTO l3_transaction_status (company_id, transaction_id, final_status, decision_id,
                                           source, method, created_by)
        VALUES (%s, %s, 'matched', %s, 'test', 'human', 'artur') RETURNING id
        """,
        (company, transaction, decision),
    ).fetchone()[0]

    rows = {
        "l3_rule": rule,
        "l3_cost_object": cost_object,
        "l3_match": match,
        "l3_allocation": allocation,
        "l3_category_decision": category,
        "l3_transaction_status": status,
    }
    for table, row_id in rows.items():
        with db_error(conn, DELETE_FORBIDDEN):
            conn.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))
        with db_error(conn, HUMAN_DECISION_PROTECTED):
            conn.execute(f"UPDATE {table} SET method = 'code' WHERE id = %s", (row_id,))
        with db_error(conn, DELETE_FORBIDDEN):
            conn.execute(f"TRUNCATE {table} CASCADE")

    # The decision log is append-only altogether.
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l3_decision_log SET reason = 'x' WHERE id = %s", (decision,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("DELETE FROM l3_decision_log WHERE id = %s", (decision,))


# ---------------------------------------------------------------------------
# (c) raw payloads are insert-only
# ---------------------------------------------------------------------------


def test_c_l0_raw_is_insert_only(conn, make):
    company = make.company()
    payload = b"<Faktura><P_2>FV 7/10/2026</P_2></Faktura>"
    raw = make.raw(company, payload, channel="ksef")

    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET meta = '{\"edited\": true}' WHERE id = %s", (raw,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET payload = 'x' WHERE id = %s", (raw,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("DELETE FROM l0_raw WHERE id = %s", (raw,))
    with db_error(conn, DELETE_FORBIDDEN):
        conn.execute("TRUNCATE l0_raw CASCADE")

    # Fetching the same file again never creates a duplicate.
    with db_error(conn, "23505"):
        make.raw(company, payload, channel="email")
    # The stored hash is recomputed by the database.
    with db_error(conn, "23514"):
        conn.execute(
            """
            INSERT INTO l0_raw (company_id, channel, sha256, content_type, payload)
            VALUES (%s, 'file', %s, 'text/plain', %s)
            """,
            (company, hashlib.sha256(b"other").hexdigest(), b"payload"),
        )
    assert conn.execute("SELECT count(*) FROM l0_raw WHERE company_id = %s", (company,)).fetchone() == (1,)


# ---------------------------------------------------------------------------
# (d) migrations are idempotent
# ---------------------------------------------------------------------------


def test_d_second_migration_run_changes_nothing(database_url):
    applied_before, pending_before = migrate.status(database_url)
    assert pending_before == []
    assert migrate.migrate(database_url) == []
    assert migrate.migrate(database_url) == []
    applied_after, pending_after = migrate.status(database_url)
    assert pending_after == []
    assert applied_after == applied_before
    assert [m.version for m in applied_after] == [m.version for m in migrate.discover()]


def test_d_runner_applies_in_order_once_and_atomically(scratch_database_url, tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0002_seed.sql").write_text("INSERT INTO t VALUES (1);\n")
    (directory / "0001_create.sql").write_text("CREATE TABLE t (x int);\n")

    applied = migrate.migrate(scratch_database_url, directory)
    assert [m.label for m in applied] == ["0001_create", "0002_seed"]
    assert migrate.migrate(scratch_database_url, directory) == []

    # A failing migration leaves nothing behind and is not recorded.
    (directory / "0003_broken.sql").write_text("CREATE TABLE u (y int);\nSELECT 1 / 0;\n")
    with pytest.raises(migrate.MigrationError, match="0003_broken"):
        migrate.migrate(scratch_database_url, directory)
    with psycopg.connect(scratch_database_url) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (1,)
        assert conn.execute("SELECT to_regclass('u')").fetchone() == (None,)
        assert conn.execute("SELECT array_agg(version ORDER BY version) FROM schema_migrations").fetchone() == ([1, 2],)
    (directory / "0003_broken.sql").unlink()

    # An applied migration is never edited.
    (directory / "0001_create.sql").write_text("CREATE TABLE t (x bigint);\n")
    with pytest.raises(migrate.MigrationError, match="zmieniona"):
        migrate.migrate(scratch_database_url, directory)


# ---------------------------------------------------------------------------
# (e) automation writes only on behalf of a rule in stage auto
# ---------------------------------------------------------------------------


def test_e_rule_write_requires_rule_in_auto_stage(conn, make):
    company = make.company()
    transaction = make.transaction(company)
    target = make.unassigned(company)
    human_decision = make.decision(company, "transaction", transaction)

    # No rule at all.
    with db_error(conn, RULE_NOT_AUTO):
        make.allocation(company, transaction, target, human_decision, method="rule")
    with db_error(conn, RULE_NOT_AUTO):
        make.decision(company, "transaction", transaction, method="rule")

    # A rule in any stage other than auto.
    for stage in ("candidate", "hint", "shadow", "suspended", "dormant"):
        rule = make.rule(company, stage=stage)
        with db_error(conn, RULE_NOT_AUTO):
            make.decision(company, "transaction", transaction, method="rule", rule_id=rule, rule_version=1)
        with db_error(conn, RULE_NOT_AUTO):
            make.allocation(company, transaction, target, human_decision, method="rule",
                            rule_id=rule, rule_version=1)

    # A rule in auto, but of another company.
    foreign_rule = make.rule(make.company(), stage="auto")
    with db_error(conn, RULE_NOT_AUTO):
        make.decision(company, "transaction", transaction, method="rule", rule_id=foreign_rule, rule_version=1)

    # A human promotes the rule to auto; from now on the automation may decide.
    rule = make.rule(company, stage="shadow")
    conn.execute("UPDATE l3_rule SET stage = 'auto', method = 'human', updated_by = 'artur' WHERE id = %s", (rule,))
    with db_error(conn, RULE_NOT_AUTO):
        make.decision(company, "transaction", transaction, method="rule", rule_id=rule, rule_version=2)
    auto_decision = make.decision(company, "transaction", transaction, method="rule", rule_id=rule, rule_version=1)
    allocation = make.allocation(company, transaction, target, auto_decision, method="rule",
                                 rule_id=rule, rule_version=1)

    # Suspended (by a human): the automation can no longer touch even its own rows ...
    conn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human' WHERE id = %s", (rule,))
    with db_error(conn, RULE_NOT_AUTO):
        conn.execute("UPDATE l3_allocation SET share = 0.5 WHERE id = %s", (allocation,))
    # ... while a human can revoke them.
    revoke = make.decision(company, "transaction", transaction, kind="revoke", reverts=auto_decision)
    conn.execute(
        "UPDATE l3_allocation SET status = 'revoked', revoked_by_decision = %s, method = 'human' WHERE id = %s",
        (revoke, allocation),
    )
    assert conn.execute("SELECT status, method FROM l3_allocation WHERE id = %s", (allocation,)).fetchone() == (
        "revoked",
        "human",
    )
