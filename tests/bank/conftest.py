"""Fixtures of the bank channel tests: the fixture company and its October ingestion."""

from __future__ import annotations

import json
from datetime import date

import psycopg
import pytest
from psycopg.rows import dict_row

from kasa.core.bank.fx import FixtureNbpSource, load_rates
from kasa.core.bank.ingest import ingest_company
from kasa.core.companies import Company, ensure_company
from kasa.core.extension import get_extension
from kasa.core.providers.bank import MockEnableBanking

from bank_paths import FIXTURES, OCTOBER



@pytest.fixture(scope="session")
def ground_truth() -> dict:
    return json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def eb_transactions() -> list[dict]:
    """Every EnableBanking transaction of the fixtures (booked and pending) with its account slug."""
    out = []
    for account in ("mbank_biezacy", "mbank_vat", "millennium_biezacy"):
        for name in ("transactions.json", "transactions_pending.json"):
            data = json.loads((FIXTURES / "bank" / "enablebanking" / account / name).read_text(encoding="utf-8"))
            out += [{**t, "_account": account} for t in data["transactions"]]
    return out


@pytest.fixture
def company(conn: psycopg.Connection) -> Company:
    created, _ = ensure_company(conn, get_extension("odm"))
    return created


@pytest.fixture
def provider() -> MockEnableBanking:
    return MockEnableBanking(FIXTURES)


@pytest.fixture
def ingested(conn: psycopg.Connection, company: Company, provider: MockEnableBanking):
    load_rates(conn, FixtureNbpSource(FIXTURES / "reference" / "nbp_rates.json"), ["EUR", "USD"],
               date(2026, 9, 1), OCTOBER[1])
    results = ingest_company(conn, company, provider, *OCTOBER)
    return company, results


@pytest.fixture
def rows_of(conn: psycopg.Connection):
    """Current transactions of a company as dicts (with the account kind and IBAN)."""

    def fetch(company_id, where: str = "TRUE", params: tuple = ()) -> list[dict]:
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                f"""
                SELECT t.*, a.kind::text AS account_kind, a.iban AS account_iban
                  FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
                 WHERE t.company_id = %s AND t.superseded_by IS NULL AND ({where})
                 ORDER BY t.booking_date NULLS LAST, t.entry_reference
                """,
                (company_id, *params),
            ).fetchall()

    return fetch


@pytest.fixture
def cli_database_url(database_url: str):
    """A separate, migrated database for CLI runs that commit (dropped afterwards)."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from kasa.core import migrate

    name = conninfo_to_dict(database_url)["dbname"].removesuffix("_test") + "_bank_cli_test"
    url = make_conninfo(database_url, dbname=name)
    admin_url = make_conninfo(database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    migrate.migrate(url)
    yield url
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
