"""Gate of phase 2, bank part (docs/FAZY.md): the balance of every account closes to the grosz
on a full month of test data, split payments reconcile, no transaction is lost, and L0
stays append-only apart from the retention purge.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import date
from pathlib import Path

import psycopg
import pytest

from kasa.core.bank.ingest import ingest_company
from kasa.core.checks import check_balance_continuity, check_mpp
from kasa.core.companies import ensure_company
from kasa.core.db import APPEND_ONLY
from kasa.core.extension import get_extension
from kasa.core.normalize import to_minor
from kasa.core.providers.bank import MockEnableBanking

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "odm"
EB = FIXTURES / "bank" / "enablebanking"
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, sqlstate: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate == sqlstate, f"{excinfo.value.sqlstate}: {excinfo.value}"


@pytest.fixture
def ground_truth() -> dict:
    return json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture
def october(conn):
    company, _ = ensure_company(conn, get_extension("odm"))
    results = ingest_company(conn, company, MockEnableBanking(FIXTURES), *OCTOBER)
    return company, results


def test_gate2_bank_month_is_complete_and_closes_to_the_grosz(conn, october, ground_truth):
    company, results = october
    assert len(results) == 3
    counts = conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'booked'), count(*) FILTER (WHERE status = 'pending')
          FROM l1_bank_transaction WHERE company_id = %s AND superseded_by IS NULL
        """,
        (company.id,),
    ).fetchone()
    assert counts == (83, 4)

    # every booked transaction of the reference exists, with its amount and account
    stored = {
        row[0]: row[1:] for row in conn.execute(
            """
            SELECT t.entry_reference, a.iban, t.amount_minor, t.booking_date, t.op_type::text
              FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
             WHERE t.company_id = %s AND t.superseded_by IS NULL
            """,
            (company.id,),
        )
    }
    ibans = {a["slug"]: a["iban"] for a in ground_truth["accounts"]}
    for tx in ground_truth["transactions"]:
        iban, amount, booked_on, _op = stored[tx["entry_reference"]]
        assert (iban, amount, booked_on.isoformat()) == (
            ibans[tx["account"]], to_minor(tx["amount"]), tx["booking_date"]), tx["entry_reference"]
    for tx in ground_truth["pending_transactions"]:
        assert tx["entry_reference"] in stored

    # own transfers are op_type own (both legs)
    own = {t["entry_reference"] for t in ground_truth["transactions"] if t["expected_status"] == "own_transfer"}
    assert len(own) == 6
    assert {ref for ref, row in stored.items() if row[3] == "own"} == own

    # the balance of every account closes to the grosz, compared with balances.json
    checks = {c.scope: c for c in check_balance_continuity(conn, company.id, *OCTOBER)}
    for account in ground_truth["accounts"]:
        balances = {b["balance_type"]: to_minor(b["balance_amount"]["amount"]) for b in
                    json.loads((EB / account["slug"] / "balances.json").read_text(encoding="utf-8"))["balances"]}
        check = checks[account["iban"]]
        assert check.ok, check.detail
        assert check.detail["opening_minor"] == balances["OPBD"] == to_minor(account["opening_balance"])
        assert check.detail["closing_minor"] == balances["CLBD"] == to_minor(account["closing_balance"])
        assert check.detail["credits_minor"] == to_minor(account["sum_credits"])
        assert check.detail["debits_minor"] == to_minor(account["sum_debits"])
        assert check.detail["available_minor"] == balances["ITAV"] == to_minor(account["available_balance"])
        assert check.detail["mismatches"] == []
    recorded = conn.execute(
        "SELECT count(*) FILTER (WHERE ok), count(*) FROM truth_check_result WHERE company_id = %s "
        "AND check_name = 'balance_continuity'", (company.id,)).fetchone()
    assert recorded == (3, 3)


def test_gate2_bank_split_payment_reconciles(conn, october, ground_truth):
    company, _ = october
    mpp = check_mpp(conn, company.id, *OCTOBER)
    assert mpp.ok, mpp.detail
    expected = to_minor(ground_truth["checks"]["mpp"]["vat_blocks_in_incoming_titles"])
    assert mpp.detail["vat_blocks_minor"] == mpp.detail["vat_account_credits_minor"] == expected == 1518000
    assert mpp.detail["legs_without_group"] == []
    groups = conn.execute(
        """
        SELECT mpp_group_id, array_agg(entry_reference ORDER BY entry_reference)
          FROM l1_bank_transaction WHERE company_id = %s AND superseded_by IS NULL AND mpp_group_id IS NOT NULL
         GROUP BY 1
        """, (company.id,)).fetchall()
    assert sorted(refs for _g, refs in groups) == sorted(
        sorted(refs) for refs in ground_truth["checks"]["mpp"]["groups"].values())


def test_gate2_bank_raw_is_append_only_except_the_retention_purge(conn, october):
    company, _ = october
    raw_id, payload = conn.execute(
        """
        SELECT r.id, r.payload FROM l0_raw r
         WHERE r.company_id = %s AND r.channel = 'bank'
           AND EXISTS (SELECT 1 FROM l1_bank_transaction t WHERE t.raw_id = r.id)
         ORDER BY r.fetched_at, r.id LIMIT 1
        """, (company.id,)).fetchone()
    facts = conn.execute("SELECT count(*) FROM l1_bank_transaction WHERE raw_id = %s", (raw_id,)).fetchone()[0]
    assert facts > 0
    digest = hashlib.sha256(bytes(payload)).hexdigest()
    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET payload = ''::bytea WHERE id = %s", (raw_id,))

    log_id = conn.execute("SELECT kasa_purge_raw(%s, 'test retencji')", (raw_id,)).fetchone()[0]
    row = conn.execute("SELECT octet_length(payload), sha256, purged_at IS NOT NULL FROM l0_raw WHERE id = %s",
                       (raw_id,)).fetchone()
    assert row == (0, digest, True)
    log = conn.execute("SELECT raw_id, reason, payload_bytes, sha256 FROM l0_purge_log WHERE id = %s",
                       (log_id,)).fetchone()
    assert log == (raw_id, "test retencji", len(bytes(payload)), digest)
    # the facts parsed from the purged payload stay, still pointing at its (empty) record
    assert conn.execute("SELECT count(*) FROM l1_bank_transaction WHERE raw_id = %s", (raw_id,)).fetchone()[0] == facts

    with db_error(conn, APPEND_ONLY):
        conn.execute("UPDATE l0_raw SET meta = '{\"x\": 1}'::jsonb WHERE id = %s", (raw_id,))
    with db_error(conn, APPEND_ONLY):
        conn.execute("DELETE FROM l0_raw WHERE id = %s", (raw_id,))
