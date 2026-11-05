"""Safeguards in the database, not in the prompt (briefing, step 8): the roles of the agent.

The local role ``kasa`` is a superuser, so the tests switch with ``SET ROLE`` (``SET LOCAL ROLE`` inside the
rolled-back transaction) and check the refusals of the database itself.
"""

from __future__ import annotations

import json
import re
import uuid

import psycopg
import pytest

from kasa.core.metrics import registry
from tests.metrics.helpers import minor, truth
from tests.semantics.helpers import phones, surnames

INSUFFICIENT = "42501"


def as_role(conn: psycopg.Connection, role: str, company_id=None) -> None:
    conn.execute(f"SET LOCAL ROLE {role}")
    conn.execute("SET LOCAL search_path = evidence, views, metrics, public")
    if company_id is not None:
        conn.execute("SELECT set_config('kasa.company_id', %s, true)", (str(company_id),))


def refused(conn: psycopg.Connection, query: str, params=()) -> str:
    with pytest.raises(psycopg.Error) as err, conn.transaction():
        conn.execute(query, params)
    return err.value.sqlstate


def test_read_only_role_has_no_base_table_and_no_write(mconn, modm):
    as_role(mconn, "kasa_agent_ro", modm.id)
    assert mconn.execute("SELECT current_user").fetchone()[0] == "kasa_agent_ro"
    for table in ("l1_bank_transaction", "l1_document", "l3_match", "pseudonym_key", "llm_call_payload",
                  "l1_pdf_extraction", "agent_call_log", "metric_proposal"):
        assert refused(mconn, f"SELECT 1 FROM public.{table} LIMIT 1") == INSUFFICIENT, table
    company = str(modm.id)
    assert refused(mconn, "INSERT INTO public.l2_suggestion (company_id, kind, proposal, source, method, created_by) "
                          "VALUES (%s, 'rule_candidate', '{}', 'x', 'code', 'x')", (company,)) == INSUFFICIENT
    assert refused(mconn, "INSERT INTO public.metric_proposal (company_id, question, proposed_by) VALUES "
                          "(%s, 'x', 'agent')", (company,)) == INSUFFICIENT
    assert refused(mconn, "INSERT INTO metrics.metric_definition (key, version, name_pl, description_pl, grain, unit, "
                          "implementation, code_sha256) VALUES ('x', 1, 'x', 'x', 'company', 'count', 'python:x', "
                          "repeat('0', 64))") == INSUFFICIENT
    assert refused(mconn, "UPDATE evidence.l1_bank_transaction SET amount_minor = 0") in (INSUFFICIENT, "55000",
                                                                                            "0A000")
    for call in ("SELECT kasa_purge_raw(gen_random_uuid(), 'x')", "SELECT kasa_expire_llm_payloads()",
                 "SELECT kasa_expire_semantics_inputs()", "SELECT kasa_refresh_agent_views()",
                 "SELECT kasa_anonymize_persons(gen_random_uuid(), current_date)"):
        assert refused(mconn, call) == INSUFFICIENT, call


def test_read_only_role_sees_one_company_through_the_mirrors(mconn, modm):
    total = mconn.execute("SELECT count(*) FROM l1_bank_transaction WHERE company_id = %s", (modm.id,)).fetchone()[0]
    other = mconn.execute("INSERT INTO company (slug, name, extension_slug) VALUES ('obca', 'Obca', 'odm') "
                          "RETURNING id").fetchone()[0]
    as_role(mconn, "kasa_agent_ro")
    assert mconn.execute("SELECT count(*) FROM l1_bank_transaction").fetchone()[0] == 0     # no company - nothing
    mconn.execute("SELECT set_config('kasa.company_id', %s, true)", (str(modm.id),))
    assert mconn.execute("SELECT count(*) FROM l1_bank_transaction").fetchone()[0] == total
    assert mconn.execute("SELECT count(*) FROM evidence.company").fetchone()[0] == 1
    mconn.execute("SELECT set_config('kasa.company_id', %s, true)", (str(other),))
    assert mconn.execute("SELECT count(*) FROM l1_bank_transaction").fetchone()[0] == 0
    assert mconn.execute("SELECT count(*) FROM views.transactions").fetchone()[0] == 0


def test_titles_come_back_as_untrusted_text_never_title_raw(mconn, modm):
    as_role(mconn, "kasa_agent_ro", modm.id)
    row = mconn.execute("SELECT title, title ->> 'untrusted_text' FROM views.transactions "
                        "WHERE entry_reference = '202610275984818978'").fetchone()
    assert set(row[0]) == {"untrusted_text"} and "FV 7/10/2026" in row[1]
    assert refused(mconn, "SELECT title_raw FROM views.transactions") == "42703"
    assert refused(mconn, "SELECT counterparty_name_raw FROM views.transactions") == "42703"


def test_no_raw_personal_data_reaches_the_schemas_of_the_agent(mconn, modm):
    """Surnames, phones and IBANs of the persons of the fixtures appear nowhere in evidence / views / metrics."""
    relations = mconn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema IN "
        "('evidence', 'views', 'metrics') ORDER BY 1, 2").fetchall()
    as_role(mconn, "kasa_agent_ro", modm.id)
    dump = []
    for schema, name in relations:
        rows = mconn.execute(f'SELECT to_jsonb(x) FROM "{schema}"."{name}" x').fetchall()
        dump += [r[0] for r in rows]
    assert len(dump) > 500
    text = json.dumps(dump, ensure_ascii=False)
    from kasa.core.pseudonym import fold

    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    assert not (surnames(truth()) & words)
    digits = re.sub(r"\D", "", text)
    assert not [p for p in phones(truth()) if p in digits]
    assert not [p["iban"] for p in truth()["pii"]["persons"] if p.get("iban") and p["iban"] in text]


def test_no_view_exposes_pay_per_person(mconn, modm):
    definitions = mconn.execute(
        "SELECT table_name, pg_get_viewdef(format('%I.%I', table_schema, table_name)::regclass) FROM "
        "information_schema.views WHERE table_schema IN ('views', 'evidence')").fetchall()
    for name, definition in definitions:
        assert not re.search(r"salar|wynagrodz|payroll|pensj", name, re.I), name
        if re.search(r"l1_bank_transaction", definition) and name in ("transactions", "l1_bank_transaction"):
            assert "kasa_tx_is_payroll" in definition, name
    salaries = [t["entry_reference"] for t in truth()["transactions"] if t.get("no_document_category") == "Wynagrodzenia"]
    as_role(mconn, "kasa_agent_ro", modm.id)
    assert mconn.execute("SELECT count(*) FROM views.transactions WHERE entry_reference = ANY(%s)",
                         (salaries,)).fetchone()[0] == 0
    masked = mconn.execute("SELECT counterparty_id, counterparty_name_raw, counterparty_pseudonym, title_raw, "
                           "counterparty_iban_raw, amount_minor FROM l1_bank_transaction WHERE entry_reference = ANY(%s)",
                           (salaries,)).fetchall()
    assert len(masked) == len(salaries)
    assert all(r[:5] == (None, None, None, None, None) for r in masked)      # an amount, never a person
    assert sorted(r[5] for r in masked) == sorted(minor(t["amount"]) for t in truth()["transactions"]
                                                  if t["entry_reference"] in salaries)


def test_suggestion_role_only_appends(mconn, modm):
    tx = mconn.execute("SELECT id FROM l1_bank_transaction WHERE entry_reference = '202610275984818978'").fetchone()[0]
    mconn.execute("SET LOCAL ROLE kasa_agent_suggest")
    mconn.execute(
        "INSERT INTO l2_suggestion (id, company_id, kind, subject_type, subject_id, proposal, source, method, "
        "prompt_version, created_by) VALUES (%s, %s, 'category', 'transaction', %s, '{\"category\": \"Inne\"}', "
        "'agent', 'model', 'test-1', 'agent:test')", (uuid.uuid4(), modm.id, tx))
    mconn.execute("INSERT INTO agent_call_log (company_id, tool, status) VALUES (%s, 'get_metric', 'ok')", (modm.id,))
    assert refused(mconn, "SELECT 1 FROM l2_suggestion LIMIT 1") == INSUFFICIENT
    assert refused(mconn, "SELECT 1 FROM l1_bank_transaction LIMIT 1") == INSUFFICIENT
    assert refused(mconn, "INSERT INTO l3_decision_log (company_id, kind, decided_by, source, method, created_by) "
                          "VALUES (%s, 'match', 'x', 'x', 'human', 'x')", (modm.id,)) == INSUFFICIENT
    assert refused(mconn, "UPDATE l2_suggestion SET status = 'accepted'") == INSUFFICIENT
    assert refused(mconn, "SELECT kasa_purge_raw(gen_random_uuid(), 'x')") == INSUFFICIENT


def test_role_settings_timeout_and_read_only(mconn):
    settings = dict(mconn.execute(
        "SELECT r.rolname, s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
        "WHERE r.rolname IN ('kasa_agent_ro', 'kasa_agent_suggest')").fetchall())
    assert "statement_timeout=15s" in settings["kasa_agent_ro"]
    assert "default_transaction_read_only=on" in settings["kasa_agent_ro"]
    assert "statement_timeout=15s" in settings["kasa_agent_suggest"]


def test_the_agent_session_is_the_read_only_role_with_the_timeout(clicked_url):
    from kasa.core.agent.db import AgentDatabase

    db = AgentDatabase(url=clicked_url, company_slug="odm")
    with db.ro() as conn:
        assert conn.execute("SELECT current_user, current_setting('statement_timeout'), "
                            "current_setting('transaction_read_only')").fetchone() == ("kasa_agent_ro", "15s", "on")
        result = registry.compute(conn, db.company_id, "receivables_open", as_of="2026-10-31")
        assert result.value == minor(truth()["receivables_open"]["total"])
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT * FROM public.l1_document LIMIT 1")
    with db.suggest() as conn:
        assert conn.execute("SELECT current_user, current_setting('statement_timeout')").fetchone() == (
            "kasa_agent_suggest", "15s")


def test_suggestion_role_writes_only_weak_proposals_and_the_automation_skips_them(mconn, modm):
    """Review of 4C/5, point 7: the role could insert hard = true / score 1 / rank 1 / accepted - the database
    turns every row of the agent into a weak open proposal of a model, and the automation skips source 'agent'."""
    from kasa.core.allocation.auto import run_auto

    subject_type, subject_id = mconn.execute(
        "SELECT subject_type::text, subject_id FROM l2_suggestion WHERE company_id = %s AND kind = 'allocation' "
        "AND status = 'open' AND subject_id IS NOT NULL LIMIT 1", (modm.id,)).fetchone()
    target = mconn.execute("SELECT id FROM l3_cost_object WHERE company_id = %s AND status = 'approved' "
                           "AND system_code IS NULL LIMIT 1", (modm.id,)).fetchone()[0]
    rule = mconn.execute("SELECT id, version FROM l3_rule WHERE company_id = %s AND stage = 'auto' LIMIT 1",
                         (modm.id,)).fetchone()
    sid = uuid.uuid4()
    mconn.execute("SET LOCAL ROLE kasa_agent_suggest")
    mconn.execute(
        """
        INSERT INTO l2_suggestion (id, company_id, kind, subject_type, subject_id, proposal, score, rank, status,
                                   resolved_at, source, method, confidence, evidence, prompt_version, created_by)
        VALUES (%s, %s, 'allocation', %s, %s, %s, 1.0, 1, 'accepted', now(), 'kasa.allocation', 'code', 1.0, %s,
                'agent-v1', 'agent:test')
        """,
        (sid, modm.id, subject_type, subject_id,
         json.dumps({"shares": [{"cost_object_id": str(target), "share": 1, "share_exact": "1"}]}),
         json.dumps({"hard": True, "auto_eligible": True, "badge": "high", "precedents": 99, "tags": ["ksef_number"],
                     "rule": {"rule_id": str(rule[0]), "version": rule[1], "rule_key": "R-x"}, "facts": ["x"]})))
    mconn.execute("RESET ROLE")
    row = mconn.execute("SELECT source, method::text, status::text, rank, score, confidence, evidence, resolved_at "
                        "FROM l2_suggestion WHERE id = %s", (sid,)).fetchone()
    source, method, status, rank, score, confidence, evidence, resolved = row
    assert (source, method, status, rank, resolved) == ("agent", "model", "open", None, None)
    assert float(score) <= 0.9 and float(confidence) <= 0.9
    assert evidence["hard"] is False and evidence["auto_eligible"] is False and "rule" not in evidence
    assert evidence["badge"] == "low" and evidence["precedents"] == 0 and evidence["tags"] == ["agent"]
    assert evidence["facts"] == ["x"]                                   # the rest of the proposal stays
    # even if a bug of the server put it at rank 1 with auto_eligible (as the owner): the automation skips it
    mconn.execute("UPDATE l2_suggestion SET rank = 1, evidence = evidence || '{\"auto_eligible\": true}' "
                  "WHERE id = %s", (sid,))
    report = run_auto(mconn, modm)
    assert str(sid) not in {f["suggestion_id"] for f in report.failed}
    assert mconn.execute("SELECT status::text FROM l2_suggestion WHERE id = %s", (sid,)).fetchone() == ("open",)
    assert mconn.execute("SELECT count(*) FROM l3_allocation WHERE subject_id = %s AND status = 'active'",
                         (subject_id,)).fetchone() == (0,)


def test_the_agent_session_sets_the_timeout_in_every_transaction(clicked_url):
    """Review of 4C/5, point 13: ``ALTER ROLE ... SET statement_timeout`` does not apply after ``SET ROLE`` -
    every transaction of the agent starts with ``SET LOCAL statement_timeout = '15s'``."""
    import time

    from kasa.core.agent.db import AgentDatabase

    class NoSessionLimit(AgentDatabase):
        """A session where the limit of the role / of the session does not hold (as after SET ROLE)."""

        def _session(self, url, role, search_path):
            conn = super()._session(url, role, search_path)
            conn.autocommit = True
            conn.execute("SET statement_timeout = 0")
            conn.autocommit = False
            return conn

    db = NoSessionLimit(url=clicked_url, company_slug="odm")
    with db.suggest() as conn:
        assert conn.execute("SELECT current_setting('statement_timeout')").fetchone() == ("15s",)
    with db.ro() as conn:
        # the transaction's own SET LOCAL holds
        assert conn.execute("SELECT current_user, current_setting('statement_timeout')").fetchone() == (
            "kasa_agent_ro", "15s")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(20)")
        assert time.monotonic() - started < 20
