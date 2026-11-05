"""Gate 3 - semantics and control sets (docs/FAZY.md): no model call contains a surname, IBAN, phone or
e-mail from the fixtures; a sensitive word bypasses the model; the only way to a model is the gate.

The full October month goes through every channel with a provider that "leaves the machine"
(it records everything it is given): PDFs from e-mails, transfer titles, invoice lines, PM sheet rows.
What it saw is checked three times: the provider's own record, ``llm_call_payload`` and
``l1_semantics.input_pseudonymized``.
"""

from __future__ import annotations

import pytest

from kasa.core import control_sets as cs
from kasa.reference import ground_truth as reference
from kasa.core import pseudonym
from kasa.core.semantics.run import run_semantics
from tests.semantics.helpers import CountingMock, RecordingLlm, ground_truth, leaks, load_month, odm_company
from tests.semantics.test_gate import test_only_the_gate_calls_a_model as test_gate3_single_way_to_a_model  # noqa: F401


@pytest.fixture
def month(conn):
    company = odm_company(conn)
    llm = RecordingLlm()
    load_month(conn, company, llm=llm)                      # PDFs are read through the gate too
    report = run_semantics(conn, company, provider=llm)
    return company, llm, report


def test_gate3_no_personal_data_reaches_a_model(conn, month):
    company, llm, report = month
    truth = ground_truth()
    assert report.calls == 82 + 50 + 20 and len(llm.prompts) == 6 + 152
    found = {i: leaks(prompt, truth) for i, prompt in enumerate(llm.prompts)}
    assert not any(found.values()), {i: f for i, f in found.items() if f}
    payloads = conn.execute(
        """
        SELECT p.prompt_pseudonymized, p.response, l.purpose, l.model FROM llm_call_payload p
          JOIN llm_call_log l ON l.id = p.call_id WHERE p.company_id = %s
        """,
        (company.id,),
    ).fetchall()
    assert len(payloads) == len(llm.prompts)
    for prompt, response, purpose, model in payloads:
        assert not leaks(prompt, truth), (purpose, leaks(prompt, truth))
        if model == "mock-expected":
            continue          # the test double answers PDFs from fixtures/expected.json (plain values), not from input
        assert not leaks(response, truth), (purpose, leaks(response, truth))
    seen = conn.execute("SELECT input_pseudonymized FROM l1_semantics WHERE company_id = %s", (company.id,)).fetchall()
    assert len(seen) == 152 and not [s for (s,) in seen if leaks(s, truth)]
    # the raw facts still hold everything (only the model's view is reduced)
    raw = conn.execute("SELECT string_agg(title_raw, ' ') FROM l1_bank_transaction WHERE company_id = %s",
                       (company.id,)).fetchone()[0]
    assert leaks(raw, truth)
    # every call is logged with a digest only; blocked attempts have no digest at all
    log = conn.execute(
        "SELECT status::text, count(*), count(input_hash) FROM llm_call_log WHERE company_id = %s GROUP BY 1",
        (company.id,),
    ).fetchall()
    assert dict((s, (n, h)) for s, n, h in log) == {"ok": (158, 158), "blocked": (1, 0)}


def test_gate3_sensitive_word_bypasses_the_model(conn, month):
    company, llm, report = month
    [bailiff] = ground_truth()["pii"]["sensitive_transactions"]
    tx_id, title = conn.execute(
        "SELECT id, title_raw FROM l1_bank_transaction WHERE company_id = %s AND entry_reference = %s "
        "AND superseded_by IS NULL", (company.id, bailiff)).fetchone()
    assert report.blocked == {"sensitive": 1}
    assert not any("KM 1187/26" in p or "zajecie wynagrodzenia" in p for p in llm.prompts)
    assert conn.execute("SELECT count(*) FROM l1_semantics WHERE subject_id = %s", (tx_id,)).fetchone() == (0,)
    proposal = conn.execute(
        "SELECT proposal FROM l2_suggestion WHERE subject_id = %s AND kind = 'category'", (tx_id,)).fetchone()[0]
    assert proposal["needs_human"] is True and proposal["reason"] == "sensitive"
    assert "Komornik" in title                                   # the fact itself is complete


def test_gate3_broken_gate_means_no_call_even_for_the_mock(conn, monkeypatch):
    company = odm_company(conn)
    load_month(conn, company)
    monkeypatch.setattr(pseudonym, "pseudonymize_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    mock = CountingMock()
    report = run_semantics(conn, company, provider=mock)
    assert mock.calls == 0 and report.calls == 0 and report.blocked["gate_unavailable"] == 152


def test_gate3_control_set_from_the_fixtures_for_phase_4(conn):
    company = odm_company(conn)
    load_month(conn, company)
    info = reference.import_ground_truth(conn, company, kind="transaction", truth=ground_truth())
    exported = cs.export_set(conn, info.id)
    truth = {t["entry_reference"]: t["expected_status"] for t in ground_truth()["transactions"]}
    assert {t["entry_reference"]: t["expected_status"] for t in exported["transactions"]} == truth
