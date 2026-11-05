"""Fixtures of the phase 4B tests: the loaded October month, a rolled-back transaction per test."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from kasa.core.companies import Company, get_company

from .loaded_db import build_loaded_database


@pytest.fixture(scope="session")
def loaded_url() -> str:
    """The October fixtures with semantics, in their own database (built once per session)."""
    return build_loaded_database()


@pytest.fixture
def lconn(loaded_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(loaded_url) as connection:
        with connection.transaction(force_rollback=True):
            yield connection


@pytest.fixture
def odm(lconn: psycopg.Connection) -> Company:
    return get_company(lconn, "odm")
