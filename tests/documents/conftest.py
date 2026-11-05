"""Fixtures of the documents channel: an isolated ODM company and fixture-file helpers.

Every test runs inside the rolled-back transaction of ``conn`` (tests/conftest.py).
The company gets a fresh id (so nothing committed by other tests interferes) but
the slug ``odm``, which is what the mock providers use to find ``fixtures/odm/``.
"""

from __future__ import annotations

import psycopg
import pytest

from kasa.core.companies import Company
from kasa.core.documents.ingest import capabilities

from .helpers import (  # noqa: F401 - re-exported for the test modules
    FIXTURES,
    ODM_NIP,
    REPO,
    current_documents,
    expected_pdfs,
    ground_truth,
    ksef_index,
    load_nbp_rates,
    observations_of,
    row_counts,
)


@pytest.fixture
def odm(conn: psycopg.Connection, make) -> Company:
    company_id = make.company(extension_slug="odm")
    conn.execute("UPDATE company SET name = 'ODM Partners' WHERE id = %s", (company_id,))
    return Company(id=company_id, slug="odm", name="ODM Partners", extension_slug="odm")


@pytest.fixture
def with_0006(conn: psycopg.Connection) -> bool:
    """True when migration 0006 (counterparties, pseudonym keys) is applied."""
    return capabilities(conn).counterparties


@pytest.fixture
def nbp(conn: psycopg.Connection) -> None:
    load_nbp_rates(conn)
