"""Helpers of the matching tests: lookups by entry reference and small synthetic scenarios."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.matching.engine import Evaluation, TxResult, evaluate
from kasa.core.normalize import normalize_invoice_number


def tx_id(conn: psycopg.Connection, entry_reference: str) -> UUID:
    return conn.execute(
        "SELECT id FROM l1_bank_transaction WHERE entry_reference = %s AND superseded_by IS NULL",
        (entry_reference,),
    ).fetchone()[0]


def doc_id(conn: psycopg.Connection, number: str, direction: str | None = None) -> UUID:
    return conn.execute(
        """
        SELECT id FROM l1_document WHERE number_normalized = %s AND superseded_by IS NULL
           AND (%s::text IS NULL OR direction::text = %s::text)
         ORDER BY (ksef_number IS NOT NULL) DESC LIMIT 1
        """,
        (normalize_invoice_number(number), direction, direction),
    ).fetchone()[0]


def result_for(conn: psycopg.Connection, company_id: UUID, entry_reference: str, **kwargs: Any) -> TxResult:
    ev: Evaluation = evaluate(conn, company_id, **kwargs)
    found = [r for r in ev.results if r.tx.entry_reference == entry_reference]
    assert found, f"{entry_reference} is not a subject"
    return found[0]


def only(conn: psycopg.Connection, company_id: UUID, transaction_id: UUID) -> TxResult:
    ev = evaluate(conn, company_id, subject_ids={transaction_id})
    assert len(ev.results) == 1
    return ev.results[0]


def account_id(conn: psycopg.Connection, company_id: UUID, kind: str = "current") -> UUID:
    return conn.execute(
        "SELECT id FROM l1_bank_account WHERE company_id = %s AND kind = %s ORDER BY iban LIMIT 1",
        (company_id, kind),
    ).fetchone()[0]


def party(conn: psycopg.Connection, company_id: UUID, name: str, nip: str | None, iban: str | None) -> UUID:
    return conn.execute("SELECT kasa_upsert_counterparty(%s, %s, %s, %s)", (company_id, nip, name, iban)).fetchone()[0]


def add_doc(conn: psycopg.Connection, company_id: UUID, number: str, gross_minor: int, *, direction: str = "purchase",
            counterparty_id: UUID | None = None, nip: str | None = None, name: str | None = None,
            issue: date = date(2026, 10, 5), due: date | None = date(2026, 10, 20), kind: str = "vat",
            currency: str = "PLN", payment_iban: str | None = None, corrects: UUID | None = None,
            ksef_number: str | None = None, fx_rate: str | None = None) -> UUID:
    own_nip = conn.execute("SELECT nip FROM company WHERE id = %s", (company_id,)).fetchone()[0]
    seller_nip, buyer_nip = (nip, own_nip) if direction == "purchase" else (own_nip, nip)
    seller_name, buyer_name = (name, "ODM") if direction == "purchase" else ("ODM", name)
    gross_pln = gross_minor if currency == "PLN" else (
        round(gross_minor * float(fx_rate)) if fx_rate else None)
    return conn.execute(
        """
        INSERT INTO l1_document (company_id, direction, number_raw, number_normalized, kind, issue_date, due_date,
                                 seller_nip, buyer_nip, seller_name, buyer_name, payment_iban, currency, gross_minor,
                                 gross_pln_minor, counterparty_id, corrects_document_id, ksef_number, fx_rate,
                                 fx_rate_source, source, method, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'test', 'code',
                'pytest')
        RETURNING id
        """,
        (company_id, direction, number, normalize_invoice_number(number), kind, issue, due, seller_nip, buyer_nip,
         seller_name, buyer_name, payment_iban, currency, gross_minor, gross_pln, counterparty_id, corrects,
         ksef_number, fx_rate, "document" if fx_rate else None),
    ).fetchone()[0]


def add_tx(conn: psycopg.Connection, company_id: UUID, amount_minor: int, title: str, *,
           counterparty_id: UUID | None = None, name: str | None = None, iban: str | None = None,
           day: date = date(2026, 10, 21), op_type: str = "transfer", extracted: dict | None = None,
           original_amount_minor: int | None = None, original_currency: str | None = None) -> UUID:
    from psycopg.types.json import Jsonb

    from kasa.core.bank.extract import extract_title

    extracted = extracted if extracted is not None else extract_title([title], reference_date=day)
    return conn.execute(
        """
        INSERT INTO l1_bank_transaction (company_id, account_id, entry_reference, fingerprint, status, booking_date,
                                         transaction_date, amount_minor, currency, counterparty_name_raw,
                                         counterparty_iban_raw, title_raw, extracted, op_type, counterparty_id,
                                         original_amount_minor, original_currency, source, method, created_by)
        VALUES (%s, %s, %s, %s, 'booked', %s, %s, %s, 'PLN', %s, %s, %s, %s, %s, %s, %s, %s, 'test', 'code', 'pytest')
        RETURNING id
        """,
        (company_id, account_id(conn, company_id), f"T{uuid.uuid4().hex[:12]}", uuid.uuid4().hex, day, day,
         amount_minor, name, iban, title, Jsonb(extracted), op_type, counterparty_id, original_amount_minor,
         original_currency),
    ).fetchone()[0]


def ksef_number(nip: str, day: str = "20261005", serial: str = "A1B2C3D4E5F6") -> str:
    """A KSeF number with a valid CRC-8."""
    from kasa.core.bank.extract import _crc8

    head = f"{nip}-{day}-{serial}"
    return f"{head}-{_crc8(head.encode('ascii')):02X}"
