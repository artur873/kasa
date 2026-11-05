"""Fixtures of the phase 3 tests (every test runs in the rolled-back transaction of ``conn``)."""

from __future__ import annotations

import psycopg
import pytest

from kasa.core.companies import Company

from .helpers import load_month, odm_company


@pytest.fixture
def odm(conn: psycopg.Connection) -> Company:
    return odm_company(conn)


@pytest.fixture
def loaded(conn: psycopg.Connection, odm: Company) -> Company:
    """The whole October month of the fixtures, imported (PDFs read by the local mock through the gate)."""
    load_month(conn, odm)
    return odm
