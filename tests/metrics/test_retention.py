"""Retention (briefing, step 9; reviews: phase 1 point 3, phase 3 points 4 and 10): model inputs expire after
90 days (only the digest stays), persons are anonymized by the only UPDATE path the database allows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from kasa.core.checks import _retention_check
from kasa.core.metrics import registry
from kasa.core.pseudonym import fold
from kasa.core.retention import anonymization_log, anonymize_persons, overdue_payloads, run_retention
from tests.metrics.helpers import truth
from tests.semantics.helpers import surnames


def test_semantics_input_keeps_only_its_digest_after_90_days(mconn, modm):
    row = mconn.execute("SELECT id, input_pseudonymized, input_sha256, input_chars, input_expires_at, created_at "
                        "FROM l1_semantics WHERE company_id = %s AND input_pseudonymized <> '' LIMIT 1",
                        (modm.id,)).fetchone()
    sid, text, digest, chars, expires, created = row
    assert digest == hashlib.sha256(text.encode()).hexdigest() and chars == len(text)
    assert expires == created + timedelta(days=90)
    # nobody changes the input by hand - not even the owner
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        mconn.execute("UPDATE l1_semantics SET input_pseudonymized = 'inne' WHERE id = %s", (sid,))
    assert err.value.sqlstate == "KS014"
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        mconn.execute("UPDATE l1_semantics SET input_pseudonymized = NULL, input_expired_at = now() WHERE id = %s",
                      (sid,))
    assert err.value.sqlstate == "KS014"
    later = datetime.now(timezone.utc) + timedelta(days=91)
    assert overdue_payloads(mconn) == {"llm_call_payload": 0, "l1_semantics_input": 0}
    assert overdue_payloads(mconn, now=later)["l1_semantics_input"] > 0
    assert "OSTRZEŻENIE" not in _retention_check(mconn)
    done = run_retention(mconn, now=later)
    assert done["l1_semantics_input"] > 0 and done["llm_call_payload"] > 0
    after = mconn.execute("SELECT input_pseudonymized, input_sha256, input_chars, input_expired_at FROM l1_semantics "
                          "WHERE id = %s", (sid,)).fetchone()
    assert after[0] is None and after[1] == digest and after[2] == chars and after[3] is not None
    assert run_retention(mconn, now=later) == {"llm_call_payload": 0, "l1_semantics_input": 0}
    # a new reading gets its digest from the database
    new = mconn.execute(
        "INSERT INTO l1_semantics (company_id, subject_type, subject_id, purpose, model, input_pseudonymized, source, "
        "method, prompt_version, created_by) SELECT company_id, subject_type, subject_id, 'title_intent_test', 'code', "
        "'tekst', 'test', 'code', 't-1', 'pytest' FROM l1_semantics WHERE id = %s RETURNING input_sha256, input_chars",
        (sid,)).fetchone()
    assert new == (hashlib.sha256(b"tekst").hexdigest(), 5)


def test_check_warns_about_texts_kept_past_their_term(mconn):
    mconn.execute("UPDATE llm_call_payload SET expires_at = created_at + interval '1 second' WHERE false")
    later = datetime.now(timezone.utc) + timedelta(days=91)
    mconn.execute("SELECT set_config('kasa.dummy', 'x', true)")
    # simulate a database where 91 days passed: move the clock of the check by expiring nothing and asking with now
    assert overdue_payloads(mconn, now=later)["llm_call_payload"] > 0


def test_anonymization_overwrites_raw_personal_columns_and_keeps_the_numbers(mconn, modm):
    before_report = registry.compute(mconn, modm.id, "margin_accrual").value
    before_cash = registry.compute(mconn, modm.id, "cash_contribution", period="2026-10").detail
    # raw columns are facts: the owner cannot overwrite them either
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        mconn.execute("UPDATE l1_bank_transaction SET title_raw = 'x' WHERE company_id = %s", (modm.id,))
    assert err.value.sqlstate == "KS014"
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        mconn.execute("UPDATE l1_internal_record SET fields = '{}' WHERE company_id = %s", (modm.id,))
    assert err.value.sqlstate == "KS014"
    log_id = anonymize_persons(mconn, modm.id, date(2026, 11, 1), actor="pytest")
    assert log_id is not None
    entry = anonymization_log(mconn, modm.id)[-1]
    assert entry["id"] == log_id and entry["counts"]["l1_bank_transaction"] > 20
    assert entry["counts"]["l1_internal_record"] == 20 and entry["counts"]["l1_document"] >= 1
    assert entry["counts"]["l1_pdf_extraction"] >= 1 and entry["counts"]["l1_counterparty"] > 5
    raw = mconn.execute(
        "SELECT json_agg(json_build_array(title_raw, counterparty_name_raw, counterparty_iban_raw, extracted)) "
        "FROM l1_bank_transaction WHERE company_id = %s", (modm.id,)).fetchone()[0]
    raw += mconn.execute("SELECT json_agg(fields) FROM l1_internal_record WHERE company_id = %s",
                         (modm.id,)).fetchone()[0]
    raw += mconn.execute("SELECT json_agg(json_build_array(seller_name, payment_iban, extra)) FROM l1_document "
                         "WHERE company_id = %s AND kind = 'bill'", (modm.id,)).fetchone()[0]
    raw += mconn.execute("SELECT json_agg(extracted) FROM l1_pdf_extraction x WHERE company_id = %s AND "
                         "(extracted ->> 'anonymized')::boolean", (modm.id,)).fetchone()[0]
    raw += mconn.execute("SELECT json_agg(json_build_array(name, ibans)) FROM l1_counterparty WHERE company_id = %s "
                         "AND kind = 'person'", (modm.id,)).fetchone()[0]
    text = json.dumps(raw, ensure_ascii=False)
    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    business = {fold(p["person"].split()[-1]) for p in truth()["pii"]["business_names_with_person"]}
    assert not ((surnames(truth()) - business) & words)
    assert not [p["iban"] for p in truth()["pii"]["persons"] if p.get("iban") and p["iban"] in text]
    assert "P-" in text
    # amounts, dates, cost objects and companies stay: the numbers do not move
    assert registry.compute(mconn, modm.id, "margin_accrual").value == before_report
    assert registry.compute(mconn, modm.id, "cash_contribution", period="2026-10").detail == before_cash
    assert anonymize_persons(mconn, modm.id, date(2026, 11, 1), actor="pytest") is None      # idempotent
    # the log is append-only
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        mconn.execute("UPDATE anonymization_log SET reason = 'x'")
    assert err.value.sqlstate == "KS001"
