"""Units of the engine on small synthetic scenarios (on top of the fixture month, always rolled back)."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import date

import psycopg
import pytest

from kasa.core.db import RULE_NOT_AUTO
from kasa.core.decisions import log_decision
from kasa.core.matching.data import load_context
from kasa.core.matching.decide import MatchDecisionError, apply_match
from kasa.core.matching.engine import evaluate
from kasa.core.matching.run import run_matching
from kasa.core.matching.suggestions import save_result

from .conftest import OCTOBER
from .helpers import add_doc, add_tx, only, party, tx_id

IBAN = "PL27114020040000300201355387"
NIP = "5260001246"


@pytest.fixture
def supplier(mconn, odm):
    return party(mconn, odm.id, "Dostawca Testowy sp. z o.o.", NIP, IBAN)


def _pay(mconn, odm, supplier, amount, title="zaplata", **kw):
    return add_tx(mconn, odm.id, amount, title, counterparty_id=supplier, name="Dostawca Testowy sp. z o.o.",
                  iban=IBAN, **kw)


def test_tolerance_one_invoice_only_and_named(mconn, odm, supplier):
    add_doc(mconn, odm.id, "T/1/2026", 100000, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    best = only(mconn, odm.id, _pay(mconn, odm, supplier, -99000)).best          # 10 zł short of 1 000 zł
    item = best.items[0]
    assert (item.difference_kind, item.difference_minor, item.amount_minor) == ("tolerance", -1000, 99000)
    assert not best.hard and "amount_tolerance" in best.tags
    # 20 zł floor: 485 zł for a 500 zł invoice is within, 470 zł is not
    add_doc(mconn, odm.id, "T/2/2026", 50000, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    near = only(mconn, odm.id, _pay(mconn, odm, supplier, -48500)).best
    assert near.items and near.items[0].difference_kind == "tolerance"
    far = only(mconn, odm.id, _pay(mconn, odm, supplier, -47000)).considered
    assert not [c for c in far if c.kind == "document" and c.items[0].difference_kind == "tolerance"]


def test_several_invoices_only_to_the_grosz(mconn, odm, supplier):
    for number, amount in (("S/1", 10000), ("S/2", 20000), ("S/3", 30000)):
        add_doc(mconn, odm.id, number, amount, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    exact = only(mconn, odm.id, _pay(mconn, odm, supplier, -50000)).best         # 200 + 300
    assert exact.kind == "document" and "subset_sum" in exact.tags and not exact.hard
    assert sorted(i.label for i in exact.items) == ["S/2", "S/3"]
    near = only(mconn, odm.id, _pay(mconn, odm, supplier, -49999)).considered      # one grosz off
    assert not [c for c in near if len(c.items) > 1]


def test_numbers_in_the_title_with_the_exact_sum_are_hard(mconn, odm, supplier):
    for number, amount in (("A/1/26", 10000), ("A/2/26", 20000)):
        add_doc(mconn, odm.id, number, amount, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    best = only(mconn, odm.id, _pay(mconn, odm, supplier, -30000, title="Faktury A/1/26, A/2/26")).best
    assert best.hard and len(best.items) == 2 and best.rule_key == "builtin:match_invoice_number_same_counterparty"
    partial = only(mconn, odm.id, _pay(mconn, odm, supplier, -25000, title="Faktury A/1/26, A/2/26")).best
    assert not partial.hard and partial.items[-1].difference_kind == "underpayment"


def test_incoming_advance_before_the_invoice_then_the_invoice_arrives(mconn, odm):
    client = party(mconn, odm.id, "Klient Zaliczkowy sp. z o.o.", None, IBAN)
    tx = add_tx(mconn, odm.id, 300000, "Zaliczka na event grudniowy", counterparty_id=client,
                name="Klient Zaliczkowy sp. z o.o.", iban=IBAN, day=date(2026, 10, 10))
    first = only(mconn, odm.id, tx).best
    assert (first.final_status, first.subtype, first.awaiting_until) == ("no_document", "advance", date(2027, 4, 8))
    run_matching(mconn, odm, period=OCTOBER, auto_decide=False)
    old = mconn.execute("SELECT id FROM l2_suggestion WHERE subject_id = %s AND status = 'open'", (tx,)).fetchall()
    # the advance invoice comes (KSeF): the next run proposes it and expires the old suggestions
    add_doc(mconn, odm.id, "ZAL 1/12/2026", 300000, direction="sales", counterparty_id=client,
            name="Klient Zaliczkowy sp. z o.o.", kind="advance", issue=date(2026, 10, 12), due=None)
    report = run_matching(mconn, odm, period=OCTOBER, auto_decide=False)
    assert report.suggestions_expired >= len(old)
    assert mconn.execute("SELECT count(*) FROM l2_suggestion WHERE id = ANY(%s) AND status = 'expired'",
                         ([o[0] for o in old],)).fetchone()[0] == len(old)
    best = mconn.execute(
        "SELECT proposal FROM l2_suggestion WHERE subject_id = %s AND status = 'open' AND rank = 1", (tx,)
    ).fetchone()[0]
    assert best["final_status"] == "matched" and best["items"][0]["amount_minor"] == 300000


def test_second_run_without_changes_writes_nothing(mconn, odm):
    run_matching(mconn, odm, period=OCTOBER)
    counts = [mconn.execute(f"SELECT count(*) FROM {t} WHERE company_id = %s", (odm.id,)).fetchone()[0]
              for t in ("l2_suggestion", "l3_match", "l3_transaction_status", "l3_decision_log")]
    again = run_matching(mconn, odm, period=OCTOBER)
    assert again.suggestions_created == 0 and again.auto == 0 and again.suggestions_unchanged == again.subjects
    assert counts == [mconn.execute(f"SELECT count(*) FROM {t} WHERE company_id = %s", (odm.id,)).fetchone()[0]
                      for t in ("l2_suggestion", "l3_match", "l3_transaction_status", "l3_decision_log")]


def test_a_rejected_proposal_is_not_proposed_again(mconn, odm):
    ref = tx_id(mconn, "202610233423350565")
    run_matching(mconn, odm, period=OCTOBER)
    first = mconn.execute(
        "SELECT id, proposal FROM l2_suggestion WHERE subject_id = %s AND status = 'open' AND rank = 1", (ref,)
    ).fetchone()
    mconn.execute("UPDATE l2_suggestion SET status = 'rejected', resolved_at = now() WHERE id = %s", (first[0],))
    run_matching(mconn, odm, period=OCTOBER)
    proposals = [r[0] for r in mconn.execute(
        "SELECT proposal FROM l2_suggestion WHERE subject_id = %s AND status = 'open'", (ref,)).fetchall()]
    assert proposals and first[1] not in proposals


def test_human_decision_on_a_queue_suggestion(mconn, odm):
    ref = tx_id(mconn, "ML2610084922077")                 # partial payment of FV 4/09/2026
    run_matching(mconn, odm, period=OCTOBER)
    rows = mconn.execute("SELECT id, rank FROM l2_suggestion WHERE subject_id = %s AND status = 'open' ORDER BY rank",
                         (ref,)).fetchall()
    decision = apply_match(mconn, rows[0][0], decided_by="artur", reason="częściowa zapłata")
    match = mconn.execute(
        "SELECT amount_minor, document_amount_minor, difference_kind::text, difference_minor, method::text "
        "FROM l3_match WHERE transaction_id = %s", (ref,)).fetchall()
    assert match == [(500000, 500000, "underpayment", -422500, "human")]
    states = dict(mconn.execute("SELECT id, status::text FROM l2_suggestion WHERE subject_id = %s", (ref,)).fetchall())
    assert states[rows[0][0]] == "accepted" and all(states[r[0]] == "rejected" for r in rows[1:])
    assert mconn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s AND resolved_by_decision = %s",
                         (ref, decision)).fetchone()[0] == len(rows)
    log = mconn.execute("SELECT kind::text, payload, suggestion_id FROM l3_decision_log WHERE id = %s",
                        (decision,)).fetchone()
    assert log[0] == "match" and log[2] == rows[0][0] and log[1]["final_status"] == "matched"
    with pytest.raises(MatchDecisionError):                # accepted once
        apply_match(mconn, rows[0][0], decided_by="artur")


def test_revoked_match_gives_the_amount_back_to_the_invoice(mconn, odm):
    ref = tx_id(mconn, "202610086622571743")              # Drukarnia Format: FV number + IBAN, automatic
    run_matching(mconn, odm, period=OCTOBER)
    doc = mconn.execute("SELECT document_id, decision_id FROM l3_match WHERE transaction_id = %s", (ref,)).fetchone()
    assert load_context(mconn, odm.id).docs[doc[0]].open_minor == 0
    # 4C: revoke = a new human decision + status revoked on the rows of the decision
    revoke = mconn.execute(
        """
        INSERT INTO l3_decision_log (company_id, kind, subject_type, subject_id, decided_by, reverts_decision_id,
                                     source, method, created_by)
        VALUES (%s, 'revoke', 'transaction', %s, 'artur', %s, 'queue', 'human', 'artur') RETURNING id
        """,
        (odm.id, ref, doc[1])).fetchone()[0]
    for table in ("l3_match", "l3_transaction_status"):
        mconn.execute(f"UPDATE {table} SET status = 'revoked', revoked_by_decision = %s, method = 'human' "
                      "WHERE decision_id = %s", (revoke, doc[1]))
    ctx = load_context(mconn, odm.id)
    assert ctx.docs[doc[0]].open_minor == ctx.docs[doc[0]].gross_minor == 295200 and ref not in ctx.decided
    assert ref in {r.tx.id for r in evaluate(mconn, odm.id).results}


def test_the_automation_needs_a_rule_in_stage_auto(mconn, odm):
    ref = tx_id(mconn, "202610086622571743")
    ev = evaluate(mconn, odm.id, subject_ids={ref})
    saved = save_result(mconn, odm.id, ev.results[0], ev.params)
    rule = ev.ctx.builtin_rules["builtin:match_invoice_number_same_counterparty"]
    # a human change of a rule carries a new decision (KS003, review of phase 1, point 13)
    suspend = log_decision(mconn, company_id=odm.id, kind="rule", subject_type="rule", subject_id=rule["id"],
                           decided_by="artur", source="test", payload={"action": "suspend"})
    mconn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human', updated_by = 'artur', decision_id = %s "
                  "WHERE id = %s", (suspend, rule["id"]))
    with pytest.raises(psycopg.Error) as err, mconn.transaction():
        apply_match(mconn, saved.suggestion_ids[0], decided_by="rule:x", method="rule", rule_id=rule["id"],
                    rule_version=rule["version"])
    assert err.value.sqlstate == RULE_NOT_AUTO
    # a suspended built-in rule = the run sends the transfer to the queue
    resume = log_decision(mconn, company_id=odm.id, kind="rule", subject_type="rule", subject_id=rule["id"],
                          decided_by="artur", source="test", payload={"action": "resume"})
    mconn.execute("UPDATE l3_rule SET stage = 'auto', method = 'human', decision_id = %s WHERE id = %s",
                  (resume, rule["id"]))
    with pytest.raises(MatchDecisionError):
        apply_match(mconn, saved.suggestion_ids[0], decided_by="rule:x", method="rule")


def test_the_automation_refuses_a_stale_suggestion(mconn, odm):
    ref = tx_id(mconn, "202610086622571743")
    ev = evaluate(mconn, odm.id, subject_ids={ref})
    saved = save_result(mconn, odm.id, ev.results[0], ev.params)
    target = ev.results[0].best.items[0].document_id
    other = add_tx(mconn, odm.id, -295200, "Faktura 214/09/2026 jeszcze raz", day=date(2026, 10, 9))
    ev2 = evaluate(mconn, odm.id, subject_ids={other})
    first = save_result(mconn, odm.id, ev2.results[0], ev2.params)
    apply_match(mconn, first.suggestion_ids[0], decided_by="artur")        # someone took the invoice
    assert mconn.execute("SELECT count(*) FROM l3_match WHERE document_id = %s", (target,)).fetchone()[0] == 1
    rule = ev.ctx.builtin_rules["builtin:match_invoice_number_same_counterparty"]
    with pytest.raises(MatchDecisionError):
        apply_match(mconn, saved.suggestion_ids[0], decided_by="rule:x", method="rule", rule_id=rule["id"],
                    rule_version=rule["version"])


@dataclass
class _Hit:
    rule_id: object
    rule_key: str
    version: int
    stage: str
    action: dict
    evidence_count: int


def test_learned_rules_layer_through_importlib(mconn, odm, monkeypatch):
    ref = tx_id(mconn, "202610097692101583")               # salary, no document
    hit = _Hit(None, "R-17", 3, "hint", {"kind": "final_status", "final_status": "no_document",
                                          "subtype": "salary"}, 14)
    fake = types.ModuleType("kasa.core.rules.apply")
    calls = []
    fake.applicable_rules = lambda conn, company_id, subject_type, facts: calls.append(facts) or [hit]
    monkeypatch.setitem(sys.modules, "kasa.core.rules.apply", fake)
    best = only(mconn, odm.id, ref).best
    assert best.subtype == "salary" and "rule:R-17@3" in best.tags and not best.hard
    assert calls and calls[0]["direction"] == "out" and "title" in calls[0]
    # built-in rules come from code, never twice from the rules layer
    hit.rule_key = "builtin:no_document_zus_tax_fee"
    assert "rule:builtin:no_document_zus_tax_fee@3" not in only(mconn, odm.id, ref).best.tags


def test_the_automation_cannot_close_a_soft_suggestion_whoever_calls(mconn, odm):
    ref = tx_id(mconn, "202610233423350565")               # Revolut: one proposal, not hard
    ev = evaluate(mconn, odm.id, subject_ids={ref})
    saved = save_result(mconn, odm.id, ev.results[0], ev.params)
    rule = ev.ctx.builtin_rules["builtin:match_single_open_invoice_exact_amount"]
    with pytest.raises(MatchDecisionError, match="trzech warunków"):
        apply_match(mconn, saved.suggestion_ids[0], decided_by="rule:x", method="rule", rule_id=rule["id"],
                    rule_version=rule["version"])
    apply_match(mconn, saved.suggestion_ids[0], decided_by="artur")      # the owner may


def test_invoice_and_its_correction_named_together_are_one_chain(mconn, odm, supplier):
    base = add_doc(mconn, odm.id, "FK/7/2026", 100000, counterparty_id=supplier, nip=NIP, payment_iban=IBAN)
    add_doc(mconn, odm.id, "KOR/7/2026", -25000, counterparty_id=supplier, nip=NIP, payment_iban=IBAN,
            kind="correction", corrects=base)
    best = only(mconn, odm.id, _pay(mconn, odm, supplier, -75000, title="FK/7/2026 i KOR/7/2026")).best
    assert best.hard and not best.blockers
    assert [(i.label, i.amount_minor, i.difference_kind) for i in best.items] == [
        ("FK/7/2026", 100000, None), ("KOR/7/2026", -25000, "correction")]
    other = only(mconn, odm.id, _pay(mconn, odm, supplier, -75000, title="FK/7/2026 i XY/99/2026")).best
    assert not other.hard and "nie wszystkie numery z tytułu znalezione" in other.blockers


def test_a_recurring_identical_bank_fee_is_not_a_duplicate(mconn, odm):
    bank = party(mconn, odm.id, "Bank Millennium S.A.", None, None)
    first = add_tx(mconn, odm.id, -500, "Prowizja za przelew Express Elixir", counterparty_id=bank, op_type="fee",
                   day=date(2026, 10, 2))
    second = add_tx(mconn, odm.id, -500, "Prowizja za przelew Express Elixir", counterparty_id=bank, op_type="fee",
                    day=date(2026, 10, 3))
    for tx in (first, second):
        best = only(mconn, odm.id, tx).best
        assert (best.subtype, best.hard) == ("fee", True) and best.margin >= 0.2
