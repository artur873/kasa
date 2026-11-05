"""Fixtures of the queue tests: the whole pipeline once per session, a rolled-back transaction per test."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from kasa.core.companies import Company, get_company

from tests.queue.pipeline import pipeline_database


@pytest.fixture(scope="session")
def pipeline_url() -> str:
    return pipeline_database()


@pytest.fixture
def qconn(pipeline_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(pipeline_url) as connection:
        with connection.transaction(force_rollback=True):
            yield connection


@pytest.fixture
def odm(qconn: psycopg.Connection) -> Company:
    return get_company(qconn, "odm")
