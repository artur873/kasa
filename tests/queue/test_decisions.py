"""The queue's decisions: accept / alternative / Inne… / reject / defer / revoke / batch / sensitive / sentinel."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kasa.core import decisions as dec
from kasa.core.queue import actions, build, state
from kasa.core.rules import lifecycle


def _match_item(conn, company_id, title_like):
    for item in actions.list_items(conn, company_id):
        if item["card_kind"] != "match":
            continue
        title = conn.execute("SELECT title_raw FROM l1_bank_transaction WHERE id = %s",
                             (item["subject_id"],)).fetchone()[0] or ""
        if title_like in title:
            return item
    raise AssertionError(title_like)


def _open_minor(conn, number):
    return conn.execute(
        """
        SELECT d.gross_minor - coalesce((SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) FROM l3_match m
                                          WHERE m.document_id = d.id AND m.status = 'active'), 0)
          FROM l1_document d WHERE d.number_raw = %s AND d.superseded_by IS NULL
        """,
        (number,),
    ).fetchone()[0]


def test_accept_writes_the_decision_with_a_ten_second_undo_and_revoke_brings_it_back(qconn, odm):
    item = _match_item(qconn, odm.id, "FV 4/09/2026 czesc 1")
    before = _open_minor(qconn, "FV 4/09/2026")
    actions.card(qconn, item["id"])                              # shown on the phone: the clock starts
    reply = actions.answer(qconn, item["id"], "accept", decided_by="artur")
    decision = reply["decision_ids"][0]
    row = qconn.execute("SELECT method::text, decided_at, undo_until, kind::text FROM l3_decision_log WHERE id = %s",
                        (decision,)).fetchone()
    assert row[0] == "human" and row[3] == "match" and row[2] - row[1] == timedelta(seconds=10)
    assert _open_minor(qconn, "FV 4/09/2026") == before - 500_000
    closed = state.get(qconn, item["id"])
    assert closed["closed_at"] and str(closed["decision_id"]) == decision and closed["duration_ms"] is not None
    # undo: a NEW decision revoke; rows revoked (nothing deleted); the invoice's balance and the card come back
    actions.undo(qconn, decision, decided_by="artur")
    assert qconn.execute("SELECT status::text FROM l3_match WHERE decision_id = %s", (decision,)).fetchone() == \
        ("revoked",)
    assert _open_minor(qconn, "FV 4/09/2026") == before
    build.build(qconn, odm)
    back = [i for i in actions.list_items(qconn, odm.id) if i["subject_id"] == item["subject_id"]
            and i["card_kind"] == "match"]
    assert len(back) == 1 and back[0]["id"] != item["id"] and back[0]["suggestion_ids"]
    with pytest.raises(dec.DecisionError):
        dec.revoke_decision(qconn, decision, "artur")                   # once


def test_other_no_document_needs_a_subtype_and_an_advance_waits_180_days(qconn, odm):
    item = _match_item(qconn, odm.id, "Zaliczka na poczet faktury")
    with pytest.raises(dec.DecisionError, match="podtyp"):
        actions.answer(qconn, item["id"], "other", decided_by="artur", extra={"kind": "no_document"})
    reply = actions.answer(qconn, item["id"], "other", decided_by="artur",
                           extra={"kind": "no_document", "subtype": "advance"})
    status = qconn.execute(
        "SELECT s.final_status::text, s.subtype, s.awaiting_until - t.booking_date FROM l3_transaction_status s "
        "JOIN l1_bank_transaction t ON t.id = s.transaction_id WHERE s.decision_id = %s",
        (reply["decision_ids"][0],)).fetchone()
    assert status == ("no_document", "advance", 180)


def test_other_matches_a_document_the_person_picked_and_alternative_is_rank_2(qconn, odm):
    item = _match_item(qconn, odm.id, "zaplata")                 # Kadr Studio: a tie of two invoices
    doc = qconn.execute("SELECT id FROM l1_document WHERE number_raw = 'FV 14/2026' AND superseded_by IS NULL"
                        ).fetchone()[0]
    reply = actions.answer(qconn, item["id"], "other", decided_by="artur",
                           extra={"kind": "matched", "targets": [{"document_id": str(doc)}]})
    match = qconn.execute("SELECT document_id, amount_minor, method::text FROM l3_match WHERE decision_id = %s",
                          (reply["decision_ids"][0],)).fetchone()
    assert match == (doc, 450000, "human")
    # every engine proposal of the transfer is resolved by the person's decision
    assert qconn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s AND status = 'open' "
                         "AND kind = 'match'", (item["subject_id"],)).fetchone() == (0,)


def test_reject_keeps_the_card_with_the_other_proposals(qconn, odm):
    item = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "match"
                and len(i["suggestion_ids"]) >= 2)
    first, second = item["suggestion_ids"][:2]
    actions.answer(qconn, item["id"], "reject", decided_by="artur")
    left = state.get(qconn, item["id"])
    assert left["closed_at"] is None and left["suggestion_ids"][0] == second
    assert qconn.execute("SELECT status::text, resolved_by_decision FROM l2_suggestion WHERE id = %s",
                         (first,)).fetchone() == ("rejected", None)


def _orange_cards(conn, company):
    """The 12 Orange invoices paid with their numbers: out of the automation (the person suspended the rule)."""
    rule = conn.execute("SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = "
                        "'builtin:match_invoice_number_same_counterparty'", (company.id,)).fetchone()[0]
    lifecycle.suspend(conn, rule, decided_by="artur", reason="chcę klikać faktury Orange sam")
    autos = conn.execute(
        """
        SELECT d.id FROM l3_decision_log d JOIN l1_bank_transaction t ON t.id = d.subject_id
         WHERE d.company_id = %s AND d.method = 'rule' AND t.title_raw LIKE 'F/400%%'
        """,
        (company.id,),
    ).fetchall()
    for (decision,) in autos:
        dec.revoke_decision(conn, decision, "artur", reason="do kolejki")
    build.build(conn, company)
    return [i for i in actions.list_items(conn, company.id) if i["card_kind"] == "match" and i["batch_key"]
            and "builtin:match_invoice_number" in i["batch_key"]]


def test_bulk_only_for_a_homogeneous_high_batch_and_undo_reverts_exactly_those(qconn, odm):
    cards = _orange_cards(qconn, odm)
    assert len(cards) == 12 and len({c["batch_key"] for c in cards}) == 1
    card = actions.card(qconn, cards[0]["id"])
    batch = card["batch"]
    assert batch["count"] == 12 and batch["amount"] == "4 380,00 zł" and "Orange" in batch["label"]
    # a batch with a card of another counterparty is refused
    other = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "match" and not i["batch_key"])
    with pytest.raises(dec.DecisionError):
        dec.bulk_accept(qconn, [cards[0]["suggestion_ids"][0], other["suggestion_ids"][0]], "artur")
    reply = actions.answer(qconn, cards[0]["id"], "bulk", decided_by="artur")
    decisions = qconn.execute("SELECT id, method::text FROM l3_decision_log WHERE batch_id = %s",
                              (reply["batch_id"],)).fetchall()
    assert len(decisions) == 12 and {m for _, m in decisions} == {"human"}        # each its own decision
    unrelated = qconn.execute("SELECT count(*) FROM l3_match WHERE status = 'active'").fetchone()[0]
    actions.undo_batch(qconn, reply["batch_id"], decided_by="artur")
    assert qconn.execute("SELECT count(*) FROM l3_decision_log WHERE reverts_decision_id = ANY(%s)",
                         ([d for d, _ in decisions],)).fetchone() == (12,)
    assert qconn.execute("SELECT count(*) FROM l3_match WHERE status = 'active'").fetchone()[0] == unrelated - 12
    build.build(qconn, odm)
    assert len([i for i in actions.list_items(qconn, odm.id) if i["batch_key"] == cards[0]["batch_key"]]) == 12


def test_not_sensitive_a_persons_term_is_retired_a_core_term_gets_an_exception(qconn, odm):
    from kasa.core.sensitive import add_human_term

    item = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "sensitive")
    add_human_term(qconn, odm.id, "dłużnika", created_by="artur")
    reply = actions.answer(qconn, item["id"], "not_sensitive", decided_by="artur", reason="to zwykła składka")
    assert reply["exception_terms"]           # komornik / zajęcie wynagrodzenia: core terms stay for everybody
    exception = qconn.execute("SELECT terms, decided_by FROM sensitive_exception WHERE subject_id = %s",
                              (item["subject_id"],)).fetchone()
    assert exception[1] == "artur" and exception[0] == reply["exception_terms"]
    assert state.get(qconn, item["id"])["outcome"] == "not_sensitive"


def test_sensitive_card_category_by_the_person(qconn, odm):
    item = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "sensitive")
    with pytest.raises(dec.DecisionError):
        actions.answer(qconn, item["id"], "accept", decided_by="artur")          # no category chosen
    reply = actions.answer(qconn, item["id"], "accept", decided_by="artur", extra={"category": "Inne"})
    assert qconn.execute("SELECT category, method::text FROM l3_category_decision WHERE decision_id = %s",
                         (reply["decision_ids"][0],)).fetchone() == ("Inne", "human")
    with pytest.raises(dec.DecisionError):
        dec.decide_category(qconn, odm.id, "transaction", item["subject_id"], "Nie ma takiej", decided_by="artur")


def test_sentinel_answer_is_stored_hidden_and_never_a_decision(qconn, odm):
    sentinel = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "sentinel")
    expected = qconn.execute("SELECT expected FROM queue_sentinel WHERE id = %s", (sentinel["sentinel_id"],)
                             ).fetchone()[0]
    decisions = qconn.execute("SELECT count(*) FROM l3_decision_log").fetchone()[0]
    card = actions.card(qconn, sentinel["id"])
    wrong = next((p for p in card["proposals"][1:]), None)
    reply = actions.answer(qconn, sentinel["id"], "accept", decided_by="artur",
                           suggestion_id=wrong["suggestion_id"] if wrong else None)
    assert reply == {"message": "Zapisano", "decision_ids": [], "undo_until": None}       # nothing tells the result
    assert qconn.execute("SELECT count(*) FROM l3_decision_log").fetchone()[0] == decisions
    passed = qconn.execute("SELECT passed, answer FROM queue_sentinel WHERE id = %s", (sentinel["sentinel_id"],)
                           ).fetchone()
    assert passed[0] is (wrong is None) or passed[0] == (passed[1] == expected)
    assert actions.stats(qconn, odm.id)["sentinels"]["answered"] == 1


def test_fast_decision_on_a_low_card_is_an_alarm(qconn, odm):
    low = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "match" and i["badge"] == "low")
    medium = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "match" and i["badge"] == "medium")
    actions.card(qconn, low["id"])
    actions.answer(qconn, low["id"], "accept", decided_by="artur")
    actions.card(qconn, medium["id"])
    qconn.execute("UPDATE queue_state SET shown_at = shown_at - interval '5 seconds' WHERE id = %s", (medium["id"],))
    actions.answer(qconn, medium["id"], "accept", decided_by="artur")
    assert state.get(qconn, low["id"])["fast_alarm"] is True
    slow = state.get(qconn, medium["id"])
    assert slow["fast_alarm"] is False and slow["duration_ms"] >= 5000
    stats = actions.stats(qconn, odm.id)
    assert stats["fast_alarms"] == 1 and any("0,8 s" in w for w in stats["warnings"])
    assert stats["work_minutes"] > 0 and stats["decided_cards"] == 2


def test_cost_object_card_buttons_and_revoke_of_an_approval(qconn, odm):
    item = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "cost_object_candidate")
    card = actions.card(qconn, item["id"])
    reply = actions.answer(qconn, item["id"], "approve", decided_by="artur")
    assert qconn.execute("SELECT status::text, method::text FROM l3_cost_object WHERE id = %s",
                         (item["subject_id"],)).fetchone() == ("approved", "human")
    # nothing allocated to it yet: the approval can be revoked (back to a candidate with its card)
    dec.revoke_decision(qconn, reply["decision_ids"][0], "artur")
    assert qconn.execute("SELECT status::text FROM l3_cost_object WHERE id = %s", (item["subject_id"],)
                         ).fetchone() == ("candidate",)
    assert qconn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s AND status = 'open'",
                         (item["subject_id"],)).fetchone() == (1,)
    other = card["candidate"]["others"][0]["id"]
    build.build(qconn, odm)
    again = next(i for i in actions.list_items(qconn, odm.id) if i["subject_id"] == item["subject_id"])
    actions.answer(qconn, again["id"], "distinct", decided_by="artur", extra={"other": other})
    assert qconn.execute("SELECT verdict::text FROM cost_object_pair_decision ORDER BY created_at DESC LIMIT 1"
                         ).fetchone() == ("different",)
