"""Export for the accounting office: documents of a month with their allocation and status (CSV);
phase 5: the full hand-over (CSV / XLSX, evidence of every decision, ``export_log``) at the end.

One row per current document (issued or sold in the month) and per row of an internal file of the
month that is a cost of its own: direction, kind, number, KSeF number, dates, counterparty (a person
only by pseudonym), amounts, memorial amount, paid (active matches), payment status, allocation
(object: share | ...) and its status (decided by a person / by a rule / in suggestions / none).
Semicolon-separated, decimal comma, UTF-8 with BOM (opens in a Polish spreadsheet as is).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.allocation.amounts import DocumentFacts, load_documents, load_internal_records
from kasa.core.allocation.objects import load_profiles, resolve
from kasa.core.allocation.subjects import load_matches
from kasa.core.reports.company import evidence_documents, month_bounds

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
    covered = evidence_documents(records, documents, load_matches(conn, company_id))
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
        if not record.record_date or not start <= record.record_date <= end:
            continue
        if record.id in covered:
            # one cost, many pieces of evidence: the row proves a document, it is not a cost of its own
            link = covered[record.id]
            proved = documents.get(link["document_id"])
            label = f"dowód dokumentu {proved.number_raw if proved else link['document_id']}"
            if link.get("presumption"):
                label += " (przypuszczenie: ta sama osoba, kwota i data)"
            rows.append(["dowód", f"wewnętrzny ({record.settlement_type or record.mapping_key})", record.mapping_key,
                         "", record.record_date.isoformat(), "", "", "", record.person_pseudonym or "", "",
                         record.currency, "", "", _amount(record.amount_minor), _amount(record.amount_minor),
                         _amount(0), "", "", label, "dowód"])
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


# ---------------------------------------------------------------------------
# Phase 5: the full hand-over to the accounting office (briefing, steps 8 and 9)
# ---------------------------------------------------------------------------
#
# Three tables of one month, CSV (three files, the same convention as above) or XLSX (one workbook, three
# sheets): documents with their matches, payment status, allocation AND the evidence of each decision
# (decision id, date, who); transfers without a document with their status and category; the summary per
# cost object (the same numbers as the metrics margin_accrual / cash_contribution - one implementation,
# ``kasa.core.reports.company``). Every hand-over is a row of ``export_log`` (who, when, scope, SHA-256 of
# every file) - each disclosure of data to the office is logged (step 9).

DOCUMENT_HEADER = [*HEADER, "dopasowane_przelewy", "dopasowanie_decyzja", "dopasowanie_data", "dopasowanie_kto",
                   "alokacja_decyzja", "alokacja_data", "alokacja_kto"]
TRANSFER_HEADER = ["data_ksiegowania", "rachunek", "kwota", "waluta", "kontrahent", "tytul", "status", "podtyp",
                   "kategoria", "status_metoda", "decyzja", "decyzja_data", "decyzja_kto", "czeka_do", "numer_operacji"]
OBJECT_HEADER = ["obiekt", "status", "przychod_memorialowo", "koszt_memorialowo", "marza_memorialowo",
                 "kasowo_w_miesiacu", "przychod_w_sugestiach", "koszt_w_sugestiach"]
SHEETS = {"dokumenty": DOCUMENT_HEADER, "przelewy_bez_dokumentu": TRANSFER_HEADER, "obiekty": OBJECT_HEADER}
SUBTYPE_PL = {"zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata", "interest": "odsetki",
              "advance": "zaliczka", "deposit": "kaucja", "card_awaiting": "karta - czeka na fakturę",
              "salary": "wynagrodzenie", "refund": "zwrot", "duplicate": "duplikat", "other": "inne"}
STATUS_PL = {"no_document": "bez dokumentu", "to_clarify": "do wyjaśnienia", "matched": "dopasowany",
             "own_transfer": "transfer własny"}


def _evidence(conn: psycopg.Connection, decision_ids: list[UUID]) -> tuple[str, str, str]:
    """(decision ids, dates, who) of a set of decisions, as text cells."""
    ids = sorted({d for d in decision_ids if d})
    if not ids:
        return "", "", ""
    rows = conn.execute(
        "SELECT id, decided_at, decided_by FROM l3_decision_log WHERE id = ANY(%s::uuid[]) ORDER BY decided_at, id",
        ([str(i) for i in ids],),
    ).fetchall()
    return (" | ".join(str(r[0]) for r in rows), " | ".join(r[1].date().isoformat() for r in rows),
            " | ".join(sorted({r[2] for r in rows})))


def document_rows(conn: psycopg.Connection, company_id: UUID, month: str) -> list[list[str]]:
    """``export_rows`` (documents of the month and internal costs) plus the evidence of matches and allocations."""
    base = export_rows(conn, company_id, month)
    documents = load_documents(conn, company_id)
    by_number: dict[tuple[str, str], DocumentFacts] = {}
    for doc in documents.values():
        by_number[(doc.direction, doc.number_raw)] = doc
    out = []
    for row in base:
        if row[0] not in ("sprzedaż", "zakup"):
            out.append(row + [""] * (len(DOCUMENT_HEADER) - len(HEADER)))
            continue
        doc = by_number.get(("sales" if row[0] == "sprzedaż" else "purchase", row[2]))
        matches = conn.execute(
            """
            SELECT coalesce(t.entry_reference, t.id::text) || ' ' || t.booking_date::text, m.decision_id
              FROM l3_match m JOIN document_lineage l ON l.document_id = m.document_id
              JOIN l1_bank_transaction t ON t.id = m.transaction_id
             WHERE l.head_id = %s AND m.status = 'active' ORDER BY t.booking_date
            """,
            (doc.id,),
        ).fetchall() if doc else []
        subjects = ([("line", ln.id) for ln in doc.lines] or [("document", doc.id)]) if doc else []
        allocations = [r[0] for r in conn.execute(
            "SELECT decision_id FROM l3_allocation WHERE company_id = %s AND status = 'active' AND subject_id = ANY(%s)",
            (company_id, [s[1] for s in subjects]),
        ).fetchall()] if subjects else []
        out.append(row + [" | ".join(m[0] for m in matches), *_evidence(conn, [m[1] for m in matches]),
                          *_evidence(conn, allocations)])
    return out


def transfer_rows(conn: psycopg.Connection, company_id: UUID, month: str) -> list[list[str]]:
    """Booked transfers of the month without a document (status, category, evidence of the decision)."""
    start, end = month_bounds(month)
    rows = conn.execute(
        """
        SELECT t.booking_date, a.iban, t.amount_minor, t.currency, t.sensitive,
               CASE WHEN c.kind = 'person' THEN coalesce(c.name_pseudonymized, 'osoba')
                    WHEN c.id IS NOT NULL THEN coalesce(c.name_pseudonymized, c.name)
                    ELSE t.counterparty_pseudonym END,
               t.title_pseudonymized, s.final_status::text, s.subtype, s.method::text, s.decision_id, s.awaiting_until,
               (SELECT cd.category FROM l3_category_decision cd WHERE cd.subject_type = 'transaction'
                   AND cd.subject_id = t.id AND cd.status = 'active' LIMIT 1),
               (SELECT g.proposal ->> 'final_status' FROM l2_suggestion g WHERE g.subject_type = 'transaction'
                   AND g.subject_id = t.id AND g.kind = 'match' AND g.status = 'open' AND g.rank = 1 LIMIT 1),
               coalesce(t.entry_reference, t.id::text)
          FROM l1_bank_transaction t
          JOIN l1_bank_account a ON a.id = t.account_id
          LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
          LEFT JOIN l3_transaction_status s ON s.transaction_id = t.id AND s.status = 'active'
         WHERE t.company_id = %s AND t.status = 'booked' AND t.superseded_by IS NULL
           AND t.booking_date BETWEEN %s AND %s
           AND NOT EXISTS (SELECT 1 FROM l3_match m WHERE m.transaction_id = t.id AND m.status = 'active'
                            AND (m.document_id IS NOT NULL OR m.internal_record_id IS NOT NULL))
           AND coalesce(s.final_status::text, '') <> 'own_transfer' AND t.op_type <> 'own'
         ORDER BY t.booking_date, t.amount_minor
        """,
        (company_id, start, end),
    ).fetchall()
    out = []
    for (day, iban, amount, currency, sensitive, party, title, status, subtype, method, decision, awaiting,
         category, proposed, ref) in rows:
        ids, dates, who = _evidence(conn, [decision])
        shown_status = STATUS_PL.get(status, status) if status else (
            f"w sugestiach: {STATUS_PL.get(proposed, proposed)}" if proposed else "bez decyzji")
        out.append([day.isoformat(), iban, _amount(amount), currency.strip(), party or "",
                    "[tytuł wrażliwy - szczegóły w kolejce]" if sensitive else (title or ""), shown_status,
                    SUBTYPE_PL.get(subtype, subtype or ""), category or "", method or "", ids, dates, who,
                    awaiting.isoformat() if awaiting else "", ref])
    return out


def object_rows(conn: psycopg.Connection, company_id: UUID, month: str) -> list[list[str]]:
    from kasa.core.reports.company import company_report

    report = company_report(conn, company_id, month)
    return [[o.name, o.status, _amount(o.revenue_minor), _amount(o.cost_minor), _amount(o.margin_minor),
             _amount(o.cash_minor), _amount(o.revenue_pending_minor), _amount(o.cost_pending_minor)]
            for o in report.objects]


@dataclass
class ExportResult:
    log_id: UUID
    files: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Eksport dla biura zapisany (export_log {self.log_id}):"]
        lines += [f"  {f['name']}: {f['rows']} wierszy, {f['bytes']} B, SHA-256 {f['sha256'][:16]}…" for f in self.files]
        return "\n".join(lines)


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _xlsx_bytes(tables: dict[str, tuple[list[str], list[list[str]]]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, (header, rows) in tables.items():
        sheet = book.create_sheet(title=name[:31])
        sheet.append(header)
        for row in rows:
            cells = []
            for value in row:
                if isinstance(value, str) and re.fullmatch(r"-?\d+,\d{2}", value):
                    cells.append(Decimal(value.replace(",", ".")))
                else:
                    cells.append(value)
            sheet.append(cells)
        for cells in sheet.iter_rows(min_row=2):
            for cell in cells:
                if isinstance(cell.value, Decimal):
                    cell.number_format = "#,##0.00"
        sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def office_tables(conn: psycopg.Connection, company_id: UUID, month: str) -> dict[str, tuple[list[str], list[list[str]]]]:
    return {"dokumenty": (DOCUMENT_HEADER, document_rows(conn, company_id, month)),
            "przelewy_bez_dokumentu": (TRANSFER_HEADER, transfer_rows(conn, company_id, month)),
            "obiekty": (OBJECT_HEADER, object_rows(conn, company_id, month))}


def write_office_export(conn: psycopg.Connection, company_id: UUID, month: str, *, fmt: str, out_dir: Path | str,
                        exported_by: str, recipient: str = "biuro rachunkowe") -> ExportResult:
    """Write the export of one month (csv: three files, xlsx: one workbook) and log the hand-over."""
    if fmt not in ("csv", "xlsx"):
        raise ValueError("format: csv | xlsx")
    month_bounds(month)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    slug = conn.execute("SELECT slug FROM company WHERE id = %s", (company_id,)).fetchone()[0]
    tables = office_tables(conn, company_id, month)
    payloads: list[tuple[str, bytes, int]] = []
    if fmt == "csv":
        for name, (header, rows) in tables.items():
            payloads.append((f"{slug}_{month}_{name}.csv", _csv_bytes(header, rows), len(rows)))
    else:
        payloads.append((f"{slug}_{month}_biuro.xlsx", _xlsx_bytes(tables), sum(len(r) for _, r in tables.values())))
    files = []
    for name, content, count in payloads:
        (directory / name).write_bytes(content)
        files.append({"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                      "rows": count})
    log_id = conn.execute(
        "INSERT INTO export_log (company_id, exported_by, recipient, scope, files) VALUES (%s, %s, %s, %s, %s) "
        "RETURNING id",
        (company_id, exported_by, recipient,
         Jsonb({"company": slug, "month": month, "format": fmt, "tables": list(tables),
                "rows": {k: len(v[1]) for k, v in tables.items()}}), Jsonb(files)),
    ).fetchone()[0]
    return ExportResult(log_id, files)


def export_history(conn: psycopg.Connection, company_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, exported_by, recipient, scope, files, exported_at FROM export_log WHERE company_id = %s "
        "ORDER BY exported_at DESC LIMIT %s", (company_id, limit),
    ).fetchall()
    return [{"id": str(r[0]), "exported_by": r[1], "recipient": r[2], "scope": r[3], "files": r[4],
             "exported_at": r[5].isoformat()} for r in rows]

