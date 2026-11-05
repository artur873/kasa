"""The gate: sensitive words, pseudonymization, the second line of defence, the call log and payloads."""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from kasa.core import pseudonym
from kasa.core.retention import expire_llm_payloads
from kasa.core.semantics.gate import Blocked, SemanticsResult, guarded_complete, leak_scan
from kasa.core.semantics.run import run_semantics
from kasa.core.semantics.titles import TitleAnswer, read_title

from .helpers import CountingMock, RecordingLlm, ground_truth

REPO = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, sqlstate: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate == sqlstate, f"{excinfo.value.sqlstate}: {excinfo.value}"


def _tx(conn, company_id, title: str, *, sensitive: bool = False, amount: int = -45000):
    account = conn.execute(
        """
        INSERT INTO l1_bank_account (company_id, iban, currency, kind, source, method, created_by)
        VALUES (%s, 'PL61109010140000071219812874', 'PLN', 'current', 'test', 'code', 'pytest')
        ON CONFLICT DO NOTHING RETURNING id
        """,
        (company_id,),
    ).fetchone()
    account_id = account[0] if account else conn.execute(
        "SELECT id FROM l1_bank_account WHERE company_id = %s", (company_id,)).fetchone()[0]
    return conn.execute(
        """
        INSERT INTO l1_bank_transaction (company_id, account_id, fingerprint, status, booking_date, amount_minor,
                                         currency, title_raw, counterparty_name_raw, op_type, sensitive,
                                         source, method, created_by)
        VALUES (%s, %s, md5(random()::text), 'booked', DATE '2026-10-12', %s, 'PLN', %s, 'Jan Kowalski', 'transfer',
                %s, 'test', 'code', 'pytest')
        RETURNING id
        """,
        (company_id, account_id, amount, title, sensitive),
    ).fetchone()[0]


def _row(conn, tx_id) -> dict:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute(
            """SELECT id, title_raw, counterparty_name_raw, amount_minor, op_type::text AS op_type, sensitive,
                      extracted FROM l1_bank_transaction WHERE id = %s""", (tx_id,)).fetchone()


def _calls(conn, company_id, status: str | None = None) -> list[tuple]:
    return conn.execute(
        """
        SELECT status::text, blocked_reason, input_hash, input_chars, purpose FROM llm_call_log
         WHERE company_id = %s AND (%s::text IS NULL OR status::text = %s::text) ORDER BY created_at, id
        """,
        (company_id, status, status),
    ).fetchall()


def test_sensitive_title_bypasses_the_model_and_goes_to_the_queue(conn, odm):
    tx_id = _tx(conn, odm.id, "Komornik KM 1187/26 - zajecie wynagrodzenia dluznika Tomasz Grabowiecki za 09/2026")
    llm = RecordingLlm()
    outcome = read_title(conn, odm, _row(conn, tx_id), provider=llm)
    assert outcome.status == "blocked" and outcome.blocked.reason == "sensitive"
    assert llm.prompts == []                                                  # no call at all
    [suggestion] = conn.execute(
        "SELECT kind::text, proposal, method::text FROM l2_suggestion WHERE subject_id = %s", (tx_id,)).fetchall()
    assert suggestion[0] == "category" and suggestion[2] == "code"
    assert suggestion[1]["needs_human"] is True and suggestion[1]["reason"] == "sensitive"
    assert "enforcement" in suggestion[1]["categories"]
    assert _calls(conn, odm.id) == [("blocked", "sensitive", None, len(_row(conn, tx_id)["title_raw"])
                                     + len("Jan Kowalski"), "title_intent")]
    assert conn.execute("SELECT count(*) FROM llm_call_payload WHERE company_id = %s", (odm.id,)).fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM l1_semantics WHERE subject_id = %s", (tx_id,)).fetchone() == (0,)
    # running again: still no call, no second suggestion
    run_semantics(conn, odm, "transactions", provider=llm)
    assert llm.prompts == []
    assert conn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s", (tx_id,)).fetchone() == (1,)


def test_the_block_of_the_gate_is_a_lasting_flag_never_retried(conn, odm):
    """Review of 3/4B, point 13: a word the ingestion did not know (a person added it later) is found by the
    gate once; the flag (a category card) stays - later passes neither call nor log a blocked attempt - and
    matching treats the transfer as sensitive."""
    from kasa.core.matching.data import load_context
    from kasa.core.sensitive import add_human_term

    tx_id = _tx(conn, odm.id, "Mediacja rodzinna - sesja 4")
    add_human_term(conn, odm.id, "mediacja rodzinna", created_by="artur")
    llm = RecordingLlm()
    run_semantics(conn, odm, "transactions", provider=llm)
    blocked = [c for c in _calls(conn, odm.id) if c[1] == "sensitive"]
    assert llm.prompts == [] and len(blocked) == 1
    for _ in range(2):
        report = run_semantics(conn, odm, "transactions", provider=llm)
        assert report.blocked == {} or not report.blocked.get("sensitive")
    assert len([c for c in _calls(conn, odm.id) if c[1] == "sensitive"]) == 1       # not retried
    assert conn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s", (tx_id,)).fetchone() == (1,)
    tx = load_context(conn, odm.id).txs[tx_id]
    assert tx.sensitive and tx.intent is None and tx.semantics is None


def test_rows_flagged_sensitive_are_never_read(conn, odm):
    # the flag (set at ingestion, or by a person) is enough - the title is not even looked at
    tx_id = _tx(conn, odm.id, "Zaplata za FV 99/10/2026 - zupelnie zwykly tytul", sensitive=True)
    for provider in (RecordingLlm(), CountingMock()):
        outcome = read_title(conn, odm, _row(conn, tx_id), provider=provider)
        assert outcome.status == "blocked" and outcome.blocked.reason == "sensitive"
    report = run_semantics(conn, odm, "transactions", provider=RecordingLlm())
    assert report.calls == 0
    for (status, reason, input_hash, chars, _purpose) in _calls(conn, odm.id):
        assert (status, reason, input_hash, chars) == ("blocked", "sensitive", None, 0)   # 0 characters read


@pytest.mark.parametrize("failure", ["raises", "missing"])
def test_no_gate_no_call_even_for_the_local_mock(conn, odm, monkeypatch, failure):
    if failure == "raises":
        def broken(*args, **kwargs):
            raise pseudonym.PseudonymizationError("klucz niedostępny")
        monkeypatch.setattr(pseudonym, "pseudonymize_text", broken)
    else:
        monkeypatch.delattr(pseudonym, "pseudonymize_text")
    tx_id = _tx(conn, odm.id, "Zwrot kosztow dla Jan Kowalski, tel. 500 975 639")
    mock = CountingMock()
    outcome = read_title(conn, odm, _row(conn, tx_id), provider=mock)
    assert outcome.status == "blocked" and outcome.blocked.reason == "gate_unavailable"
    assert mock.calls == 0
    report = run_semantics(conn, odm, "transactions", provider=mock)
    assert mock.calls == 0 and report.calls == 0 and report.blocked["gate_unavailable"] == 1
    assert {c[:3] for c in _calls(conn, odm.id)} == {("blocked", "gate_unavailable", None)}
    assert conn.execute("SELECT count(*) FROM l1_semantics WHERE company_id = %s", (odm.id,)).fetchone() == (0,)


def test_second_line_of_defence_blocks_a_leaking_gate(conn, odm, monkeypatch):
    # a gate that "works" but leaves the data in place: the call must not happen anyway
    monkeypatch.setattr(pseudonym, "pseudonymize_text", lambda conn, company_id, text, **kw: (text, []))
    mock = CountingMock()
    for title in ("Zwrot dla klienta, tel. 500 975 639", "konto PL61 1090 1014 0000 0712 1981 2874",
                  "kontakt jan.kowalski@example.com", "PESEL 44051401359"):
        tx_id = _tx(conn, odm.id, title)
        outcome = read_title(conn, odm, _row(conn, tx_id), provider=mock)
        assert outcome.status == "blocked" and outcome.blocked.reason == "leak_suspected", title
    assert mock.calls == 0


def test_leak_scan_ignores_identifiers_of_invoices_and_amounts():
    clean = ("/VAT/12880,00/IDC/5342650778/INV/FV 3/10/2026/TXT/rozliczenie Gala ACME | 5342650778-20261012-"
             "32A55F0B257F-45 | F/40020041/10/26 | 14 078,40 zł | NR UNIK. EAB 1802447133 | 202610234002647079 "
             "| Karta 5375 **** **** 4417 | VAT: IE6392017LH | 08-10.10.2026 | P-7f3a T-0a1b I-99ff")
    assert leak_scan(clean) == []
    assert leak_scan("tel. 500 975 639") == ["phone"] and leak_scan("+48500975639") == ["phone"]
    assert leak_scan("PL28 2492 4115 2270 9072 8598 4411") == ["iban"]
    assert leak_scan("58 1021 0028 3340 9381 7629 1318") == ["iban"]
    assert leak_scan("a@b.pl") == ["email"] and leak_scan("44051401359") == ["pesel"]


def test_call_log_keeps_digests_and_the_payload_expires_after_90_days(conn, odm):
    tx_id = _tx(conn, odm.id, "Czynsz pazdziernik 2026 dla Jan Kowalski")
    outcome = guarded_complete(conn, odm, "title_intent", "Czynsz pazdziernik 2026 dla Jan Kowalski", TitleAnswer,
                               provider=RecordingLlm(), subject=("transaction", tx_id))
    assert isinstance(outcome, SemanticsResult) and outcome.data.intent == "rent"
    log = conn.execute(
        """
        SELECT l.status::text, l.input_hash, l.input_chars, l.output_hash, l.provider, l.model, l.prompt_version,
               l.leaves_machine, p.prompt_pseudonymized, p.response, p.expires_at - p.created_at
          FROM llm_call_log l JOIN llm_call_payload p ON p.call_id = l.id WHERE l.id = %s
        """,
        (outcome.call_id,),
    ).fetchone()
    status, input_hash, chars, output_hash, provider, model, version, leaves, prompt, response, keep = log
    assert (status, provider, model, version, leaves) == ("ok", "recording", "mock-heuristic", "v1", True)
    assert len(input_hash) == 64 and chars == len(prompt) and len(output_hash) == 64
    assert "Kowalski" not in prompt and "P-" in prompt and json.loads(response)["intent"] == "rent"
    assert keep == timedelta(days=90)
    # the log has no column with the text; payloads change only through the retention function
    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_call_log'")}
    assert not columns & {"prompt", "input", "text", "response", "prompt_pseudonymized"}
    with db_error(conn, "KS001"):
        conn.execute("UPDATE llm_call_log SET model = 'x' WHERE id = %s", (outcome.call_id,))
    with db_error(conn, "KS010"):
        conn.execute("UPDATE llm_call_payload SET prompt_pseudonymized = NULL WHERE call_id = %s", (outcome.call_id,))
    with db_error(conn, "KS010"):
        conn.execute("DELETE FROM llm_call_payload WHERE call_id = %s", (outcome.call_id,))
    assert expire_llm_payloads(conn) == 0                                   # not yet
    assert expire_llm_payloads(conn, now=datetime.now(timezone.utc) + timedelta(days=91)) >= 1
    after = conn.execute("SELECT prompt_pseudonymized, response, expired_at FROM llm_call_payload WHERE call_id = %s",
                         (outcome.call_id,)).fetchone()
    assert after[0] is None and after[1] is None and after[2] is not None
    assert conn.execute("SELECT input_hash FROM llm_call_log WHERE id = %s", (outcome.call_id,)).fetchone() == (
        input_hash,)


def test_unknown_purpose_is_refused(conn, odm):
    with pytest.raises(ValueError, match="nieznany cel"):
        guarded_complete(conn, odm, "approve_everything", "x", TitleAnswer, provider=CountingMock())


def test_the_bailiff_transfer_of_the_fixtures_never_reaches_a_model(conn, loaded):
    llm = RecordingLlm()
    report = run_semantics(conn, loaded, "transactions", provider=llm)
    [bailiff] = ground_truth()["pii"]["sensitive_transactions"]
    tx_id = conn.execute("SELECT id FROM l1_bank_transaction WHERE entry_reference = %s AND superseded_by IS NULL",
                         (bailiff,)).fetchone()[0]
    assert report.blocked == {"sensitive": 1}
    assert not any("KM 1187" in p or "1187/26" in p for p in llm.prompts)
    proposal = conn.execute("SELECT proposal FROM l2_suggestion WHERE subject_id = %s", (tx_id,)).fetchone()[0]
    assert proposal["needs_human"] is True and proposal["reason"] == "sensitive"


def test_only_the_gate_calls_a_model():
    """Grep: outside kasa/core/semantics/gate.py nobody calls provider.complete / extract_invoice."""
    offenders = []
    for path in (REPO / "kasa").rglob("*.py"):
        if path == REPO / "kasa" / "core" / "semantics" / "gate.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            if re.search(r"\.(?:complete|extract_invoice)\s*\(", code) and not re.search(r"\bdef\s", code):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []
    gate = (REPO / "kasa" / "core" / "semantics" / "gate.py").read_text(encoding="utf-8")
    assert "provider.complete(" in gate and "provider.extract_invoice(" in gate
    assert isinstance(Blocked("x"), Blocked)
