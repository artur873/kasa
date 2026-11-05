"""Fixtures of the metric tests: the clicked pipeline database (built once per session), a rolled-back
transaction per test, and the agent's read-only session on it."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from kasa.core.companies import Company, get_company
from tests.metrics.helpers import clicked_database


@pytest.fixture(scope="session")
def clicked_url() -> str:
    return clicked_database()


@pytest.fixture
def mconn(clicked_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(clicked_url) as connection:
        with connection.transaction(force_rollback=True):
            yield connection


@pytest.fixture
def modm(mconn: psycopg.Connection) -> Company:
    return get_company(mconn, "odm")
