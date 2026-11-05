"""The fixtures' reference truth (``fixtures/<company>/ground_truth.json``) as control-set labels.

* ``expected_labels(conn, company_id, truth, kind)`` - {(subject_type, subject_id): label} for
  every transaction (status, subtype, documents) or cost (cost object by name, UNASSIGNED or a
  split) of the file that exists in the database; documents are found by KSeF number, by
  number (e-mail PDFs), by sheet row (internal records) or by entry reference (refunds).
* ``import_ground_truth`` - a control set labeled from the file (``labeled_by = 'fixture'``),
  so phase 4 has a control set without clicking; ``sentinels_for`` - the same labels as
  sentinels of a drawn set.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from kasa.core import control_sets as cs
from kasa.core.normalize import normalize_invoice_number
from kasa.core.providers.ksef import fixtures_dir

# no_document_category of the file -> subtype of a label
SUBTYPE_BY_CATEGORY = {
    "ZUS": "zus", "Podatki (US)": "tax", "Prowizje bankowe": "fee", "Odsetki": "fee", "Wynagrodzenia": "salary",
    "Zaliczki": "advance", "Kaucje": "deposit", "Zajęcie komornicze": "other",
}


def default_path(company_slug: str) -> Path | None:
    path = fixtures_dir() / company_slug / "ground_truth.json"
    return path if path.is_file() else None


def load(path: Path | str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def expected_labels(conn: psycopg.Connection, company_id: UUID, truth: dict[str, Any] | None,
                    kind: str) -> dict[tuple[str, UUID], dict[str, Any]]:
    """Labels known from a ground-truth file, keyed by subject (only subjects found in the database)."""
    if not truth:
        return {}
    if kind == "transaction":
        return _expected_transactions(conn, company_id, truth)
    return _expected_costs(conn, company_id, truth)


def _tx_by_reference(conn: psycopg.Connection, company_id: UUID) -> dict[str, UUID]:
    rows = conn.execute(
        """
        SELECT entry_reference, id FROM l1_bank_transaction
         WHERE company_id = %s AND superseded_by IS NULL AND status = 'booked' AND entry_reference IS NOT NULL
        """,
        (company_id,),
    ).fetchall()
    return dict(rows)


def _document_by_ksef(conn: psycopg.Connection, company_id: UUID) -> dict[str, UUID]:
    rows = conn.execute(
        """
        SELECT ksef_number, id FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL AND ksef_number IS NOT NULL
        """,
        (company_id,),
    ).fetchall()
    return dict(rows)


def _document_by_number(conn: psycopg.Connection, company_id: UUID, number: str,
                        direction: str | None = None) -> UUID | None:
    try:
        normalized = normalize_invoice_number(number)
    except (TypeError, ValueError):
        return None
    row = conn.execute(
        """
        SELECT id FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL AND number_normalized = %s
           AND (%s::text IS NULL OR direction::text = %s::text)
         ORDER BY (ksef_number IS NOT NULL) DESC, issue_date DESC LIMIT 1
        """,
        (company_id, normalized, direction, direction),
    ).fetchone()
    return row[0] if row else None


def _internal_by_row(conn: psycopg.Connection, company_id: UUID, sheet_row: int, file_hint: str | None) -> UUID | None:
    row = conn.execute(
        """
        SELECT r.id FROM l1_internal_record r JOIN l0_raw f ON f.id = r.file_raw_id
         WHERE r.company_id = %s AND r.superseded_by IS NULL AND r.row_no = %s
         ORDER BY (%s::text IS NOT NULL AND coalesce(f.external_ref, '') LIKE %s || '%%') DESC,
                  (f.content_type LIKE '%%sheet%%') DESC, r.created_at
         LIMIT 1
        """,
        (company_id, sheet_row, file_hint, file_hint or ""),
    ).fetchone()
    return row[0] if row else None


def _resolve_doc(conn: psycopg.Connection, company_id: UUID, doc: dict[str, Any], by_ksef: dict[str, UUID],
                 by_ref: dict[str, UUID]) -> dict[str, str] | None:
    doc_type = doc.get("doc_type")
    if doc_type == "ksef" and doc.get("doc_id") in by_ksef:
        return {"type": "document", "id": str(by_ksef[doc["doc_id"]])}
    if doc_type in ("ksef", "email_pdf") and doc.get("invoice_number"):
        found = _document_by_number(conn, company_id, doc["invoice_number"], doc.get("direction") or "purchase")
        return {"type": "document", "id": str(found)} if found else None
    if doc_type == "pm_sheet" and doc.get("sheet_row"):
        file_hint = str(doc.get("doc_id") or "").split(":")[0] or None
        found = _internal_by_row(conn, company_id, int(doc["sheet_row"]), file_hint)
        return {"type": "internal_record", "id": str(found)} if found else None
    if doc_type == "bank_tx" and doc.get("doc_id") in by_ref:
        return {"type": "transaction", "id": str(by_ref[doc["doc_id"]])}
    return None


def _expected_transactions(conn: psycopg.Connection, company_id: UUID,
                           truth: dict[str, Any]) -> dict[tuple[str, UUID], dict[str, Any]]:
    by_ref = _tx_by_reference(conn, company_id)
    by_ksef = _document_by_ksef(conn, company_id)
    out: dict[tuple[str, UUID], dict[str, Any]] = {}
    for tx in truth.get("transactions", []):
        tx_id = by_ref.get(tx.get("entry_reference"))
        if tx_id is None:
            continue
        status = tx["expected_status"]
        label: dict[str, Any] = {"status": status}
        if status == "no_document":
            label["subtype"] = SUBTYPE_BY_CATEGORY.get(tx.get("no_document_category") or "", "other")
        elif status == "matched":
            docs = [_resolve_doc(conn, company_id, d, by_ksef, by_ref) for d in tx.get("matched_documents") or []]
            label["documents"] = [d for d in docs if d]
            if not label["documents"] or len(label["documents"]) < len(docs):
                label["other_document"] = not label["documents"]
        label["note"] = f"ground truth: {tx.get('scenario_key') or tx.get('scenario')}"
        out[("transaction", tx_id)] = label
    return out


def _cost_label(allocation: Sequence[dict[str, Any]] | None, single: str | None = None) -> dict[str, Any] | None:
    if single:
        return {"unassigned": True} if single == "UNASSIGNED" else {"cost_object": single}
    items = list(allocation or [])
    if not items:
        return None
    if len(items) == 1:
        name = items[0].get("event_name") or items[0].get("event")
        return {"unassigned": True} if name == "UNASSIGNED" else {"cost_object": name}
    return {"split": [{"name": i.get("event_name") or i.get("event"), "share": str(i.get("share"))} for i in items]}


def _expected_costs(conn: psycopg.Connection, company_id: UUID,
                    truth: dict[str, Any]) -> dict[tuple[str, UUID], dict[str, Any]]:
    by_ksef = _document_by_ksef(conn, company_id)
    out: dict[tuple[str, UUID], dict[str, Any]] = {}
    for invoice in truth.get("invoices", []):
        if invoice.get("direction") != "purchase":
            continue
        doc_id = by_ksef.get(invoice.get("ksef_number"))
        label = _cost_label(invoice.get("final_allocation"))
        if doc_id and label:
            out[("document", doc_id)] = {**label, "note": f"ground truth: {invoice.get('invoice_number')}"}
    for document in truth.get("email_documents", []):
        doc_id = _document_by_number(conn, company_id, document.get("number") or "", "purchase")
        label = _cost_label(document.get("final_allocation"))
        if doc_id and label and ("document", doc_id) not in out:
            out[("document", doc_id)] = {**label, "note": f"ground truth: {document.get('id')}"}
    for row in truth.get("pm_sheet_rows", []):
        record = _internal_by_row(conn, company_id, int(row.get("sheet_row") or 0),
                                  str(row.get("id") or "").split(":")[0] or None)
        label = _cost_label(None, row.get("final_event_name") or row.get("final_event"))
        if record and label:
            out[("internal_record", record)] = {**label, "note": f"ground truth: {row.get('id')}"}
    return out


def import_ground_truth(conn: psycopg.Connection, company: Any, *, kind: str = "transaction",
                        truth: dict[str, Any] | None = None, set_id: UUID | None = None,
                        name: str | None = None) -> cs.ControlSetInfo:
    """A control set labeled from the reference file (default: the company's fixtures)."""
    if truth is None:
        path = default_path(company.slug)
        if path is None:
            raise cs.ControlSetError(f"brak pliku prawdy referencyjnej dla firmy {company.slug}")
        truth = load(path)
    return cs.import_labels(conn, company, kind=kind, expected=expected_labels(conn, company.id, truth, kind),
                            set_id=set_id, name=name or f"ground-truth-{kind}", source="ground_truth")


def sentinels_for(conn: psycopg.Connection, company: Any, kind: str,
                  path: Path | str | None = None) -> dict[tuple[str, UUID], dict[str, Any]]:
    """Known answers for the sentinels of a new set ({} when there is no reference file)."""
    path = path if path is not None else default_path(company.slug)
    truth = load(path) if path is not None and Path(path).is_file() else None
    return expected_labels(conn, company.id, truth, kind)
