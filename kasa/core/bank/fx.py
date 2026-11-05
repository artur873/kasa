"""Exchange rates (NBP table A) in ``l1_fx_rate``.

Rule from the briefing: a foreign-currency document is converted with the NBP
average rate of the last business day BEFORE the relevant date (sale/issue date
of the invoice, booking date of a card payment) - :func:`rate_before`.

Two sources behind one interface (:class:`FxRateSource`):
* :class:`FixtureNbpSource` - ``<fixtures>/reference/nbp_rates.json`` (fictional
  rates for tests; ``KASA_FIXTURES_DIR``);
* :class:`NbpApiSource` - https://api.nbp.pl/api/exchangerates/rates/a/{code}/{date}/
  (real, no key needed; not used by tests).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.providers.bank import fixtures_dir_path

NBP_SOURCE = "nbp_a"
NBP_API_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/{date}/?format=json"


@dataclass(frozen=True)
class FxRate:
    currency: str
    rate_date: date
    rate: Decimal            # PLN for 1 unit
    table_ref: str | None
    source: str = NBP_SOURCE


class FxRateSource(Protocol):
    def rate_on(self, currency: str, day: date) -> FxRate | None:
        """The published rate of exactly that day (None on days without a table)."""

    def rates_between(self, currency: str, start: date, end: date) -> list[FxRate]:
        """All published rates in the range."""


class FixtureNbpSource:
    """Rates from ``reference/nbp_rates.json`` (fictional, for tests and the mock)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else fixtures_dir_path() / "reference" / "nbp_rates.json"
        data = json.loads(self.path.read_bytes())
        self._rates: dict[str, dict[date, FxRate]] = {}
        for currency, items in data["rates"].items():
            for item in items:
                day = date.fromisoformat(item["date"])
                self._rates.setdefault(currency.upper(), {})[day] = FxRate(
                    currency.upper(), day, Decimal(item["mid"]), item.get("table"), NBP_SOURCE)

    @property
    def currencies(self) -> list[str]:
        return sorted(self._rates)

    def rate_on(self, currency: str, day: date) -> FxRate | None:
        return self._rates.get(currency.upper(), {}).get(day)

    def rates_between(self, currency: str, start: date, end: date) -> list[FxRate]:
        return sorted((r for d, r in self._rates.get(currency.upper(), {}).items() if start <= d <= end),
                      key=lambda r: r.rate_date)


class NbpApiSource:
    """The public NBP API (table A, average rates). Network access only here."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def _fetch(self, url: str) -> dict | None:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed https host
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:      # no table that day (weekend, holiday)
                return None
            raise

    def rate_on(self, currency: str, day: date) -> FxRate | None:
        data = self._fetch(NBP_API_URL.format(code=currency.lower(), date=day.isoformat()))
        if not data:
            return None
        item = data["rates"][0]
        return FxRate(currency.upper(), date.fromisoformat(item["effectiveDate"]), Decimal(str(item["mid"])),
                      item.get("no"), NBP_SOURCE)

    def rates_between(self, currency: str, start: date, end: date) -> list[FxRate]:
        url = (f"https://api.nbp.pl/api/exchangerates/rates/a/{currency.lower()}/"
               f"{start.isoformat()}/{end.isoformat()}/?format=json")
        data = self._fetch(url)
        if not data:
            return []
        return [FxRate(currency.upper(), date.fromisoformat(i["effectiveDate"]), Decimal(str(i["mid"])),
                       i.get("no"), NBP_SOURCE) for i in data["rates"]]


def store_rates(conn: psycopg.Connection, rates: Iterable[FxRate], *, created_by: str = "kasa.fx") -> int:
    """Insert shared reference rates (company_id NULL); repeating changes nothing. Returns rows inserted."""
    inserted = 0
    for rate in rates:
        row = conn.execute(
            """
            INSERT INTO l1_fx_rate (company_id, rate_date, currency, rate, table_ref, source, method, confidence,
                                    evidence, created_by)
            VALUES (NULL, %s, %s, %s, %s, %s, 'code', 1, %s, %s)
            ON CONFLICT (company_id, source, currency, rate_date) DO NOTHING
            RETURNING id
            """,
            (rate.rate_date, rate.currency, rate.rate, rate.table_ref, rate.source,
             Jsonb({"table": rate.table_ref}), created_by),
        ).fetchone()
        inserted += row is not None
    return inserted


def load_rates(conn: psycopg.Connection, source: FxRateSource, currencies: Iterable[str], start: date,
               end: date) -> int:
    total = 0
    for currency in currencies:
        total += store_rates(conn, source.rates_between(currency, start, end))
    return total


def rate_before(conn: psycopg.Connection, currency: str, day: date, *, source: str = NBP_SOURCE,
                company_id=None, max_days_back: int = 10) -> FxRate | None:
    """NBP rate of the last business day before ``day`` (the briefing's "kurs z dnia poprzedniego")."""
    if currency.upper() == "PLN":
        return None
    row = conn.execute(
        """
        SELECT currency, rate_date, rate, table_ref, source FROM l1_fx_rate
         WHERE currency = %s AND source = %s AND rate_date < %s AND rate_date >= %s
           AND (company_id IS NULL OR company_id = %s)
         ORDER BY rate_date DESC, company_id NULLS LAST LIMIT 1
        """,
        (currency.upper(), source, day, day - timedelta(days=max_days_back), company_id),
    ).fetchone()
    if row is None:
        return None
    return FxRate(row[0].strip(), row[1], Decimal(row[2]), row[3], row[4])
