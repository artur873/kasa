"""Evidence of mining (review of 4C/5, point 3): a decision of the automation counts only when the identifiers
of the title decide the group by themselves - the numbers written in the title name exactly the documents of
the decision. Otherwise the automation would confirm its own choice."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from kasa.core.decisions import log_decision
from kasa.core.rules import mine


def _documents(conn, company_id, *, direction: str = "purchase") -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.id, d.number_raw, d.number_normalized, d.counterparty_id FROM l1_document d
         WHERE d.company_id = %s AND d.direction = %s AND d.superseded_by IS NULL AND d.counterparty_id IS NOT NULL
           AND (SELECT count(*) FROM l1_document o WHERE o.company_id = d.company_id AND o.superseded_by IS NULL
                   AND o.direction = d.direction AND o.number_normalized = d.number_normalized) = 1
         ORDER BY d.counterparty_id, d.issue_date, d.id
        """,
        (company_id, direction),
    ).fetchall()
    return [{"id": r[0], "number": r[1], "normalized": r[2], "party": r[3]} for r in rows]


def _transfer(conn, company_id, party: UUID, title: str, numbers: list[dict[str, Any]], amount: int = -10_000) -> UUID:
    account = conn.execute(
        "INSERT INTO l1_bank_account (company_id, iban, currency, kind, source, method, created_by) "
        "VALUES (%s, %s, 'PLN', 'current', 'test', 'code', 'pytest') RETURNING id",
        (company_id, f"PL{uuid.uuid4().int % 10**26:026d}"),
    ).fetchone()[0]
    extracted = {"invoice_numbers": [{"raw": n["number"], "normalized": n["normalized"], "alt": [],
                                      "pattern": "keyword"} for n in numbers], "extractor_version": 2}
    return conn.execute(
        """
        INSERT INTO l1_bank_transaction (company_id, account_id, entry_reference, fingerprint, status, booking_date,
                                         amount_minor, currency, title_raw, op_type, counterparty_id, extracted,
                                         source, method, created_by)
        VALUES (%s, %s, %s, %s, 'booked', DATE '2026-10-20', %s, 'PLN', %s, 'transfer', %s, %s, 'test', 'code',
                'pytest') RETURNING id
        """,
        (company_id, account, f"SYN-{uuid.uuid4().hex[:12]}", uuid.uuid4().hex, amount, title, party,
         Jsonb(extracted)),
    ).fetchone()[0]


def _automatic_match(conn, company_id, tx: UUID, documents: list[UUID]) -> UUID:
    rule_id, version = conn.execute(
        "SELECT id, version FROM l3_rule WHERE company_id = %s AND rule_key = "
        "'builtin:match_invoice_number_same_counterparty' AND stage = 'auto'", (company_id,)).fetchone()
    return log_decision(
        conn, company_id=company_id, kind="match", subject_type="transaction", subject_id=tx,
        decided_by=f"rule:builtin@{version}", source="kasa.matching", method="rule", rule_id=rule_id,
        rule_version=version,
        payload={"final_status": "matched", "items": [{"document_id": str(d), "amount_minor": 5_000} for d in documents]},
        evidence={"tags": ["invoice_number", "counterparty_strong"], "facts": ["numer w tytule"]})


def _cases_of(conn, company_id, tx: UUID) -> list[mine.Case]:
    return [c for c in mine.collect(conn, company_id) if c.subject_id == tx and c.family == "monthly_bulk"]


def test_automatic_match_is_evidence_only_when_the_title_alone_decides_the_group(qconn, odm):
    docs = _documents(qconn, odm.id)
    by_party: dict[UUID, list[dict[str, Any]]] = {}
    for doc in docs:
        by_party.setdefault(doc["party"], []).append(doc)
    party, (first, second, *_rest) = next((p, d) for p, d in by_party.items() if len(d) >= 2)

    # the title names both invoices, the automation settled exactly both: evidence (a bulk payment)
    both = _transfer(qconn, odm.id, party, f"Zaplata {first['number']} {second['number']}", [first, second])
    _automatic_match(qconn, odm.id, both, [first["id"], second["id"]])
    assert mine.title_resolves(qconn, odm.id, both, [first["id"], second["id"]])
    [case] = _cases_of(qconn, odm.id, both)
    assert case.source == "hard_identifier" and case.outcome_key == "bulk"

    # the title names one invoice, the automation settled two: its own choice, not evidence - skipped
    more = _transfer(qconn, odm.id, party, f"Zaplata {first['number']}", [first])
    _automatic_match(qconn, odm.id, more, [first["id"], second["id"]])
    assert not mine.title_resolves(qconn, odm.id, more, [first["id"], second["id"]])
    assert _cases_of(qconn, odm.id, more) == []

    # the number of the title is of no document (the automation decided by something else): skipped
    unknown = _transfer(qconn, odm.id, party, "Zaplata FV 999/99/2099",
                        [{"number": "FV 999/99/2099", "normalized": "FV999992099"}])
    _automatic_match(qconn, odm.id, unknown, [first["id"]])
    assert _cases_of(qconn, odm.id, unknown) == []

    # no number in the title at all: skipped
    bare = _transfer(qconn, odm.id, party, "Przelew", [])
    _automatic_match(qconn, odm.id, bare, [first["id"]])
    assert _cases_of(qconn, odm.id, bare) == []


def test_a_number_shared_by_two_documents_does_not_decide_alone(qconn, odm):
    """FV 12/09/2026 of two sellers in the fixtures: the automation chose by the counterparty, not by the title."""
    row = qconn.execute(
        """
        SELECT d.subject_id, d.payload FROM l3_decision_log d JOIN l1_bank_transaction t ON t.id = d.subject_id
         WHERE d.company_id = %s AND d.method = 'rule' AND d.kind = 'match' AND t.title_raw LIKE 'FV 12/09/2026%%'
        """,
        (odm.id,),
    ).fetchone()
    assert row is not None
    tx, payload = row
    assert qconn.execute("SELECT count(*) FROM l1_document WHERE company_id = %s AND number_normalized = 'FV1292026' "
                         "AND superseded_by IS NULL", (odm.id,)).fetchone() == (2,)
    assert not mine.title_resolves(qconn, odm.id, tx, [i["document_id"] for i in payload["items"]])
    assert _cases_of(qconn, odm.id, tx) == []
    # every automatic case that is counted is decided by its title
    counted = [c for c in mine.collect(qconn, odm.id) if c.source == "hard_identifier"]
    assert counted
    for case in counted:
        payload = qconn.execute("SELECT payload FROM l3_decision_log WHERE id = %s", (case.decision_id,)).fetchone()[0]
        assert mine.title_resolves(qconn, odm.id, case.subject_id,
                                   [i["document_id"] for i in payload["items"] if i.get("document_id")])
