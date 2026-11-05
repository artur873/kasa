"""Migration 0009 (matching) and the one exchange-rate function."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from kasa.core.db import APPEND_ONLY, HUMAN_DECISION_PROTECTED
from kasa.core.decisions import log_decision
from kasa.core.matching.fx import convert_minor, rate_for
from kasa.core.matching.weights import badge

BUILTIN = {"builtin:match_ksef_number", "builtin:match_mpp_block", "builtin:match_invoice_number_same_counterparty",
           "builtin:match_single_open_invoice_exact_amount", "builtin:own_transfer", "builtin:no_document_zus_tax_fee"}
CHECK_VIOLATION = "23514"


def test_every_new_company_gets_the_builtin_rules_by_the_briefing(conn, make):
    company = make.company()
    rows = conn.execute(
        """
        SELECT rule_key, stage::text, method::text, created_by, version, evidence ->> 'quote'
          FROM l3_rule WHERE company_id = %s AND rule_key LIKE 'builtin:match%%' OR (company_id = %s AND rule_key IN
               ('builtin:own_transfer', 'builtin:no_document_zus_tax_fee'))
        """,
        (company, company),
    ).fetchall()
    assert {r[0] for r in rows} == BUILTIN
    assert all(r[1:5] == ("auto", "human", "briefing-2026-11-03", 1) and r[5] for r in rows)
    # the owner may suspend a built-in rule; automation can never touch it (KS003)
    rule = conn.execute("SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = 'builtin:own_transfer'",
                        (company,)).fetchone()[0]
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'code' WHERE id = %s", (rule,))
    assert err.value.sqlstate == HUMAN_DECISION_PROTECTED
    # ... and a human change carries a new decision (KS003, review of phase 1, point 13)
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human' WHERE id = %s", (rule,))
    assert err.value.sqlstate == HUMAN_DECISION_PROTECTED
    decision = log_decision(conn, company_id=company, kind="rule", subject_type="rule", subject_id=rule,
                            decided_by="artur", source="test", payload={"action": "suspend"})
    conn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human', updated_by = 'artur', decision_id = %s "
                 "WHERE id = %s", (decision, rule))


def test_match_has_exactly_one_of_three_targets(conn, make):
    company = make.company()
    account = make.account(company)
    tx = make.transaction(company, account)
    other = make.transaction(company, account, amount_minor=45000)
    document = make.document(company)
    decision = make.decision(company, "transaction", tx, kind="match")
    insert = """
        INSERT INTO l3_match (company_id, transaction_id, document_id, related_transaction_id, amount_minor,
                              difference_kind, difference_minor, decision_id, source, method, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test', 'human', 'artur')
    """
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute(insert, (company, tx, document, other, 45000, None, None, decision))
    assert err.value.sqlstate == CHECK_VIOLATION
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute(insert, (company, tx, None, tx, 45000, None, None, decision))      # not itself
    assert err.value.sqlstate == CHECK_VIOLATION
    conn.execute(insert, (company, tx, None, other, 45000, "overpayment", 45000, decision))
    conn.execute(insert, (company, tx, document, None, -20000, "correction", -20000, decision))


def test_awaiting_until_calibration_and_card_holds(conn, make):
    company = make.company()
    tx = make.transaction(company)
    decision = make.decision(company, "transaction", tx, kind="match")
    conn.execute(
        """
        INSERT INTO l3_transaction_status (company_id, transaction_id, final_status, subtype, awaiting_until,
                                           decision_id, source, method, created_by)
        VALUES (%s, %s, 'no_document', 'advance', DATE '2027-04-05', %s, 'test', 'human', 'artur')
        """,
        (company, tx, decision),
    )
    # a booked operation is never "resolved" like a card hold
    other = make.transaction(company, conn.execute(
        "SELECT account_id FROM l1_bank_transaction WHERE id = %s", (tx,)).fetchone()[0], amount_minor=-1)
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute("UPDATE l1_bank_transaction SET resolved_by_transaction_id = %s WHERE id = %s", (other, tx))
    assert err.value.sqlstate == CHECK_VIOLATION
    labels = conn.execute(
        "INSERT INTO control_set (company_id, name, kind, size_target, created_by) VALUES (%s, 'x', 'transaction', 1, "
        "'t') RETURNING id", (company,)).fetchone()[0]
    row = conn.execute(
        """
        INSERT INTO calibration_result (run_id, company_id, control_set_id, params, metrics, chosen)
        VALUES (gen_random_uuid(), %s, %s, '{"temperature": 1}', '{}', true) RETURNING id
        """,
        (company, labels),
    ).fetchone()[0]
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute("UPDATE calibration_result SET chosen = false WHERE id = %s", (row,))
    assert err.value.sqlstate == APPEND_ONLY
    values = {r[0] for r in conn.execute("SELECT unnest(enum_range(NULL::bank_tx_status))::text").fetchall()}
    assert values == {"booked", "pending", "expired"}


def test_rate_for_is_the_last_nbp_table_before_the_date(conn):
    conn.execute(
        """
        INSERT INTO l1_fx_rate (company_id, rate_date, currency, rate, table_ref, source, method, created_by)
        VALUES (NULL, DATE '2031-01-09', 'EUR', 4.30, '5/A', 'nbp_a', 'code', 'test'),
               (NULL, DATE '2031-01-10', 'EUR', 4.31, '6/A', 'nbp_a', 'code', 'test')
        """
    )
    # Monday 13.01: the table of Friday 10.01; Friday itself: Thursday's table
    assert rate_for(conn, "EUR", date(2031, 1, 13)) == (Decimal("4.31"), date(2031, 1, 10))
    assert rate_for(conn, "eur", date(2031, 1, 10)) == (Decimal("4.30"), date(2031, 1, 9))
    assert rate_for(conn, "EUR", date(2031, 1, 9)) is None
    assert rate_for(conn, "PLN", date(2031, 1, 9)) == (Decimal(1), date(2031, 1, 9))
    assert convert_minor(9900, "EUR", Decimal("4.2512")) == 42087


def test_badge_rules_of_the_contract():
    assert badge(0.96, True) == "high" and badge(0.96, False) == "medium"
    assert badge(0.7, False) == "medium" and badge(0.69, True) == "low"
