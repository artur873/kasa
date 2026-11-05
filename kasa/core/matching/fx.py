"""The one exchange-rate function of matching (review of phase 2, point 6).

Rule: the NBP table A rate of the last business day BEFORE the date (``rate_for``). It is
used for the named difference "kurs" (fx) between a foreign-currency document and the PLN
of a transfer or a card payment. Stored facts are never changed: the bank's own rate stays
in ``l1_bank_transaction``, the document's own rate (KursWaluty) in ``l1_document``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from kasa.core.normalize import from_minor, to_minor

NBP_SOURCE = "nbp_a"
MAX_DAYS_BACK = 10          # a gap longer than Easter + a weekend means the table is missing


def rate_for(conn: psycopg.Connection, currency: str, day: date, *, company_id: Any = None,
             source: str = NBP_SOURCE) -> tuple[Decimal, date] | None:
    """(rate PLN per 1 unit, table date) of the last NBP table A published before ``day``.

    PLN gives ``(1, day)``. ``None`` when no table is stored in the 10 days before.
    """
    currency = currency.upper().strip()
    if currency == "PLN":
        return Decimal(1), day
    row = conn.execute(
        """
        SELECT rate, rate_date FROM l1_fx_rate
         WHERE currency = %s AND source = %s AND rate_date < %s AND rate_date >= %s
           AND (company_id IS NULL OR company_id = %s)
         ORDER BY rate_date DESC, company_id NULLS LAST LIMIT 1
        """,
        (currency, source, day, day - timedelta(days=MAX_DAYS_BACK), company_id),
    ).fetchone()
    if row is None:
        return None
    return Decimal(row[0]), row[1]


def convert_minor(amount_minor: int, currency: str, rate: Decimal) -> int:
    """Minor units of ``currency`` -> PLN grosze at ``rate`` (half up)."""
    return to_minor(from_minor(amount_minor, currency) * rate, "PLN", rounding=ROUND_HALF_UP)


class RateCache:
    """``rate_for`` with a per-run cache (one query per currency and day)."""

    def __init__(self, conn: psycopg.Connection, company_id: Any = None):
        self.conn = conn
        self.company_id = company_id
        self._cache: dict[tuple[str, date], tuple[Decimal, date] | None] = {}

    def __call__(self, currency: str, day: date) -> tuple[Decimal, date] | None:
        key = (currency.upper().strip(), day)
        if key not in self._cache:
            self._cache[key] = rate_for(self.conn, currency, day, company_id=self.company_id)
        return self._cache[key]
