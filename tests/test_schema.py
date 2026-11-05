"""Database invariants beyond gate 1: provenance, subjects, cost objects, dedup."""

from __future__ import annotations

import contextlib
import uuid

import psycopg
import pytest

from kasa.core.checks import missing_triggers, provenance_gaps, run_checks
from kasa.core.cost_objects import approve
from kasa.core.db import (
    ALLOCATION_TARGET_INVALID,
    COMPANY_CHANGE_FORBIDDEN,
    DELETE_FORBIDDEN,
    RULE_NOT_AUTO,
    SUBJECT_MISSING,
)

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
EXCLUSION_VIOLATION = "23P01"
NOT_NULL_VIOLATION = "23502"


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, *sqlstates: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate in sqlstates, f"{excinfo.value.sqlstate}: {excinfo.value}"


def test_every_l1_l3_table_has_provenance_and_guards(conn):
    assert provenance_gaps(conn) == []
    assert missing_triggers(conn) == []


def test_check_command_passes_on_migrated_database(database_url):
    results = run_checks(database_url)
    assert all(r.ok for r in results), [(r.name, r.detail) for r in results if not r.ok]


def test_each_company_gets_exactly_one_unassigned(conn, make):
    company = make.company()
    rows = conn.execute(
        "SELECT kind, name, status, method FROM l3_cost_object WHERE company_id = %s", (company,)
    ).fetchall()
    assert rows == [("unassigned", "UNASSIGNED", "approved", "code")]
    with db_error(conn, UNIQUE_VIOLATION):
        conn.execute(
            """
            INSERT INTO l3_cost_object (company_id, kind, name, status, system_code, source, method, created_by)
            VALUES (%s, 'unassigned', 'UNASSIGNED', 'approved', 'UNASSIGNED', 'test', 'code', 'pytest')
            """,
            (company,),
        )
    # The reserved kind belongs to the system row only.
    with db_error(conn, CHECK_VIOLATION):
        conn.execute(
            """
            INSERT INTO l3_cost_object (company_id, kind, name, status, source, method, created_by)
            VALUES (%s, 'unassigned', 'Inne', 'approved', 'test', 'human', 'artur')
            """,
            (company,),
        )


def test_cost_objects_are_never_approved_by_automation(conn, make):
    company = make.company()

    def insert(status: str, method: str, prompt_version: str | None = None) -> uuid.UUID:
        return conn.execute(
            """
            INSERT INTO l3_cost_object (company_id, kind, name, status, source, method, prompt_version, created_by)
            VALUES (%s, 'project', 'Gala', %s, 'test', %s, %s, 'pytest') RETURNING id
            """,
            (company, status, method, prompt_version),
        ).fetchone()[0]

    candidate = insert("candidate", "code")
    insert("candidate", "model", prompt_version="pairs-v1")
    with db_error(conn, CHECK_VIOLATION):
        insert("candidate", "model")  # model output must carry its prompt version
    with db_error(conn, CHECK_VIOLATION):
        insert("approved", "code")
    with db_error(conn, RULE_NOT_AUTO):
        insert("approved", "rule")
    with db_error(conn, CHECK_VIOLATION):
        conn.execute("UPDATE l3_cost_object SET status = 'approved' WHERE id = %s", (candidate,))

    # An allocation may not point at a candidate.
    transaction = make.transaction(company)
    decision = make.decision(company, "transaction", transaction)
    with db_error(conn, ALLOCATION_TARGET_INVALID):
        make.allocation(company, transaction, candidate, decision)

    approve(conn, candidate, decided_by="artur")
    assert conn.execute("SELECT status, method FROM l3_cost_object WHERE id = %s", (candidate,)).fetchone() == (
        "approved",
        "human",
    )
    make.allocation(company, transaction, candidate, decision)
    with pytest.raises(ValueError):
        approve(conn, candidate, decided_by="artur")


def test_polymorphic_subject_must_exist_in_the_same_company(conn, make):
    company, other = make.company(), make.company()
    foreign_transaction = make.transaction(other)
    target = make.unassigned(company)

    with db_error(conn, SUBJECT_MISSING):
        make.decision(company, "transaction", uuid.uuid4())
    with db_error(conn, SUBJECT_MISSING):
        make.decision(company, "transaction", foreign_transaction)
    with db_error(conn, SUBJECT_MISSING):
        conn.execute(
            """
            INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, source, method, created_by)
            VALUES (%s, 'match', 'document', %s, '{}', 'test', 'code', 'pytest')
            """,
            (company, uuid.uuid4()),
        )
    transaction = make.transaction(company)
    decision = make.decision(company, "transaction", transaction)
    with db_error(conn, SUBJECT_MISSING):
        make.allocation(company, uuid.uuid4(), target, decision)
    allocation = make.allocation(company, transaction, target, decision)
    with db_error(conn, COMPANY_CHANGE_FORBIDDEN):
        conn.execute("UPDATE l3_allocation SET company_id = %s WHERE id = %s", (other, allocation))


def test_suggestions_are_not_decisions(conn, make):
    company = make.company()
    transaction = make.transaction(company)

    def suggest(method: str, prompt_version: str | None = None) -> uuid.UUID:
        return conn.execute(
            """
            INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank,
                                       source, method, prompt_version, confidence, created_by)
            VALUES (%s, 'category', 'transaction', %s, '{"category": "ZUS"}', 14, 1,
                    'test', %s, %s, 0.97, 'pytest') RETURNING id
            """,
            (company, transaction, method, prompt_version),
        ).fetchone()[0]

    suggestion = suggest("rule")
    suggest("model", prompt_version="title-v3")
    with db_error(conn, CHECK_VIOLATION):
        suggest("human")
    with db_error(conn, CHECK_VIOLATION):
        suggest("model")
    with db_error(conn, DELETE_FORBIDDEN):
        conn.execute("DELETE FROM l2_suggestion WHERE id = %s", (suggestion,))
    conn.execute("UPDATE l2_suggestion SET status = 'expired', resolved_at = now() WHERE id = %s", (suggestion,))


def test_l1_facts_are_never_deleted_and_rules_never_write_facts(conn, make):
    company = make.company()
    document = make.document(company)
    transaction = make.transaction(company)
    for table, row_id in (("l1_document", document), ("l1_bank_transaction", transaction)):
        with db_error(conn, DELETE_FORBIDDEN):
            conn.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))
        with db_error(conn, DELETE_FORBIDDEN):
            conn.execute(f"TRUNCATE {table} CASCADE")
    with db_error(conn, CHECK_VIOLATION):
        conn.execute("UPDATE l1_document SET method = 'rule' WHERE id = %s", (document,))
    with db_error(conn, NOT_NULL_VIOLATION):
        conn.execute(
            """
            INSERT INTO l1_title_semantics (company_id, transaction_id, intent, source, method, created_by)
            VALUES (%s, %s, 'invoice_payment', 'mock-llm', 'model', 'pytest')
            """,
            (company, transaction),
        )


def test_one_document_many_observations(conn, make):
    """The same invoice from KSeF, a PDF in an e-mail and JPK: three observations, one document."""
    company = make.company()
    document = make.document(company, ksef_number="5260001246-20261007-0100A0B1C2D3-4F")
    for channel, linked_by in (("ksef", "created"), ("pdf", "ksef_number"), ("file", "nip_kind_number")):
        raw = make.raw(company, channel=channel)
        conn.execute(
            """
            INSERT INTO l1_document_observation (company_id, document_id, raw_id, linked_by, observed,
                                                 source, method, created_by)
            VALUES (%s, %s, %s, %s, '{"gross_minor": 1230000}', %s, 'code', 'pytest')
            """,
            (company, document, raw, linked_by, channel),
        )
    assert conn.execute(
        "SELECT count(*), count(DISTINCT document_id) FROM l1_document_observation WHERE company_id = %s", (company,)
    ).fetchone() == (3, 1)


def test_ksef_number_identifies_one_current_document(conn, make):
    company = make.company()
    ksef = "5260001246-20261007-0100A0B1C2D3-4F"
    first = make.document(company, ksef_number=ksef)
    with db_error(conn, EXCLUSION_VIOLATION):
        make.document(company, ksef_number=ksef)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")

    # A new parser version: insert the new row and supersede the old one in one transaction.
    with conn.transaction():
        second = make.document(company, ksef_number=ksef)
        conn.execute("UPDATE l1_document SET superseded_by = %s WHERE id = %s", (second, first))
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    current = conn.execute(
        "SELECT id FROM l1_document WHERE ksef_number = %s AND superseded_by IS NULL", (ksef,)
    ).fetchall()
    assert current == [(second,)]


def test_bank_refetch_never_duplicates_current_transactions(conn, make):
    company = make.company()
    account = make.account(company)
    make.transaction(company, account, fingerprint="fp-1", entry_reference="EB-1")
    with db_error(conn, EXCLUSION_VIOLATION):
        make.transaction(company, account, fingerprint="fp-2", entry_reference="EB-1")
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with db_error(conn, EXCLUSION_VIOLATION):
        make.transaction(company, account, fingerprint="fp-1")
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # Another account may have the same fingerprint.
    make.transaction(company, make.account(company, iban="PL27114020040000300201355387"), fingerprint="fp-1")
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_match_points_at_exactly_one_document_or_internal_record(conn, make):
    company = make.company()
    transaction = make.transaction(company)
    decision = make.decision(company, "transaction", transaction, kind="match")
    with db_error(conn, CHECK_VIOLATION):
        conn.execute(
            """
            INSERT INTO l3_match (company_id, transaction_id, amount_minor, decision_id, source, method, created_by)
            VALUES (%s, %s, 45000, %s, 'test', 'human', 'artur')
            """,
            (company, transaction, decision),
        )
