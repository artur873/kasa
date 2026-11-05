"""Export for the accounting office: documents of a month with their allocation and status (CSV).

One row per current document (issued or sold in the month) and per row of an internal file of the
month that is a cost of its own: direction, kind, number, KSeF number, dates, counterparty (a person
only by pseudonym), amounts, memorial amount, paid (active matches), payment status, allocation
(object: share | ...) and its status (decided by a person / by a rule / in suggestions / none).
Semicolon-separated, decimal comma, UTF-8 with BOM (opens in a Polish spreadsheet as is).
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

import psycopg

from kasa.core.allocation.amounts import load_documents, load_internal_records
from kasa.core.allocation.objects import load_profiles, resolve
from kasa.core.allocation.subjects import evidence_of_documents, load_matches
from kasa.core.reports.company import month_bounds

HEADER = ["kierunek", "rodzaj", "numer", "numer_ksef", "data_wystawienia", "data_sprzedazy", "okres_od", "okres_do",
          "kontrahent", "nip", "waluta", "netto", "vat", "brutto", "brutto_pln", "memorialowo_pln", "zaplacono",
          "status_platnosci", "alokacja", "status_alokacji"]


def _amount(minor: int | None) -> str:
    if minor is None:
        return ""
    return f"{Decimal(minor) / 100:.2f}".replace(".", ",")


def export_rows(conn: psycopg.Connection, company_id: UUID, month: str) -> list[list[str]]:
    start, end = month_bounds(month)
    profiles = load_profiles(conn, company_id)
    documents = load_documents(conn, company_id)
    records = load_internal_records(conn, company_id)
    covered = evidence_of_documents(records, documents, load_matches(conn, company_id))
    allocations: dict[tuple[str, UUID], list[tuple[str, Decimal, str]]] = defaultdict(list)
    for subject_type, subject_id, cost_object_id, share, method in conn.execute(
        "SELECT subject_type::text, subject_id, cost_object_id, share, method::text FROM l3_allocation "
        "WHERE company_id = %s AND status = 'active'", (company_id,)).fetchall():
        target = resolve(profiles, cost_object_id)
        allocations[(subject_type, subject_id)].append((target.name if target else str(cost_object_id), share, method))
    pending = {(r[0], r[1]) for r in conn.execute(
        "SELECT subject_type::text, subject_id FROM l2_suggestion WHERE company_id = %s AND kind = 'allocation' "
        "AND status = 'open'", (company_id,)).fetchall()}
    paid = dict(conn.execute(
        """
        SELECT l.head_id, sum(coalesce(m.document_amount_minor, m.amount_minor))
          FROM l3_match m JOIN document_lineage l ON l.document_id = m.document_id
         WHERE m.company_id = %s AND m.status = 'active' GROUP BY l.head_id
        """,
        (company_id,),
    ).fetchall())

    def allocation_text(keys: list[tuple[str, UUID]]) -> tuple[str, str]:
        parts: dict[str, Decimal] = defaultdict(Decimal)
        methods = set()
        for key in keys:
            for name, share, method in allocations.get(key, []):
                parts[name] += Decimal(share) / len(keys)
                methods.add(method)
        decided = sum(1 for k in keys if allocations.get(k))
        if decided == len(keys) and keys:
            status = "reguła" if methods == {"rule"} else "człowiek" if methods == {"human"} else "człowiek+reguła"
        elif decided or any(k in pending for k in keys):
            status = "w sugestiach"
        else:
            status = "brak"
        text = " | ".join(f"{name}: {float(share):.6f}".replace(".", ",") for name, share in sorted(parts.items()))
        return text, status

    rows: list[list[str]] = []
    for doc in sorted(documents.values(), key=lambda d: (d.issue_date, d.number_raw)):
        in_month = start <= doc.issue_date <= end or (doc.sale_date is not None and start <= doc.sale_date <= end)
        if not in_month:
            continue
        keys = [("line", ln.id) for ln in doc.lines] or [("document", doc.id)]
        alloc, alloc_status = allocation_text(keys)
        settled = int(paid.get(doc.id) or 0)
        gross = int(doc.gross_minor)
        pay_status = ("zapłacona" if gross and abs(settled) >= abs(gross) else
                      "częściowo" if settled else "otwarta")
        nip = doc.buyer_nip if doc.direction == "sales" else doc.seller_nip
        rows.append([
            "sprzedaż" if doc.direction == "sales" else "zakup", doc.kind, doc.number_raw, doc.ksef_number or "",
            doc.issue_date.isoformat(), doc.sale_date.isoformat() if doc.sale_date else "",
            doc.service_period_start.isoformat() if doc.service_period_start else "",
            doc.service_period_end.isoformat() if doc.service_period_end else "",
            doc.counterparty_display or "", nip or "", doc.currency, _amount(doc.net_minor), _amount(doc.vat_minor),
            _amount(gross), _amount(doc.gross_pln), _amount(doc.memorial_minor), _amount(settled), pay_status,
            alloc, alloc_status,
        ])
    for record in sorted(records.values(), key=lambda r: (r.record_date or start, str(r.id))):
        if record.id in covered or not record.record_date or not start <= record.record_date <= end:
            continue
        alloc, alloc_status = allocation_text([("internal_record", record.id)])
        rows.append(["koszt", f"wewnętrzny ({record.settlement_type or record.mapping_key})", record.mapping_key,
                     "", record.record_date.isoformat(), "", "", "", record.person_pseudonym or "", "",
                     record.currency, "", "", _amount(record.amount_minor), _amount(record.amount_minor),
                     _amount(record.memorial_minor), "", "", alloc, alloc_status])
    return rows


def export_csv(conn: psycopg.Connection, company_id: UUID, month: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(HEADER)
    writer.writerows(export_rows(conn, company_id, month))
    return "﻿" + buffer.getvalue()
