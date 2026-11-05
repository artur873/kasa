"""Synthetic history for the rule tests: transfers of a known counterparty in earlier months.

The fixtures have one bank month (October) - a rule needs >= 5 cases in >= 2 months, so the tests add
transfers of ZUS / Orange in other months (a separate account, so no balance of the fixtures changes)
and decide them through the queue's API (``decide_other``), as a person would.
"""

from __future__ import annotations

import uuid
from datetime import date
from uuid import UUID

import psycopg

from kasa.core.decisions import decide_other


def counterparty(conn: psycopg.Connection, company_id: UUID, name_like: str) -> UUID:
    """The counterparty of a fixture transfer (as the bank sees it)."""
    return conn.execute(
        """
        SELECT counterparty_id FROM l1_bank_transaction
         WHERE company_id = %s AND counterparty_name_raw ILIKE %s AND counterparty_id IS NOT NULL
         ORDER BY booking_date LIMIT 1
        """,
        (company_id, name_like),
    ).fetchone()[0]


def synthetic_account(conn: psycopg.Connection, company_id: UUID) -> UUID:
    return conn.execute(
        """
        INSERT INTO l1_bank_account (company_id, iban, currency, kind, source, method, created_by)
        VALUES (%s, %s, 'PLN', 'current', 'test', 'code', 'pytest') RETURNING id
        """,
        (company_id, f"PL{uuid.uuid4().int % 10**26:026d}"),
    ).fetchone()[0]


def synthetic_transfers(conn: psycopg.Connection, company_id: UUID, party: UUID, days: list[date], *,
                        amount_minor: int = -143_824, title: str = "Skladki ZUS za {month}", op_type: str = "zus",
                        account_id: UUID | None = None) -> list[UUID]:
    account_id = account_id or synthetic_account(conn, company_id)
    out = []
    for n, day in enumerate(days):
        out.append(conn.execute(
            """
            INSERT INTO l1_bank_transaction (company_id, account_id, entry_reference, fingerprint, status,
                                             booking_date, amount_minor, currency, title_raw, op_type,
                                             counterparty_id, counterparty_name_raw, source, method, created_by)
            VALUES (%s, %s, %s, %s, 'booked', %s, %s, 'PLN', %s, %s, %s,
                    (SELECT name FROM l1_counterparty WHERE id = %s), 'test', 'code', 'pytest')
            RETURNING id
            """,
            (company_id, account_id, f"SYN-{uuid.uuid4().hex[:12]}", uuid.uuid4().hex, day, amount_minor - n,
             title.format(month=f"{day:%m/%Y}"), op_type, party, party),
        ).fetchone()[0])
    return out


def months(start: date, count: int, per_month: int = 1) -> list[date]:
    """``per_month`` days in each of ``count`` months starting at ``start`` (the 5th, 10th, ...)."""
    out = []
    year, month = start.year, start.month
    for _ in range(count):
        out += [date(year, month, 5 + 3 * k) for k in range(per_month)]
        year, month = (year + (month == 12), month % 12 + 1)
    return out


def decide_category_by_person(conn: psycopg.Connection, company_id: UUID, transactions: list[UUID], category: str,
                              *, by: str = "artur") -> list[UUID]:
    """The person's "Inne… → kategoria" on each transfer (the queue's API, never apply_* directly)."""
    return [decide_other(conn, company_id, "transaction", tx, decided_by=by, kind="category",
                         category=category).decision_id for tx in transactions]
