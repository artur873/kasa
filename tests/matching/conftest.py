"""Fixtures of the matching tests.

The full October month of the fixtures (bank, KSeF, JPK, PM sheet, PDFs, title semantics of the
local model, ground-truth control set) is loaded ONCE into its own database ``kasa_matching_test``
and committed; every test works in a transaction of its own that is always rolled back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from kasa.core import migrate
from kasa.core.companies import Company, get_company
from kasa.core.providers.llm import MockLlm
from kasa.core.semantics.run import run_semantics
from kasa.reference import ground_truth as reference
from tests.conftest import database_url_for, drop_database, recreate_database
from tests.semantics.helpers import load_month, odm_company

MATCHING_DB = "kasa_matching_test"


@pytest.fixture(scope="session")
def matching_url() -> Iterator[str]:
    url = database_url_for(MATCHING_DB)
    recreate_database(url)
    migrate.migrate(url)
    with psycopg.connect(url) as conn:
        company = odm_company(conn)
        load_month(conn, company)
        run_semantics(conn, company, provider=MockLlm())
        reference.import_ground_truth(conn, company, kind="transaction")
        conn.commit()
    yield url
    drop_database(url)


@pytest.fixture
def mconn(matching_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(matching_url) as connection, connection.transaction(force_rollback=True):
        yield connection


@pytest.fixture
def odm(mconn: psycopg.Connection) -> Company:
    return get_company(mconn, "odm")


@pytest.fixture
def copy_url(matching_url: str) -> Iterator[str]:
    """A committed copy of the loaded month for tests that commit (CLI)."""
    url = database_url_for("kasa_matching_copy_test")
    drop_database(url)
    dbname = conninfo_to_dict(url)["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(dbname), sql.Identifier(MATCHING_DB)))
    yield url
    drop_database(url)


OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
