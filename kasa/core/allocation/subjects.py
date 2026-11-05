"""Subjects of allocation and the rule "one cost, many pieces of evidence" (briefing, step 5).

A subject is what gets an allocation: a document line, a document without lines, a row of an
internal file, a transfer without a document. The same cost may be seen three times - a bill
PDF, a PM sheet row and a BLIK transfer - and is allocated (and counted) once, by precedence
**document > internal record > transfer**:

* a transfer matched (``l3_match``) to a document or a row is not a subject: the cash follows
  the allocation of the document (or row);
* a row is not a subject (and not a cost of its own) when it is evidence of a document: the same
  transfer is matched to both, or - without any match yet - the row names the same person as the
  bill of a person, with the same amount and a date within a month;
* a transfer is a subject only when it has no document: its final status says so (no document,
  to clarify, matched only to another transfer), the matching layer proposes "no document", or -
  before any matching - its title carries no invoice reference.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.allocation.amounts import (
    DocumentFacts,
    InternalRecordFacts,
    LineFacts,
    head_documents,
)

DUPLICATE_DAYS = 31


@dataclass
class Subject:
    subject_type: str                   # line | document | internal_record | transaction
    subject_id: UUID
    text: str
    tokens: frozenset[str]
    window: tuple[date, date] | None
    window_source: str | None
    text_windows: list[tuple[date, date]] = field(default_factory=list)
    city_key: str | None = None
    city_from_text: bool = False
    direction: str | None = None
    client_tokens: frozenset[str] = frozenset()
    counterparty_id: UUID | None = None
    counterparty_kind: str | None = None
    amount_minor: int = 0               # memorial PLN (lines, rows) / signed amount (transfers)
    month_only: bool = False
    persons: frozenset[str] = frozenset()
    document: DocumentFacts | None = None
    line: LineFacts | None = None
    record: InternalRecordFacts | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, UUID]:
        return self.subject_type, self.subject_id


@dataclass
class MatchIndex:
    """Active matches of the company (by transfer and by target)."""

    by_transaction: dict[UUID, list[dict[str, Any]]] = field(default_factory=dict)
    transactions_of_document: dict[UUID, set[UUID]] = field(default_factory=dict)
    transactions_of_record: dict[UUID, set[UUID]] = field(default_factory=dict)


def load_matches(conn: psycopg.Connection, company_id: UUID) -> MatchIndex:
    heads = head_documents(conn, company_id)
    has_related = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'l3_match' "
        "AND column_name = 'related_transaction_id')"
    ).fetchone()[0]
    related = "related_transaction_id" if has_related else "NULL::uuid"
    rows = conn.execute(
        f"""
        SELECT transaction_id, document_id, internal_record_id, {related}, amount_minor, group_id, id
          FROM l3_match WHERE company_id = %s AND status = 'active'
         ORDER BY transaction_id, created_at, id
        """,
        (company_id,),
    ).fetchall()
    index = MatchIndex()
    for tx, doc, rec, rel, amount, group, mid in rows:
        doc = heads.get(doc, doc) if doc else None
        index.by_transaction.setdefault(tx, []).append(
            {"document_id": doc, "internal_record_id": rec, "related_transaction_id": rel,
             "amount_minor": int(amount), "group_id": group, "id": mid})
        if doc:
            index.transactions_of_document.setdefault(doc, set()).add(tx)
        if rec:
            index.transactions_of_record.setdefault(rec, set()).add(tx)
    return index


def evidence_of_documents(records: dict[UUID, InternalRecordFacts], documents: dict[UUID, DocumentFacts],
                          matches: MatchIndex) -> dict[UUID, dict[str, Any]]:
    """Internal rows that are evidence of a document (the document is the cost). record id -> link."""
    covered: dict[UUID, dict[str, Any]] = {}
    for tx, items in matches.by_transaction.items():
        docs = [i["document_id"] for i in items if i["document_id"]]
        for item in items:
            rec = item["internal_record_id"]
            if rec and docs and rec in records:
                covered[rec] = {"document_id": docs[0], "reason": "l3_match", "transaction_id": tx}
    by_person: dict[str, list[DocumentFacts]] = defaultdict(list)
    for doc in documents.values():
        if doc.direction == "purchase" and doc.counterparty_kind == "person" and doc.counterparty_pseudonym:
            by_person[doc.counterparty_pseudonym].append(doc)
    for rec in records.values():
        if rec.id in covered or not rec.person_pseudonym or rec.amount_minor is None or rec.record_date is None:
            continue
        found = [d for d in by_person.get(rec.person_pseudonym, [])
                 if d.gross_pln == rec.amount_minor
                 and abs(((d.sale_date or d.issue_date) - rec.record_date).days) <= DUPLICATE_DAYS]
        if len(found) == 1:
            covered[rec.id] = {"document_id": found[0].id, "reason": "same_person_amount_date"}
    return covered


def transaction_states(conn: psycopg.Connection, company_id: UUID) -> dict[UUID, dict[str, Any]]:
    """Final status and the best open match proposal of every transfer (what the matching layer says)."""
    states: dict[UUID, dict[str, Any]] = defaultdict(dict)
    for tx, status, subtype in conn.execute(
        "SELECT transaction_id, final_status::text, subtype FROM l3_transaction_status "
        "WHERE company_id = %s AND status = 'active'",
        (company_id,),
    ).fetchall():
        states[tx]["status"], states[tx]["subtype"] = status, subtype
    for tx, proposal in conn.execute(
        """
        SELECT DISTINCT ON (subject_id) subject_id, proposal FROM l2_suggestion
         WHERE company_id = %s AND kind = 'match' AND subject_type = 'transaction' AND status = 'open'
         ORDER BY subject_id, rank, score DESC NULLS LAST, created_at
        """,
        (company_id,),
    ).fetchall():
        states[tx]["proposal"] = proposal or {}
    return states


def is_direct_transaction(tx: dict[str, Any], matches: MatchIndex, state: dict[str, Any]) -> bool:
    """A transfer without a document gets its own allocation (see the module docstring)."""
    if tx["op_type"] == "own" or tx["counterparty_kind"] == "own" or state.get("status") == "own_transfer":
        return False
    items = matches.by_transaction.get(tx["id"], [])
    if any(i["document_id"] or i["internal_record_id"] for i in items):
        return False
    status = state.get("status")
    if status in ("no_document", "to_clarify") or (status == "matched" and items):
        return True
    if status == "matched":
        return False
    proposal = state.get("proposal")
    if proposal is not None:
        if proposal.get("final_status") in ("no_document", "to_clarify", "own_transfer"):
            return proposal.get("final_status") != "own_transfer"
        targets = [i for i in proposal.get("items") or [] if i.get("document_id") or i.get("internal_record_id")]
        return not targets
    return not tx["invoice_reference"] and tx["intent"] != "invoice_payment"
