"""Fixes of the review of phases 4A/4B (docs/PRZEGLADY.md, "Fazy 4A i 4B"): points 2, 4, 6, 7, 12, 13, 15."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest

from kasa.core.allocation.amounts import load_documents, load_internal_records
from kasa.core.allocation.subjects import evidence_of_documents, load_matches
from kasa.core.bank.extract import extract_title
from kasa.core.checks import check_match_coverage
from kasa.core.decisions import log_decision, revoke_decision
from kasa.core.matching import auto
from kasa.core.matching import weights as W
from kasa.core.matching.data import load_context
from kasa.core.matching.decide import MatchDecisionError, apply_match
from kasa.core.matching.engine import evaluate
from kasa.core.matching.run import run_matching
from kasa.core.queue import build

from .conftest import OCTOBER
from .helpers import add_doc, add_tx, only, party, result_for, tx_id

NIP = "5260001246"
IBAN = "PL27114020040000300201355387"
NAME = "Dostawca Testowy sp. z o.o."


def _open_rank1(conn, transaction_id):
    return conn.execute(
        "SELECT id, proposal, evidence FROM l2_suggestion WHERE subject_id = %s AND kind = 'match' AND status = 'open' "
        "AND rank = 1", (transaction_id,)).fetchone()


def _status(conn, transaction_id):
    return conn.execute("SELECT final_status::text, method::text FROM l3_transaction_status WHERE transaction_id = %s "
                        "AND status = 'active'", (transaction_id,)).fetchone()


# ---------------------------------------------------------------------------
# point 2: hard evidence from an invoice number only with NIP / IBAN
# ---------------------------------------------------------------------------


def test_same_name_is_a_point_signal_and_a_persons_match_links_the_iban(mconn, odm):
    # a sales invoice knows the buyer by NIP, the bank knows the payer by name and IBAN: two counterparties
    buyer = party(mconn, odm.id, "Klient Testowy sp. z o.o.", NIP, None)
    payer_iban = "PL61109010140000071219812874"
    bank_party = party(mconn, odm.id, "KLIENT TESTOWY SP. Z O.O.", None, payer_iban)
    assert bank_party != buyer
    first = add_doc(mconn, odm.id, "FS/1/2026", 246000, direction="sales", counterparty_id=buyer, nip=NIP,
                    name="Klient Testowy sp. z o.o.")
    tx1 = add_tx(mconn, odm.id, 246000, "Zaplata FS/1/2026", counterparty_id=bank_party,
                 name="KLIENT TESTOWY SP. Z O.O.", iban=payer_iban)
    best = only(mconn, odm.id, tx1).best
    assert best.items[0].document_id == first and best.score > 0.99
    assert not best.hard and "counterparty_name" in best.tags and "counterparty_name_only" in best.tags
    assert not auto.eligibility(best, {"auto_threshold": 0.0, "margin_min": 0.0}, {})[0]
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, tx1) is None                                    # the queue, not the automation
    # the person's first match ties the payer's IBAN to the counterparty with the NIP
    decision = apply_match(mconn, _open_rank1(mconn, tx1)[0], decided_by="artur")
    linked = mconn.execute("SELECT evidence -> 'iban_linked' FROM l3_decision_log WHERE id = %s",
                           (decision,)).fetchone()[0]
    assert linked["iban"] == payer_iban and linked["nip"] == NIP
    assert payer_iban in mconn.execute("SELECT ibans FROM l1_counterparty WHERE id = %s", (buyer,)).fetchone()[0]
    # ...and from then on an invoice number from that IBAN is hard evidence
    second = add_doc(mconn, odm.id, "FS/2/2026", 123000, direction="sales", counterparty_id=buyer, nip=NIP,
                     name="Klient Testowy sp. z o.o.", issue=date(2026, 10, 15), due=date(2026, 10, 29))
    tx2 = add_tx(mconn, odm.id, 123000, "Zaplata FS/2/2026", counterparty_id=bank_party,
                 name="KLIENT TESTOWY SP. Z O.O.", iban=payer_iban, day=date(2026, 10, 27))
    best = only(mconn, odm.id, tx2).best
    assert best.items[0].document_id == second and best.hard
    assert best.rule_key == "builtin:match_invoice_number_same_counterparty" and "counterparty_strong" in best.tags
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, tx2) == ("matched", "rule")


def test_no_iban_link_through_an_operator_or_another_name(mconn, odm):
    buyer = party(mconn, odm.id, "Klient Testowy sp. z o.o.", NIP, None)
    add_doc(mconn, odm.id, "FS/3/2026", 50000, direction="sales", counterparty_id=buyer, nip=NIP,
            name="Klient Testowy sp. z o.o.")
    revolut = party(mconn, odm.id, "Revolut Bank UAB", None, "LT063250066711277862")
    title = "KLIENT TESTOWY SP. Z O.O. FS/3/2026"
    tx = add_tx(mconn, odm.id, 50000, title, counterparty_id=revolut, name="Revolut Bank UAB",
                iban="LT063250066711277862",
                extracted=extract_title([title], reference_date=date(2026, 10, 21),
                                        counterparty_name="Revolut Bank UAB"))
    run_matching(mconn, odm, period=OCTOBER)
    decision = apply_match(mconn, _open_rank1(mconn, tx)[0], decided_by="artur")
    assert mconn.execute("SELECT evidence ? 'iban_linked' FROM l3_decision_log WHERE id = %s",
                         (decision,)).fetchone()[0] is False
    assert mconn.execute("SELECT ibans FROM l1_counterparty WHERE id = %s", (buyer,)).fetchone()[0] == []


def test_a_counterparty_joined_only_by_name_is_not_an_identifier(mconn, odm):
    # a PDF bill without NIP and IBAN joins the bank's counterparty by name: the same id, not hard evidence
    who = party(mconn, odm.id, "Studio Bez Nipu", None, IBAN)
    assert party(mconn, odm.id, "Studio Bez Nipu", None, None) == who
    add_doc(mconn, odm.id, "R/9/2026", 70000, counterparty_id=who, name="Studio Bez Nipu")
    tx = add_tx(mconn, odm.id, -70000, "R/9/2026", counterparty_id=who, name="Studio Bez Nipu", iban=IBAN)
    best = only(mconn, odm.id, tx).best
    assert "counterparty_strong" not in best.tags and "counterparty_name" in best.tags and not best.hard
    # the IBAN of the bill itself is an identifier
    add_doc(mconn, odm.id, "R/10/2026", 80000, counterparty_id=who, name="Studio Bez Nipu", payment_iban=IBAN)
    tx = add_tx(mconn, odm.id, -80000, "R/10/2026", counterparty_id=who, name="Studio Bez Nipu", iban=IBAN)
    best = only(mconn, odm.id, tx).best
    assert "counterparty_strong" in best.tags and best.hard


# ---------------------------------------------------------------------------
# point 4: only what the automation will close reserves an invoice
# ---------------------------------------------------------------------------


@pytest.fixture
def two_payments(mconn, odm):
    supplier = party(mconn, odm.id, NAME, NIP, IBAN)
    invoice = add_doc(mconn, odm.id, "R/1/2026", 50000, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    a = add_tx(mconn, odm.id, -50000, "R/1/2026", counterparty_id=supplier, name=NAME, iban=IBAN,
               day=date(2026, 10, 21))
    b = add_tx(mconn, odm.id, -50000, "druga wplata R/1/2026", counterparty_id=supplier, name=NAME, iban=IBAN,
               day=date(2026, 10, 22))
    return invoice, a, b


def _suspend(conn, odm, rule_key):
    rule = load_context(conn, odm.id).builtin_rules[rule_key]
    decision = log_decision(conn, company_id=odm.id, kind="rule", subject_type="rule", subject_id=rule["id"],
                            decided_by="artur", source="test", payload={"action": "suspend"})
    conn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human', updated_by = 'artur', decision_id = %s "
                 "WHERE id = %s", (decision, rule["id"]))


def test_a_hard_candidate_the_automation_will_not_close_reserves_nothing(mconn, odm, two_payments):
    invoice, a, b = two_payments
    ev = evaluate(mconn, odm.id, subject_ids={a, b})
    first, second = (next(r for r in ev.results if r.tx.id == t) for t in (a, b))
    assert first.best.hard and a in ev.reserved               # eligible: it claims the invoice
    assert second.best.items[0].difference_kind == "overpayment"
    _suspend(mconn, odm, "builtin:match_invoice_number_same_counterparty")
    ev = evaluate(mconn, odm.id, subject_ids={a, b})
    first, second = (next(r for r in ev.results if r.tx.id == t) for t in (a, b))
    assert first.best.hard and not ev.reserved                # hard, but no rule in stage auto: no claim
    assert second.best.items[0].document_id == invoice and second.best.items[0].difference_kind is None
    # without the automation nothing is reserved either
    _ = evaluate(mconn, odm.id, subject_ids={a, b}, reserve=False)
    assert not _.reserved


def test_a_reservation_refused_by_the_automation_is_recomputed(mconn, odm, two_payments, monkeypatch):
    invoice, a, b = two_payments

    def refuse(*args, **kwargs):
        raise MatchDecisionError("automat: sugestia nieaktualna (test)")

    monkeypatch.setattr(auto, "apply_match", refuse)
    report = run_matching(mconn, odm, period=OCTOBER)
    assert report.passes >= 1 and any("nieaktualna" in e for e in report.auto_errors)
    # B was first proposed as an overpayment (the invoice "taken" by A); after the pass without A's claim
    # its open proposal is the invoice itself - no overpayment that does not exist
    proposal = _open_rank1(mconn, b)[1]
    assert proposal["items"][0]["document_id"] == str(invoice) and proposal["items"][0]["difference_kind"] is None
    assert _open_rank1(mconn, a)[1]["items"][0]["document_id"] == str(invoice)
    assert _status(mconn, a) is None and _status(mconn, b) is None


# ---------------------------------------------------------------------------
# point 6: one cost, many pieces of evidence - evidence_only in the group
# ---------------------------------------------------------------------------


def test_the_sheet_row_of_a_bill_is_evidence_in_the_group(mconn, odm):
    ref = tx_id(mconn, "202610160785476080")                   # MC: bill 4/2026 + sheet row R08 + transfer
    run_matching(mconn, odm, period=OCTOBER)
    rows = mconn.execute(
        "SELECT document_id IS NOT NULL, internal_record_id, amount_minor, document_amount_minor, evidence_only, "
        "group_id, decision_id FROM l3_match WHERE transaction_id = %s AND status = 'active' ORDER BY evidence_only",
        (ref,)).fetchall()
    assert [(r[0], r[2], r[4]) for r in rows] == [(True, 180000, False), (False, 0, True)]
    assert rows[0][5] == rows[1][5] and rows[0][6] == rows[1][6]           # one group, one decision
    record = rows[1][1]
    assert check_match_coverage(mconn, odm.id, *OCTOBER, record=False).ok
    # the row is settled through the bill: never a candidate of another transfer, never a cost of its own
    assert load_context(mconn, odm.id).records[record].open_minor == 0
    covered = evidence_of_documents(load_internal_records(mconn, odm.id), load_documents(mconn, odm.id),
                                    load_matches(mconn, odm.id), person_heuristic=False)
    assert covered[record]["reason"] == "evidence_only" and covered[record]["group_id"] == rows[0][5]
    log = mconn.execute("SELECT evidence -> 'evidence_only' FROM l3_decision_log WHERE id = %s",
                        (rows[0][6],)).fetchone()[0]
    assert [e["label"] for e in log] == ["arkusz_pm_2026-10:R08"]
    # undo: the evidence goes with the decision
    revoke_decision(mconn, rows[0][6], "artur")
    assert mconn.execute("SELECT count(*) FROM l3_match WHERE transaction_id = %s AND status = 'active'",
                         (ref,)).fetchone()[0] == 0


def test_evidence_only_rows_have_no_amount(mconn, odm):
    ref = tx_id(mconn, "202610160785476080")
    record = mconn.execute("SELECT id FROM l1_internal_record WHERE row_no = 9 AND superseded_by IS NULL").fetchone()[0]
    decision = log_decision(mconn, company_id=odm.id, kind="match", subject_type="transaction", subject_id=ref,
                            decided_by="test", source="test")
    with pytest.raises(psycopg.errors.CheckViolation), mconn.transaction():
        mconn.execute("INSERT INTO l3_match (company_id, transaction_id, internal_record_id, amount_minor, "
                      "evidence_only, decision_id, source, method, created_by) "
                      "VALUES (%s, %s, %s, 180000, true, %s, 'test', 'human', 'test')", (odm.id, ref, record, decision))


# ---------------------------------------------------------------------------
# point 7: installments of a foreign invoice close it in its currency
# ---------------------------------------------------------------------------


@pytest.fixture
def euro_invoice(mconn, odm):
    iban = "DE89370400440532013000"
    supplier = party(mconn, odm.id, "Euro Lieferant GmbH", None, iban)
    invoice = add_doc(mconn, odm.id, "EU/7/2026", 1000000, counterparty_id=supplier, name="Euro Lieferant GmbH",
                      currency="EUR", fx_rate="4.25", payment_iban=iban)
    return supplier, iban, invoice


def _installment(mconn, odm, euro_invoice, amount_pln, title, day, original=None):
    supplier, iban, _ = euro_invoice
    return add_tx(mconn, odm.id, -amount_pln, title, counterparty_id=supplier, name="Euro Lieferant GmbH", iban=iban,
                  day=day, op_type="foreign_transfer", original_amount_minor=-original if original else None,
                  original_currency="EUR" if original else None)


def _accept(mconn, odm, transaction_id):
    run_matching(mconn, odm, period=OCTOBER)
    suggestion, proposal, _ = _open_rank1(mconn, transaction_id)
    apply_match(mconn, suggestion, decided_by="artur")
    return proposal["items"]


@pytest.mark.parametrize("with_euro_amount", [True, False])
def test_two_euro_installments_close_the_invoice_to_the_cent_with_fx_on_the_rest(mconn, odm, euro_invoice,
                                                                                 with_euro_amount):
    _, _, invoice = euro_invoice
    if with_euro_amount:            # the bank gives the amount in EUR; the second one is 0,50 EUR short (rounding)
        first = _installment(mconn, odm, euro_invoice, 2130000, "EU/7/2026 rata 1", date(2026, 10, 10), 500000)
        second = _installment(mconn, odm, euro_invoice, 2121200, "EU/7/2026 rata 2", date(2026, 10, 24), 499950)
    else:                           # only PLN: the first one's part of the invoice at the invoice's rate
        first = _installment(mconn, odm, euro_invoice, 2100000, "EU/7/2026 rata 1", date(2026, 10, 10))
        second = _installment(mconn, odm, euro_invoice, 2160000, "EU/7/2026 rata 2", date(2026, 10, 24))
    items = _accept(mconn, odm, first)
    assert items[0]["document_id"] == str(invoice) and items[0]["difference_kind"] == "underpayment"
    items = _accept(mconn, odm, second)
    assert items[0]["document_id"] == str(invoice) and items[0]["difference_kind"] == "fx"
    rows = mconn.execute("SELECT transaction_id, document_amount_minor, difference_kind::text, difference_minor "
                         "FROM l3_match WHERE document_id = %s AND status = 'active'", (invoice,)).fetchall()
    assert sum(r[1] for r in rows) == 1000000                        # 10 000,00 EUR to the cent
    last = next(r for r in rows if r[0] == second)
    rest = 1000000 - next(r for r in rows if r[0] == first)[1]
    assert last[1] == rest and last[2] == "fx"                     # the rest of the balance, the difference "kurs"
    if with_euro_amount:
        assert last[3] == 2121200 - 2125000                         # 5 000,00 EUR x 4,25 = 21 250,00 zł
    assert load_context(mconn, odm.id).docs[invoice].open_minor == 0
    assert check_match_coverage(mconn, odm.id, *OCTOBER, record=False).ok


# ---------------------------------------------------------------------------
# point 12: a company with its legal form in the title of an operator's payment
# ---------------------------------------------------------------------------


def test_revolut_title_with_the_legal_form_weighs_like_the_same_name(mconn, odm):
    result = result_for(mconn, odm.id, "202610233423350565")
    assert result.tx.extracted["payer_hint"] == {"name": "KWADRAT DEVELOPMENT SP. Z O.O.", "legal_form": "SP. Z O.O."}
    signal = next(s for s in result.best.signals if s.tag == "counterparty_title_name")
    assert signal.weight == W.WEIGHTS["counterparty_name"] and "formą prawną" in signal.fact_pl
    assert not result.best.hard                                    # still the queue (one proposal to click)


# ---------------------------------------------------------------------------
# point 13: the block of the semantics gate is a lasting flag, matching respects it
# ---------------------------------------------------------------------------


def test_a_subject_flagged_by_the_semantics_gate_is_sensitive_for_matching(mconn, odm):
    ref = tx_id(mconn, "202610086622571743")                       # Drukarnia: hard evidence, automatic
    assert only(mconn, odm.id, ref).best.hard
    mconn.execute(
        "INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, source, method, created_by) "
        "VALUES (%s, 'category', 'transaction', %s, '{\"needs_human\": true, \"reason\": \"sensitive\"}', "
        "'semantics', 'code', 'test')", (odm.id, ref))
    best = only(mconn, odm.id, ref).best
    assert not best.hard and any("wrażliwe" in b for b in best.blockers)
    # "to nie jest wrażliwe" of a person lifts it
    mconn.execute("INSERT INTO sensitive_exception (company_id, subject_type, subject_id, terms, reason, decided_by) "
                  "VALUES (%s, 'transaction', %s, '{}', 'test', 'artur')", (odm.id, ref))
    assert only(mconn, odm.id, ref).best.hard


# ---------------------------------------------------------------------------
# point 15: an invoice named in the title that is not in KSeF yet - wait a day, not the queue
# ---------------------------------------------------------------------------


def test_a_number_not_in_ksef_yet_waits_a_day_then_the_normal_run(mconn, odm):
    today = date.today()
    supplier = party(mconn, odm.id, NAME, NIP, IBAN)
    tx = add_tx(mconn, odm.id, -61500, "Zaplata za FV 77/11/2026", counterparty_id=supplier, name=NAME, iban=IBAN,
                day=today)
    run_matching(mconn, odm)
    _, proposal, evidence = _open_rank1(mconn, tx)
    assert proposal["final_status"] == "to_clarify" and proposal["awaiting_until"] == (today + timedelta(1)).isoformat()
    assert "awaiting_ksef" in evidence["tags"] and not evidence["hard"] and _status(mconn, tx) is None
    assert not any(i.subject_id == tx for i in build._match_items(mconn, odm.id))      # not in the queue (yet)
    # the day after tomorrow without the invoice: the queue
    later = evaluate(mconn, odm.id, subject_ids={tx}, today=today + timedelta(2)).results[0].best
    assert not later.has("awaiting_ksef")
    # the invoice arrives from KSeF: the next run proposes it as usual (and the automation closes it)
    invoice = add_doc(mconn, odm.id, "FV 77/11/2026", 61500, counterparty_id=supplier, nip=NIP, payment_iban=IBAN,
                      issue=today, due=today + timedelta(14))
    run_matching(mconn, odm)
    assert _status(mconn, tx) == ("matched", "rule")
    assert mconn.execute("SELECT document_id FROM l3_match WHERE transaction_id = %s", (tx,)).fetchone()[0] == invoice
