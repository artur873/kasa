"""Migration 0006: retention purge, counterparty function, KSeF number format, append-only check results."""

from __future__ import annotations

import contextlib
import hashlib

import psycopg
import pytest

from kasa.core import retention
from kasa.core.db import APPEND_ONLY, DELETE_FORBIDDEN
from kasa.core.pseudonym import pseudonym_for
from kasa.core.raw_store import store_raw

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
INVALID_PARAMETER = "22023"


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, *sqlstates: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate in sqlstates, f"{excinfo.value.sqlstate}: {excinfo.value}"


def test_purge_is_the_only_change_of_raw_data(conn, make):
    company = make.company()
    payload = b'{"transactions": []}'
    raw = store_raw(conn, company, "bank", payload, content_type="application/json", external_ref="eb:/x")

    # without the function nothing changes, not even with the flag set by hand
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET payload = ''::bytea, purged_at = now() WHERE id = %s", (raw.id,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("SET LOCAL kasa.purge = 'on'")
        conn.execute("UPDATE l0_raw SET payload = ''::bytea, purged_at = now() WHERE id = %s", (raw.id,))
    with db_error(conn, INVALID_PARAMETER):
        retention.purge_raw(conn, raw.id, "  ")

    log_id = retention.purge_raw(conn, raw.id, "koniec umowy", actor="artur")
    row = conn.execute("SELECT payload, sha256, purged_at FROM l0_raw WHERE id = %s", (raw.id,)).fetchone()
    assert bytes(row[0]) == b"" and row[1] == hashlib.sha256(payload).hexdigest() and row[2] is not None
    [entry] = retention.purge_log(conn, company)
    assert (entry.id, entry.raw_id, entry.sha256, entry.payload_bytes, entry.reason, entry.purged_by) == (
        log_id, raw.id, row[1], len(payload), "koniec umowy", "artur")
    assert retention.purge_raw(conn, raw.id, "jeszcze raz") == log_id          # idempotent

    # a purged record stays append-only and its log too
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET meta = '{}'::jsonb WHERE id = %s", (raw.id,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("DELETE FROM l0_raw WHERE id = %s", (raw.id,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_purge_log SET reason = 'x' WHERE id = %s", (log_id,))
    with db_error(conn, DELETE_FORBIDDEN):
        conn.execute("TRUNCATE l0_purge_log")
    # the digest keeps deduplication: the same bytes are not stored again
    again = store_raw(conn, company, "bank", payload, content_type="application/json")
    assert (again.id, again.created) == (raw.id, False)
    # a purged payload must be empty
    with db_error(conn, CHECK_VIOLATION):
        conn.execute(
            """
            INSERT INTO l0_raw (company_id, channel, sha256, content_type, payload, purged_at)
            VALUES (%s, 'file', %s, 'text/plain', 'x', now())
            """, (company, hashlib.sha256(b"x").hexdigest()))


def test_purge_company_and_shred_person_keys(conn, make):
    company = make.company()
    for n in range(3):
        store_raw(conn, company, "file", f"plik {n}".encode(), content_type="text/plain")
    assert len(retention.purge_company_raw(conn, company, "koniec umowy")) == 3
    assert retention.purge_company_raw(conn, company, "koniec umowy") == []
    pseudonym_for(conn, company, "person", "Anna Mazur")
    pseudonym_for(conn, company, "iban", "PL10114958040940956375786126")
    assert retention.shred_pseudonyms(conn, company) == 1
    assert conn.execute(
        "SELECT kind::text, value_encrypted IS NULL FROM pseudonym_key WHERE company_id = %s ORDER BY kind::text",
        (company,)).fetchall() == [("iban", False), ("person", True)]


def test_upsert_counterparty_finds_by_nip_iban_and_name(conn, make):
    company = make.company()

    def upsert(nip, name, iban, **named):
        extra = "".join(f", {k} => %({k})s" for k in named)
        return conn.execute(f"SELECT kasa_upsert_counterparty(%(c)s, %(nip)s, %(name)s, %(iban)s{extra})",
                            {"c": company, "nip": nip, "name": name, "iban": iban, **named}).fetchone()[0]

    acme = upsert("PL 951-612-79-75", "ACME Polska sp. z o.o.", None, p_source="ksef", p_confidence=1)
    assert upsert("9516127975", None, "PL19 1026 0283 8510 6583 1252 8272") == acme
    assert upsert(None, "ACME (bank)", "PL19102602838510658312528272") == acme       # by IBAN
    row = conn.execute("SELECT nip, name, ibans, kind::text, source, confidence FROM l1_counterparty WHERE id = %s",
                       (acme,)).fetchone()
    assert row == ("9516127975", "ACME Polska sp. z o.o.", ["PL19102602838510658312528272"], "company", "ksef", 1)

    bank_first = upsert(None, "Orbita Media sp. z o.o.", "PL97109891848649662922442446")
    assert upsert("7795524621", "Orbita Media sp. z o.o.", "PL97109891848649662922442446") == bank_first
    assert conn.execute("SELECT nip FROM l1_counterparty WHERE id = %s", (bank_first,)).fetchone() == ("7795524621",)

    merchant = upsert(None, "GOOGLE *WORKSPACE", None)
    assert upsert(None, "google  *workspace", None) == merchant                     # name key
    foreign = upsert("DE 123456789", "Helios Software GmbH", "DE35100900004953145714")
    assert conn.execute("SELECT nip, foreign_tax_id FROM l1_counterparty WHERE id = %s", (foreign,)).fetchone() == (
        None, "DE123456789")
    assert upsert(None, None, None) is None
    with db_error(conn, INVALID_PARAMETER):
        upsert("5342650779", "zła suma kontrolna", None)
    with db_error(conn, UNIQUE_VIOLATION):
        conn.execute(
            "INSERT INTO l1_counterparty (company_id, nip, kind, source, method, created_by) "
            "VALUES (%s, '9516127975', 'company', 't', 'code', 'pytest')", (company,))


def test_own_accounts_and_own_nip_resolve_to_the_company(conn, make):
    company = make.company()
    make.account(company, iban="PL40114020040000350277812345")
    early = conn.execute("SELECT kasa_upsert_counterparty(%s, '5342650778', 'Nasza firma', NULL)",
                         (company,)).fetchone()[0]
    own = conn.execute("SELECT kasa_upsert_counterparty(%s, NULL, NULL, 'PL40114020040000350277812345')",
                       (company,)).fetchone()[0]
    assert own != early
    # learning our NIP merges the row created before we knew it was us
    assert conn.execute("SELECT kasa_own_counterparty(%s, NULL, '5342650778')", (company,)).fetchone()[0] == own
    assert conn.execute("SELECT superseded_by FROM l1_counterparty WHERE id = %s", (early,)).fetchone() == (own,)
    assert conn.execute("SELECT kasa_upsert_counterparty(%s, '5342650778', NULL, NULL)",
                        (company,)).fetchone()[0] == own
    row = conn.execute("SELECT kind::text, nip, ibans FROM l1_counterparty WHERE id = %s", (own,)).fetchone()
    assert row == ("own", "5342650778", ["PL40114020040000350277812345"])
    with db_error(conn, INVALID_PARAMETER):
        conn.execute("SELECT kasa_own_counterparty(%s, NULL, '9516127975')", (company,))


def test_counterparty_columns_on_transactions_and_documents(conn, make):
    company = make.company()
    party = conn.execute("SELECT kasa_upsert_counterparty(%s, '9516127975', 'ACME', NULL)", (company,)).fetchone()[0]
    document = make.document(company)
    conn.execute("UPDATE l1_document SET counterparty_id = %s WHERE id = %s", (party, document))
    tx = make.transaction(company)
    conn.execute("UPDATE l1_bank_transaction SET counterparty_id = %s WHERE id = %s", (party, tx))
    other = make.company()
    foreign_party = conn.execute("SELECT kasa_upsert_counterparty(%s, '9516127975', 'ACME', NULL)",
                                 (other,)).fetchone()[0]
    with db_error(conn, "23503"):                                # never a party of another company
        conn.execute("UPDATE l1_document SET counterparty_id = %s WHERE id = %s", (foreign_party, document))


def test_ksef_number_format_and_split_roles(conn, make):
    company = make.company()
    make.document(company, ksef_number="5342650778-20261021-1D7921E70E4A-35")
    for bad in ("5342650778-20261021-1d7921e70e4a-35", "534265077-20261021-1D7921E70E4A-35", "FV 7/10/2026"):
        with db_error(conn, CHECK_VIOLATION):
            make.document(company, ksef_number=bad)
    tx = make.transaction(company)
    with db_error(conn, CHECK_VIOLATION):
        conn.execute("UPDATE l1_bank_transaction SET mpp_role = 'main' WHERE id = %s", (tx,))
    with db_error(conn, CHECK_VIOLATION):
        conn.execute("UPDATE l1_bank_transaction SET mpp_group_id = gen_random_uuid(), mpp_role = 'other' "
                     "WHERE id = %s", (tx,))


def test_truth_check_results_are_append_only(conn, make):
    company = make.company()
    rid = conn.execute(
        """
        INSERT INTO truth_check_result (company_id, check_name, period_start, period_end, ok)
        VALUES (%s, 'balance_continuity', '2026-10-01', '2026-10-31', true) RETURNING id
        """, (company,)).fetchone()[0]
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE truth_check_result SET ok = false WHERE id = %s", (rid,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("DELETE FROM truth_check_result WHERE id = %s", (rid,))
