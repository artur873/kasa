"""Database connection helpers (psycopg 3, no ORM)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql://kasa:kasa@localhost:5432/kasa"
DATABASE_URL_ENV = "KASA_DATABASE_URL"

# SQLSTATE codes raised by the guards defined in migrations/0001_core.sql.
APPEND_ONLY = "KS001"
DELETE_FORBIDDEN = "KS002"
HUMAN_DECISION_PROTECTED = "KS003"
RULE_NOT_AUTO = "KS004"
SUBJECT_MISSING = "KS005"
COMPANY_CHANGE_FORBIDDEN = "KS006"
ALLOCATION_TARGET_INVALID = "KS007"


def database_url() -> str:
    """Connection string from KASA_DATABASE_URL, or the local default."""
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def connect(url: str | None = None, *, autocommit: bool = False, dict_rows: bool = False) -> psycopg.Connection:
    """Open a new connection. The caller owns it (use as a context manager)."""
    kwargs = {"autocommit": autocommit, "application_name": "kasa"}
    if dict_rows:
        kwargs["row_factory"] = dict_row
    return psycopg.connect(url or database_url(), **kwargs)


@contextmanager
def transaction(conn: psycopg.Connection | None = None, *, url: str | None = None) -> Iterator[psycopg.Connection]:
    """Run a block in a transaction.

    With ``conn`` the block runs in a transaction (or a savepoint, if one is
    already open) on that connection. Without it a new connection is opened,
    committed on success, rolled back on error and closed.
    """
    if conn is not None:
        with conn.transaction():
            yield conn
        return
    with connect(url) as own:
        with own.transaction():
            yield own
