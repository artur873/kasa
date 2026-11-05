"""Memorial (accrual) amounts of documents, lines and internal rows - one definition for the
candidate card, the allocation and the company report (briefing, step 8).

Revenue (sales) and cost (purchases), PLN minor units, without sign (the direction says which):

* advance invoice (``advance``) and proforma: 0 - an advance is never revenue, a proforma is an offer;
* settlement invoice (``settlement``): the whole order = sum of its invoice lines (FA(3) lists the
  whole order; the document total is net of the advances);
* correction: its own (negative) net - the difference;
* purchase receipt, bill of a person and foreign invoice without Polish VAT: gross (nothing to deduct);
* other documents: net; a foreign currency amount times the document rate (PLN is derived).

A document total is split over its lines in proportion to their base amounts (largest remainder,
so the lines always sum to the document to the grosz). The recognition date is the service period
spread over days, else the sale date, else the issue date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg

ZERO_KINDS = frozenset({"advance", "proforma"})
GROSS_COST_KINDS = frozenset({"receipt", "bill", "foreign"})


def distribute(total: int, weights: list[Fraction | int]) -> list[int]:
    """Split ``total`` in proportion to ``weights`` so that the parts sum exactly to ``total``.

    Largest remainder method on exact fractions (works for negative totals); all-zero weights
    split equally.
    """
    if not weights:
        return []
    weights = [Fraction(w) for w in weights]
    denominator = sum(weights)
    if denominator == 0:
        weights = [Fraction(1)] * len(weights)
        denominator = Fraction(len(weights))
    exact = [Fraction(total) * w / denominator for w in weights]
    parts = [e.numerator // e.denominator for e in exact]          # floor
    remainder = total - sum(parts)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:remainder]:
        parts[i] += 1
    return parts


def to_pln(amount_minor: int | None, currency: str, fx_rate: Decimal | None) -> int:
    if amount_minor is None:
        return 0
    if currency == "PLN" or fx_rate is None:
        return int(amount_minor)
    return int((Decimal(amount_minor) * Decimal(fx_rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass
class LineFacts:
    id: UUID
    line_no: int
    name: str
    net_minor: int | None
    vat_minor: int | None
    gross_minor: int | None
    line_kind: str
    memorial_minor: int = 0       # PLN, share of the document memorial total
    cash_weight_minor: int = 0    # PLN gross, weight of the line in a payment of the document


@dataclass
class DocumentFacts:
    id: UUID
    company_id: UUID
    direction: str
    kind: str
    number_raw: str
    ksef_number: str | None
    currency: str
    net_minor: int | None
    vat_minor: int | None
    gross_minor: int
    fx_rate: Decimal | None
    gross_pln_minor: int | None
    issue_date: date
    sale_date: date | None
    service_period_start: date | None
    service_period_end: date | None
    corrects_document_id: UUID | None
    counterparty_id: UUID | None
    counterparty_name: str | None
    counterparty_kind: str | None
    counterparty_pseudonym: str | None
    seller_nip: str | None
    buyer_nip: str | None
    issue_place: str | None
    lines: list[LineFacts] = field(default_factory=list)
    memorial_minor: int = 0
    gross_pln: int = 0

    @property
    def recognition_window(self) -> tuple[date, date]:
        if self.service_period_start and self.service_period_end:
            return self.service_period_start, self.service_period_end
        day = self.sale_date or self.issue_date
        return day, day

    @property
    def counterparty_display(self) -> str | None:
        """The other party as it may be shown: persons only by pseudonym."""
        if self.counterparty_kind == "person":
            return self.counterparty_pseudonym
        return self.counterparty_name


def document_memorial_total(doc: DocumentFacts) -> int:
    if doc.kind in ZERO_KINDS:
        return 0
    if doc.direction == "purchase" and doc.kind in GROSS_COST_KINDS:
        if doc.gross_pln_minor is not None:
            return int(doc.gross_pln_minor)
        return to_pln(doc.gross_minor, doc.currency, doc.fx_rate)
    if doc.kind == "settlement":
        invoice_lines = [ln for ln in doc.lines if ln.line_kind == "invoice" and ln.net_minor is not None]
        if invoice_lines:
            return to_pln(sum(ln.net_minor for ln in invoice_lines), doc.currency, doc.fx_rate)
    base = doc.net_minor if doc.net_minor is not None else doc.gross_minor
    return to_pln(base, doc.currency, doc.fx_rate)


def _line_base(doc: DocumentFacts, line: LineFacts) -> int:
    if doc.direction == "purchase" and doc.kind in GROSS_COST_KINDS:
        value = line.gross_minor if line.gross_minor is not None else line.net_minor
    else:
        value = line.net_minor if line.net_minor is not None else line.gross_minor
    return int(value or 0)


def _line_gross(line: LineFacts) -> int:
    if line.gross_minor is not None:
        return int(line.gross_minor)
    return int(line.net_minor or 0) + int(line.vat_minor or 0)


def compute_amounts(doc: DocumentFacts) -> DocumentFacts:
    """Fill memorial and cash weights of the document and its lines (in place)."""
    doc.memorial_minor = document_memorial_total(doc)
    doc.gross_pln = int(doc.gross_pln_minor) if doc.gross_pln_minor is not None else to_pln(
        doc.gross_minor, doc.currency, doc.fx_rate)
    if doc.lines:
        if doc.kind == "settlement":
            lines = [ln for ln in doc.lines if ln.line_kind == "invoice"] or doc.lines
        else:
            lines = doc.lines
        parts = distribute(doc.memorial_minor, [_line_base(doc, ln) for ln in lines])
        for line, part in zip(lines, parts, strict=True):
            line.memorial_minor = part
        cash = distribute(doc.gross_pln, [_line_gross(ln) for ln in doc.lines])
        for line, part in zip(doc.lines, cash, strict=True):
            line.cash_weight_minor = part
    return doc


def split_by_month(amount: int, window: tuple[date, date]) -> dict[str, int]:
    """Amount spread over the days of the window, per month 'YYYY-MM' (exact to the grosz)."""
    start, end = window
    if end < start:
        start, end = end, start
    days: dict[str, int] = defaultdict(int)
    day = start
    while day <= end:
        days[f"{day.year:04d}-{day.month:02d}"] += 1
        day += timedelta(days=1)
    months = sorted(days)
    parts = distribute(amount, [days[m] for m in months])
    return dict(zip(months, parts, strict=True))


def load_documents(conn: psycopg.Connection, company_id: UUID, *, current_only: bool = True,
                   ids: list[UUID] | None = None) -> dict[UUID, DocumentFacts]:
    """Current documents of a company with their lines and amounts."""
    rows = conn.execute(
        """
        SELECT d.id, d.company_id, d.direction::text, d.kind::text, d.number_raw, d.ksef_number, d.currency,
               d.net_minor, d.vat_minor, d.gross_minor, d.fx_rate, d.gross_pln_minor, d.issue_date, d.sale_date,
               d.service_period_start, d.service_period_end, d.corrects_document_id, d.counterparty_id,
               cp.name, cp.kind::text, cp.name_pseudonymized, d.seller_nip, d.buyer_nip,
               coalesce(d.extra ->> 'issue_place', NULL)
          FROM l1_document d
          LEFT JOIN l1_counterparty cp ON cp.id = d.counterparty_id
         WHERE d.company_id = %(company)s
           AND (NOT %(current)s OR d.superseded_by IS NULL)
           AND (%(ids)s::uuid[] IS NULL OR d.id = ANY(%(ids)s::uuid[]))
        """,
        {"company": company_id, "current": current_only, "ids": ids},
    ).fetchall()
    docs: dict[UUID, DocumentFacts] = {}
    for r in rows:
        docs[r[0]] = DocumentFacts(
            id=r[0], company_id=r[1], direction=r[2], kind=r[3], number_raw=r[4], ksef_number=r[5],
            currency=r[6].strip(), net_minor=r[7], vat_minor=r[8], gross_minor=r[9], fx_rate=r[10],
            gross_pln_minor=r[11], issue_date=r[12], sale_date=r[13], service_period_start=r[14],
            service_period_end=r[15], corrects_document_id=r[16], counterparty_id=r[17], counterparty_name=r[18],
            counterparty_kind=r[19], counterparty_pseudonym=r[20], seller_nip=r[21], buyer_nip=r[22],
            issue_place=r[23],
        )
    if docs:
        for r in conn.execute(
            """
            SELECT id, document_id, line_no, name, net_minor, vat_minor, gross_minor, line_kind
              FROM l1_document_line WHERE document_id = ANY(%s) ORDER BY document_id, line_no
            """,
            (list(docs),),
        ).fetchall():
            docs[r[1]].lines.append(LineFacts(id=r[0], line_no=r[2], name=r[3], net_minor=r[4], vat_minor=r[5],
                                              gross_minor=r[6], line_kind=r[7]))
    for doc in docs.values():
        compute_amounts(doc)
    return docs


def head_documents(conn: psycopg.Connection, company_id: UUID) -> dict[UUID, UUID]:
    """Every document version -> the current head of its chain (matches may point at an old version)."""
    rows = conn.execute(
        "SELECT document_id, head_id FROM document_lineage WHERE company_id = %s", (company_id,)
    ).fetchall()
    return dict(rows)


@dataclass
class InternalRecordFacts:
    id: UUID
    mapping_key: str
    record_date: date | None
    amount_minor: int | None
    currency: str
    cost_object_ref: str | None
    settlement_type: str | None
    person_pseudonym: str | None
    fields: dict[str, Any]

    @property
    def memorial_minor(self) -> int:
        return int(self.amount_minor or 0)


def load_internal_records(conn: psycopg.Connection, company_id: UUID) -> dict[UUID, InternalRecordFacts]:
    rows = conn.execute(
        """
        SELECT id, mapping_key, record_date, amount_minor, currency, cost_object_ref, settlement_type,
               person_pseudonym, fields
          FROM l1_internal_record WHERE company_id = %s AND superseded_by IS NULL
        """,
        (company_id,),
    ).fetchall()
    return {r[0]: InternalRecordFacts(r[0], r[1], r[2], r[3], r[4].strip(), r[5], r[6], r[7], r[8]) for r in rows}
