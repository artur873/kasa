"""Facts of a subject as rules see them, and the canonical counterparty.

The same keys the phase 4 contract gives ``applicable_rules`` (4A: transactions, 4B: lines,
documents, internal rows, transactions): ``counterparty_id``, ``counterparty_kind``, ``op_type``,
``intent``, ``title``, ``amount_minor``, ``direction``, ``counterparty_iban``, ``subject_type``.

A counterparty seen in the bank with a one-off IBAN (Orange: every invoice has its own virtual
account) is the same firm as the counterparty with the NIP whose IBANs contain it: rules speak of
the canonical counterparty (``canonical_counterparty``), so twelve payments to twelve IBANs are one
"Orange" for mining, scope and batches.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.normalize import normalize_iban


def clean_iban(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_iban(value)
    except (TypeError, ValueError):
        return None


def canonical_counterparty(conn: psycopg.Connection, counterparty_id: UUID | str | None) -> UUID | None:
    """The current counterparty with a tax id that shares an IBAN or the name with this one (else itself)."""
    if counterparty_id is None or counterparty_id == "":
        return None
    row = conn.execute(
        """
        WITH RECURSIVE head AS (
            SELECT id, superseded_by, 0 AS depth FROM l1_counterparty WHERE id = %s
            UNION ALL
            SELECT c.id, c.superseded_by, h.depth + 1 FROM l1_counterparty c JOIN head h ON c.id = h.superseded_by
             WHERE h.depth < 20
        )
        SELECT c.id, c.nip, c.foreign_tax_id,
               (SELECT n.id FROM l1_counterparty n
                 WHERE n.company_id = c.company_id AND n.superseded_by IS NULL AND n.id <> c.id
                   AND (n.nip IS NOT NULL OR n.foreign_tax_id IS NOT NULL)
                   AND (n.ibans && c.ibans OR (c.name_key IS NOT NULL AND n.name_key = c.name_key))
                 ORDER BY n.created_at, n.id LIMIT 1)
          FROM l1_counterparty c
         WHERE c.id = (SELECT id FROM head WHERE superseded_by IS NULL ORDER BY depth DESC LIMIT 1)
        """,
        (str(counterparty_id),),
    ).fetchone()
    if row is None:
        return UUID(str(counterparty_id))
    own_id, nip, foreign, other = row
    if nip or foreign or other is None:
        return own_id
    return other


def counterparty_name(conn: psycopg.Connection, counterparty_id: UUID | str | None) -> str | None:
    if not counterparty_id:
        return None
    row = conn.execute("SELECT name FROM l1_counterparty WHERE id = %s", (str(counterparty_id),)).fetchone()
    return row[0] if row else None


def subject_facts(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID) -> dict[str, Any]:
    """Facts of one subject (empty dict when it is not a subject rules speak about)."""
    with conn.cursor(row_factory=dict_row) as cur:
        if subject_type == "transaction":
            r = cur.execute(
                """
                SELECT t.id, t.counterparty_id, c.kind::text AS counterparty_kind, t.op_type::text AS op_type,
                       coalesce(t.title_raw, '') AS title, t.amount_minor, t.counterparty_iban_raw, t.booking_date,
                       t.sensitive, s.intent, t.counterparty_name_raw
                  FROM l1_bank_transaction t
                  LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
                  LEFT JOIN title_semantics_current s ON s.transaction_id = t.id
                 WHERE t.id = %s AND t.company_id = %s
                """,
                (subject_id, company_id),
            ).fetchone()
            if r is None:
                return {}
            return {
                "subject_type": "transaction", "transaction_id": str(r["id"]),
                "counterparty_id": str(r["counterparty_id"]) if r["counterparty_id"] else None,
                "counterparty_kind": r["counterparty_kind"], "op_type": r["op_type"], "title": r["title"],
                "amount_minor": int(r["amount_minor"]), "direction": "in" if r["amount_minor"] > 0 else "out",
                "intent": None if r["sensitive"] else r["intent"], "counterparty_iban": clean_iban(
                    r["counterparty_iban_raw"]),
                "date": r["booking_date"], "label": r["title"] or r["counterparty_name_raw"] or "",
                "counterparty_name": r["counterparty_name_raw"],
            }
        if subject_type in ("document", "line"):
            r = cur.execute(
                """
                SELECT d.id AS document_id, d.counterparty_id, c.kind::text AS counterparty_kind,
                       d.direction::text AS direction, d.number_raw, coalesce(d.gross_pln_minor, d.gross_minor) AS gross,
                       coalesce(d.sale_date, d.issue_date) AS day, l.name AS line_name,
                       coalesce(l.gross_minor, l.net_minor) AS line_amount, d.seller_name, d.buyer_name
                  FROM l1_document d
                  LEFT JOIN l1_document_line l ON l.id = %(line)s
                  LEFT JOIN l1_counterparty c ON c.id = d.counterparty_id
                 WHERE d.company_id = %(company)s
                   AND d.id = coalesce((SELECT document_id FROM l1_document_line WHERE id = %(line)s), %(doc)s)
                """,
                {"line": subject_id if subject_type == "line" else None,
                 "doc": subject_id if subject_type == "document" else None, "company": company_id},
            ).fetchone()
            if r is None:
                return {}
            party = r["seller_name"] if r["direction"] == "purchase" else r["buyer_name"]
            text = r["line_name"] if subject_type == "line" else f"{r['number_raw']} {party or ''}".strip()
            return {
                "subject_type": subject_type, "document_id": str(r["document_id"]),
                "counterparty_id": str(r["counterparty_id"]) if r["counterparty_id"] else None,
                "counterparty_kind": r["counterparty_kind"], "op_type": None, "title": text or "",
                "amount_minor": int((r["line_amount"] if subject_type == "line" else r["gross"]) or 0),
                "direction": r["direction"], "intent": None, "counterparty_iban": None, "date": r["day"],
                "label": f"{r['number_raw']}" + (f" - {r['line_name']}" if subject_type == "line" else ""),
                "counterparty_name": party,
            }
        if subject_type == "internal_record":
            r = cur.execute(
                """
                SELECT id, record_date, amount_minor, cost_object_ref, person_pseudonym, settlement_type
                  FROM l1_internal_record WHERE id = %s AND company_id = %s
                """,
                (subject_id, company_id),
            ).fetchone()
            if r is None:
                return {}
            return {
                "subject_type": "internal_record", "counterparty_id": None, "counterparty_kind": "person",
                "op_type": None, "title": r["cost_object_ref"] or "", "amount_minor": int(r["amount_minor"] or 0),
                "direction": "internal", "intent": None, "counterparty_iban": None, "date": r["record_date"],
                "label": f"{r['cost_object_ref'] or ''} {r['record_date'] or ''}".strip(),
                "counterparty_name": r["person_pseudonym"],
            }
    return {}


def month_of(day: date | None) -> str | None:
    return f"{day:%Y-%m}" if day else None
