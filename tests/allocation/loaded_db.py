"""A database with the whole October month of the ODM fixtures, loaded once per test session.

Every channel (bank, KSeF, JPK, PM sheet, PDFs), then the semantics of titles, lines and sheet rows
with the local mock model - the state the user gets after init-db, bank ingest, documents
ksef-sync / import / import-pdf and semantics run. Tests open a transaction on it that is always
rolled back.
"""

from __future__ import annotations

import psycopg
from psycopg.conninfo import conninfo_to_dict

from kasa.core import migrate
from kasa.core.providers.llm import MockLlm
from kasa.core.semantics.run import run_semantics
from tests.conftest import database_url_for, recreate_database
from tests.semantics.helpers import load_month, odm_company


def derived_name(suffix: str) -> str:
    """A database name next to the session's test database (kasa_test -> kasa_<suffix>_test), so that two
    sessions with different KASA_TEST_DATABASE_URL never drop each other's databases."""
    base = conninfo_to_dict(database_url_for())["dbname"]
    stem = base[: -len("_test")] if base.endswith("_test") else base
    return f"{stem}_{suffix}_test"


def build_loaded_database(dbname: str | None = None) -> str:
    url = database_url_for(dbname or derived_name("alloc"))
    recreate_database(url)
    migrate.migrate(url)
    with psycopg.connect(url) as conn:
        company = odm_company(conn)
        load_month(conn, company)
        run_semantics(conn, company, "all", provider=MockLlm())
        conn.commit()
    return url
