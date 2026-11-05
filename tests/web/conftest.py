"""Panel tests: the app on the test's connection (rolled back after the test)."""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from kasa.core.companies import Company
from kasa.web.app import create_app
from tests.semantics.helpers import load_month, odm_company


@pytest.fixture
def odm(conn: psycopg.Connection) -> Company:
    return odm_company(conn)


@pytest.fixture
def loaded(conn: psycopg.Connection, odm: Company) -> Company:
    load_month(conn, odm)
    return odm


def app_on(conn: psycopg.Connection, **kwargs):
    @contextmanager
    def factory():
        with conn.transaction():          # a savepoint per request inside the test's transaction
            yield conn

    return create_app(connection_factory=factory, user="artur", **kwargs)


@pytest.fixture
def client(conn: psycopg.Connection, loaded: Company) -> TestClient:
    with TestClient(app_on(conn, company="odm")) as test_client:
        yield test_client
