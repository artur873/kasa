"""Shared fixtures.

The test database ``kasa_test`` is dropped, created and migrated once per
session. Each test gets a connection inside a transaction that is always
rolled back; use ``with conn.transaction():`` (a savepoint) around statements
that are expected to fail.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from kasa.core import migrate
from kasa.core.db import DATABASE_URL_ENV, DEFAULT_DATABASE_URL
from kasa.core.extension import EXTENSIONS_PATH_ENV

TEST_DB_NAME = "kasa_test"
TEST_DATABASE_URL_ENV = "KASA_TEST_DATABASE_URL"


def database_url_for(dbname: str = TEST_DB_NAME) -> str:
    explicit = os.environ.get(TEST_DATABASE_URL_ENV)
    if explicit and dbname == TEST_DB_NAME:
        return explicit
    base = explicit or os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL
    return make_conninfo(base, dbname=dbname)



def recreate_database(url: str) -> None:
    dbname = conninfo_to_dict(url)["dbname"]
    if not dbname.endswith("_test"):
        raise RuntimeError(f"refusing to drop a database that is not a test database: {dbname}")
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def drop_database(url: str) -> None:
    dbname = conninfo_to_dict(url)["dbname"]
    if not dbname.endswith("_test"):
        raise RuntimeError(f"refusing to drop a database that is not a test database: {dbname}")
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))


@pytest.fixture(scope="session", autouse=True)
def _isolate_environment() -> Iterator[None]:
    """Point every code path at the test database and only the built-in extensions."""
    url = database_url_for()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(DATABASE_URL_ENV, url)
        mp.delenv(EXTENSIONS_PATH_ENV, raising=False)
        yield


@pytest.fixture(scope="session")
def database_url() -> str:
    url = database_url_for()
    recreate_database(url)
    migrate.migrate(url)
    return url


@pytest.fixture
def scratch_database_url() -> Iterator[str]:
    """A fresh, empty database for tests that need their own (dropped afterwards)."""
    url = database_url_for("kasa_scratch_test")
    recreate_database(url)
    yield url
    drop_database(url)


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url) as connection:
        with connection.transaction(force_rollback=True):
            yield connection


class Factory:
    """Minimal valid rows for tests (provenance filled in)."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def _one(self, query: str, params: dict[str, Any]) -> Any:
        return self.conn.execute(query, params).fetchone()[0]

    def company(self, slug: str | None = None, extension_slug: str = "odm") -> uuid.UUID:
        slug = slug or f"co_{uuid.uuid4().hex[:8]}"
        return self._one(
            "INSERT INTO company (slug, name, extension_slug) VALUES (%(slug)s, %(slug)s, %(ext)s) RETURNING id",
            {"slug": slug, "ext": extension_slug},
        )

    def raw(self, company_id: uuid.UUID, payload: bytes | None = None, channel: str = "file") -> uuid.UUID:
        payload = payload if payload is not None else uuid.uuid4().bytes
        return self._one(
            """
            INSERT INTO l0_raw (company_id, channel, sha256, content_type, payload)
            VALUES (%(company_id)s, %(channel)s, %(sha)s, 'application/octet-stream', %(payload)s)
            RETURNING id
            """,
            {
                "company_id": company_id,
                "channel": channel,
                "sha": hashlib.sha256(payload).hexdigest(),
                "payload": payload,
            },
        )

    def account(self, company_id: uuid.UUID, iban: str = "PL61109010140000071219812874") -> uuid.UUID:
        return self._one(
            """
            INSERT INTO l1_bank_account (company_id, iban, currency, kind, source, method, created_by)
            VALUES (%(company_id)s, %(iban)s, 'PLN', 'current', 'test', 'code', 'pytest')
            RETURNING id
            """,
            {"company_id": company_id, "iban": iban},
        )

    def transaction(self, company_id: uuid.UUID, account_id: uuid.UUID | None = None,
                    amount_minor: int = -45000, fingerprint: str | None = None,
                    entry_reference: str | None = None) -> uuid.UUID:
        account_id = account_id or self.account(company_id)
        return self._one(
            """
            INSERT INTO l1_bank_transaction (
                company_id, account_id, entry_reference, fingerprint, status, booking_date,
                amount_minor, currency, title_raw, op_type, source, method, created_by)
            VALUES (%(company_id)s, %(account_id)s, %(entry_reference)s, %(fingerprint)s, 'booked', DATE '2026-03-12',
                    %(amount)s, 'PLN', 'BLIK 450', 'blik', 'test', 'code', 'pytest')
            RETURNING id
            """,
            {
                "company_id": company_id,
                "account_id": account_id,
                "entry_reference": entry_reference,
                "fingerprint": fingerprint or uuid.uuid4().hex,
                "amount": amount_minor,
            },
        )

    def document(self, company_id: uuid.UUID, ksef_number: str | None = None,
                 number_raw: str = "FV 7/10/2026") -> uuid.UUID:
        from kasa.core.normalize import normalize_invoice_number

        return self._one(
            """
            INSERT INTO l1_document (
                company_id, direction, ksef_number, number_raw, number_normalized, kind,
                issue_date, currency, gross_minor, source, method, created_by)
            VALUES (%(company_id)s, 'sales', %(ksef)s, %(raw)s, %(norm)s, 'vat',
                    DATE '2026-10-07', 'PLN', 1230000, 'test', 'code', 'pytest')
            RETURNING id
            """,
            {"company_id": company_id, "ksef": ksef_number, "raw": number_raw,
             "norm": normalize_invoice_number(number_raw)},
        )

    def rule(self, company_id: uuid.UUID, stage: str = "shadow", rule_key: str | None = None,
             version: int = 1) -> uuid.UUID:
        return self._one(
            """
            INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, source, method, created_by)
            VALUES (%(company_id)s, %(key)s, %(version)s, %(stage)s, %(pattern)s, %(action)s, 'test', 'human', 'artur')
            RETURNING id
            """,
            {
                "company_id": company_id,
                "key": rule_key or f"R-{uuid.uuid4().hex[:6]}",
                "version": version,
                "stage": stage,
                "pattern": Jsonb({"counterparty_iban": "PL61109010140000071219812874"}),
                "action": Jsonb({"category": "ZUS"}),
            },
        )

    def decision(self, company_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID,
                 kind: str = "allocation", method: str = "human", rule_id: uuid.UUID | None = None,
                 rule_version: int | None = None, reverts: uuid.UUID | None = None) -> uuid.UUID:
        return self._one(
            """
            INSERT INTO l3_decision_log (
                company_id, kind, subject_type, subject_id, rule_id, rule_version, decided_by,
                reverts_decision_id, source, method, created_by)
            VALUES (%(company_id)s, %(kind)s, %(subject_type)s, %(subject_id)s, %(rule_id)s, %(rule_version)s,
                    %(decided_by)s, %(reverts)s, 'test', %(method)s, 'pytest')
            RETURNING id
            """,
            {
                "company_id": company_id,
                "kind": kind,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "decided_by": "artur" if method == "human" else f"rule:{rule_id}",
                "reverts": reverts,
                "method": method,
            },
        )

    def unassigned(self, company_id: uuid.UUID) -> uuid.UUID:
        return self._one(
            "SELECT id FROM l3_cost_object WHERE company_id = %(c)s AND system_code = 'UNASSIGNED'",
            {"c": company_id},
        )

    def allocation(self, company_id: uuid.UUID, subject_id: uuid.UUID, cost_object_id: uuid.UUID,
                   decision_id: uuid.UUID, method: str = "human", rule_id: uuid.UUID | None = None,
                   rule_version: int | None = None, subject_type: str = "transaction",
                   share: str = "1") -> uuid.UUID:
        return self._one(
            """
            INSERT INTO l3_allocation (
                company_id, subject_type, subject_id, cost_object_id, share, decision_id,
                rule_id, rule_version, source, method, created_by)
            VALUES (%(company_id)s, %(subject_type)s, %(subject_id)s, %(cost_object_id)s, %(share)s,
                    %(decision_id)s, %(rule_id)s, %(rule_version)s, 'test', %(method)s, 'pytest')
            RETURNING id
            """,
            {
                "company_id": company_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "cost_object_id": cost_object_id,
                "share": share,
                "decision_id": decision_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "method": method,
            },
        )


@pytest.fixture
def make(conn: psycopg.Connection) -> Factory:
    return Factory(conn)

