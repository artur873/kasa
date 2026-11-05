"""Gate of phase 4 (docs/FAZY.md): matching, cost objects, allocation, rules and the review queue.

A fresh database runs the whole pipeline (``tests/queue/pipeline.py``: every channel ingested, semantics,
control set from the reference file, calibration, matching, discovery, allocation, queue), then the
user clicks the queue as the reference truth says - only through the queue's API
(``kasa.core.queue.actions.answer`` -> ``accept_suggestion`` / ``decide_other``), approving the five
candidates on their cards. Then:

(a) 0 false automatic matches (and allocations) against the labels;
(b) 0 approved cost objects without a person's decision in ``l3_decision_log``;
(c) company report: control difference 0,00 in both views, margins per object = ground truth to the grosz;
(d) the automation writing over a subject decided by a person -> KS003;
(e) rules: after synthetic decisions from 2 months a ZUS / Orange candidate appears, goes hint -> shadow ->
    ready -> auto through observations, one correction suspends it; the audit draws 2% (min 20);
(f) undo: decision -> revoke -> the transfer back in the queue, the invoice's balance back; the batch of
    12 Orange payments (all "high") accepted at once and undone at once (exactly those 12);
(g) sentinels and the 0.8 s alarm;
(h) truth checks 1-5 green after everything.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from uuid import UUID

import psycopg
import pytest

from kasa.core.checks import (
    check_allocation_shares,
    check_balance_continuity,
    check_ksef_completeness,
    check_match_coverage,
    check_mpp,
)
from kasa.core.companies import get_company
from kasa.core.db import HUMAN_DECISION_PROTECTED
from kasa.core.decisions import accept_suggestion, decide_other, revoke_decision
from kasa.core.matching.calibrate import SUBTYPE_EQUIVALENT
from kasa.core.matching.decide import MatchDecisionError, apply_match
from kasa.core.providers.ksef import MockKsef
from kasa.core.queue import actions, build
from kasa.core.reports.company import company_report
from kasa.core.rules import audit, lifecycle
from kasa.core.rules.auto import apply_category_rules
from kasa.reference import ground_truth as reference
from kasa.reference.ground_truth import SUBTYPE_BY_CATEGORY
from tests.allocation.helpers import event_map, expected_allocations, minor
from tests.allocation.helpers import ground_truth as load_truth
from tests.conftest import drop_database
from tests.queue.pipeline import USER, click_queue, copy_database, derived_name, pipeline_database
from tests.rules.helpers import counterparty, decide_category_by_person, months, synthetic_transfers

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
TRUTH = load_truth()


@pytest.fixture(scope="module")
def clicked() -> Iterator[tuple[str, object]]:
    """The pipeline database after the person answered the whole queue (committed copy)."""
    url = copy_database(pipeline_database(), derived_name("gate4"))
    with psycopg.connect(url) as conn:
        company = get_company(conn, "odm")
        log = click_queue(conn, company)
        conn.commit()
    yield url, log
    drop_database(url)


@pytest.fixture
def gconn(clicked) -> Iterator[psycopg.Connection]:
    with psycopg.connect(clicked[0]) as connection, connection.transaction(force_rollback=True):
        yield connection


@pytest.fixture
def odm(gconn):
    return get_company(gconn, "odm")


def test_the_person_answered_the_queue_through_its_buttons(clicked, gconn, odm):
    _, log = clicked
    assert log.buttons["approve"] == 5                     # the five candidates, on their cards
    # 39 match cards: 35 + 4 invoice numbers with a counterparty agreeing only by name (review of 4A, point 2)
    # no sentinel while the person clicks: they repeat her OWN decisions older than 7 days (review of 4C/5, pt 5)
    assert log.cards["match"] == 39 and log.sentinels == 0
    assert log.buttons["accept"] > 100                     # nearly every card: the first or another proposal
    # left in the queue: what the reference file does not decide (19 September documents of the JPK and the
    # 2 transfers "to clarify" - their allocation waits for the person's next step)
    left = [u for u in log.unexpected if u.startswith("allocation")]
    assert len(left) == len(log.unexpected) == 21
    # no decision of the person was written around the queue
    sources = dict(gconn.execute("SELECT source, count(*) FROM l3_decision_log WHERE method = 'human' GROUP BY 1"
                                 ).fetchall())
    assert set(sources) == {"queue"}


# ---------------------------------------------------------------------------
# (a) 0 false automatic decisions
# ---------------------------------------------------------------------------


def _subtype(value):
    return SUBTYPE_EQUIVALENT.get(value, value)


def test_a_zero_false_automatic_matches_and_allocations(gconn, odm):
    labels = reference.expected_labels(gconn, odm.id, TRUTH, "transaction")
    autos = gconn.execute(
        """
        SELECT d.subject_id, d.payload FROM l3_decision_log d
         WHERE d.company_id = %s AND d.kind = 'match' AND d.method = 'rule'
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
        """,
        (odm.id,),
    ).fetchall()
    assert len(autos) == 41                 # 45 - 4 name-only counterparties (review of 4A, point 2)
    false = []
    for subject, payload in autos:
        label = labels[("transaction", subject)]
        if payload["final_status"] != label["status"]:
            false.append((subject, payload["final_status"], label["status"]))
        elif label["status"] == "matched":
            got = {i.get("document_id") or i.get("internal_record_id") or i.get("related_transaction_id")
                   for i in payload["items"]}
            if got != {d["id"] for d in label["documents"]}:
                false.append((subject, got, label["documents"]))
        elif label["status"] == "no_document" and _subtype(payload.get("subtype")) != _subtype(label.get("subtype")):
            false.append((subject, payload.get("subtype"), label.get("subtype")))
    assert not false, false
    # the automation of allocation (signal A) against the reference file as well
    expected = expected_allocations(gconn, odm, event_map(gconn, odm), TRUTH)
    wrong = []
    for subject_type, subject_id in gconn.execute(
        "SELECT DISTINCT subject_type::text, subject_id FROM l3_allocation WHERE company_id = %s AND method = 'rule' "
        "AND status = 'active'", (odm.id,)).fetchall():
        got = {r[0]: Fraction(r[1]) for r in gconn.execute(
            "SELECT cost_object_id, share FROM l3_allocation WHERE subject_id = %s AND status = 'active'",
            (subject_id,)).fetchall()}
        want = expected[(subject_type, subject_id)]
        if set(got) != set(want) or any(abs(got[k] - want[k]) > Fraction(1, 100000) for k in got):
            wrong.append(subject_id)
    assert not wrong


# ---------------------------------------------------------------------------
# (b) no cost object without a person's card
# ---------------------------------------------------------------------------


def test_b_no_approved_cost_object_without_a_persons_decision(gconn, odm):
    rows = gconn.execute(
        """
        SELECT co.id, co.name, co.method::text,
               EXISTS (SELECT 1 FROM l3_decision_log d
                        WHERE d.id = co.decision_id AND d.method = 'human' AND d.kind = 'cost_object'
                          AND d.payload ->> 'action' IN ('approve', 'merge') AND d.subject_id = co.id)
          FROM l3_cost_object co
         WHERE co.company_id = %s AND co.status = 'approved' AND co.system_code IS NULL
        """,
        (odm.id,),
    ).fetchall()
    assert len(rows) == 5 and {r[1] for r in rows} == {e["name"] for e in TRUTH["events"]}
    assert all(method == "human" and has_card for _, _, method, has_card in rows)


# ---------------------------------------------------------------------------
# (c) company report
# ---------------------------------------------------------------------------


def test_c_company_report_difference_zero_and_margins_equal_the_ground_truth(gconn, odm):
    report = company_report(gconn, odm.id, "2026-10")
    assert report.cash_difference_minor == 0 and report.memorial_difference_minor == 0
    for event in TRUTH["events"]:
        line = report.object_line(event["name"])
        assert line.revenue_minor == minor(event["revenue_net_pln"]), event["id"]
        assert line.cost_minor == minor(event["cost_net_pln"]), event["id"]
        assert line.margin_minor == minor(event["margin_net_pln"]), event["id"]
        assert line.cash_minor == minor(event["cash_contribution_pln"]), event["id"]
        assert line.cost_pending_minor == 0 and line.revenue_pending_minor == 0
    expected = TRUTH["metrics_expected"]["cash_report_2026_10"]
    assert report.cash["objects"] == minor(expected["events"])
    assert report.cash["unassigned"] == minor(expected["unassigned"])
    assert report.cash_control_minor == minor(expected["balance_change"])


# ---------------------------------------------------------------------------
# (d) the automation never writes over a person
# ---------------------------------------------------------------------------


def test_d_automation_over_a_persons_decision_is_refused_by_the_database(gconn, odm):
    status_id, tx, decision = gconn.execute(
        "SELECT id, transaction_id, decision_id FROM l3_transaction_status WHERE company_id = %s AND method = 'human' "
        "AND status = 'active' LIMIT 1", (odm.id,)).fetchone()
    rule = gconn.execute("SELECT id, version FROM l3_rule WHERE company_id = %s AND rule_key = "
                         "'builtin:no_document_zus_tax_fee'", (odm.id,)).fetchone()
    with pytest.raises(psycopg.Error) as err, gconn.transaction():
        gconn.execute("UPDATE l3_transaction_status SET method = 'rule', rule_id = %s, rule_version = %s, "
                      "final_status = 'no_document' WHERE id = %s", (rule[0], rule[1], status_id))
    assert err.value.sqlstate == HUMAN_DECISION_PROTECTED
    # code posing as the person without a new decision: refused too (review of phase 1, point 13)
    with pytest.raises(psycopg.Error) as err, gconn.transaction():
        gconn.execute("UPDATE l3_transaction_status SET final_status = 'to_clarify', method = 'human' WHERE id = %s",
                      (status_id,))
    assert err.value.sqlstate == HUMAN_DECISION_PROTECTED
    allocation = gconn.execute("SELECT id FROM l3_allocation WHERE company_id = %s AND method = 'human' AND "
                               "status = 'active' LIMIT 1", (odm.id,)).fetchone()[0]
    with pytest.raises(psycopg.Error) as err, gconn.transaction():
        gconn.execute("UPDATE l3_allocation SET method = 'rule', rule_id = %s, rule_version = %s WHERE id = %s",
                      (rule[0], rule[1], allocation))
    assert err.value.sqlstate == HUMAN_DECISION_PROTECTED
    # and the automation's own path refuses a subject decided by a person
    suggestion = gconn.execute("SELECT id FROM l2_suggestion WHERE subject_id = %s AND kind = 'match' "
                               "ORDER BY created_at DESC LIMIT 1", (tx,)).fetchone()[0]
    gconn.execute("UPDATE l2_suggestion SET status = 'open', resolved_at = NULL, resolved_by_decision = NULL "
                  "WHERE id = %s", (suggestion,))
    with pytest.raises(MatchDecisionError):
        apply_match(gconn, suggestion, decided_by="rule:x", method="rule", rule_id=rule[0], rule_version=rule[1])
    assert decision


# ---------------------------------------------------------------------------
# (e) rules and the audit
# ---------------------------------------------------------------------------


def _card(conn, company_id, kind, text):
    for item in actions.list_items(conn, company_id):
        if item["card_kind"] == kind and text in item["rank_reason"]:
            return item
    return None


def test_e_rule_from_two_months_matures_through_observations_and_one_correction_suspends(gconn, odm):
    zus = counterparty(gconn, odm.id, "Zakład Ubezpieczeń%")
    orange = counterparty(gconn, odm.id, "Orange%")
    # synthetic history: 6 ZUS transfers and 6 Orange transfers in two months, categorized by the person
    decide_category_by_person(gconn, odm.id, synthetic_transfers(gconn, odm.id, zus,
                                                                 months(date(2026, 8, 1), 2, per_month=3)), "Wynagrodzenia i daniny / ZUS")
    decide_category_by_person(gconn, odm.id, synthetic_transfers(
        gconn, odm.id, orange, months(date(2026, 8, 1), 2, per_month=3), amount_minor=-39_000,
        title="F/4001/{month}", op_type="transfer"), "Biuro / IT i subskrypcje (SaaS)")
    build.build(gconn, odm)
    zus_card = _card(gconn, odm.id, "rule_candidate", "Wynagrodzenia i daniny / ZUS")
    orange_card = _card(gconn, odm.id, "rule_candidate", "Orange")
    assert zus_card and orange_card
    card = actions.card(gconn, zus_card["id"])
    assert card["rule"]["summary"] == "6 z 6 u 1 kontrahenta" and len(card["rule"]["examples"]) == 3
    actions.answer(gconn, zus_card["id"], "accept", decided_by=USER)            # candidate -> hint
    rule_key = card["rule"]["rule_key"]

    def stage():
        return gconn.execute("SELECT effective_stage::text, observations, ready_for_auto FROM rule_stage_effective "
                             "WHERE company_id = %s AND rule_key = %s", (odm.id, rule_key)).fetchone()

    assert stage() == ("hint", 0, False)
    # every further decision of the person is an observation of the rule
    more = synthetic_transfers(gconn, odm.id, zus, months(date(2020, 1, 1), 40, per_month=2))
    decide_category_by_person(gconn, odm.id, more[:30], "Wynagrodzenia i daniny / ZUS")
    assert stage() == ("shadow", 30, False)
    decide_category_by_person(gconn, odm.id, more[30:73], "Wynagrodzenia i daniny / ZUS")
    assert stage() == ("shadow", 73, True)
    build.build(gconn, odm)
    ready = _card(gconn, odm.id, "rule_ready", rule_key)
    assert ready is not None
    actions.answer(gconn, ready["id"], "accept", decided_by=USER)               # "włącz auto" - one click
    assert stage()[0] == "auto"
    fresh = synthetic_transfers(gconn, odm.id, zus, [date(2026, 11, 2)])
    # the automation categorizes every ZUS transfer still without a category (October's, the 7 left, the new one)
    assert apply_category_rules(gconn, odm).applied == 9
    signed = gconn.execute("SELECT method::text FROM l3_category_decision WHERE subject_id = %s AND status = 'active'",
                           (fresh[0],)).fetchone()
    assert signed == ("rule",)
    # one correction: the person revokes the automatic category -> suspended, with a post-mortem draft
    auto_decision = gconn.execute("SELECT decision_id FROM l3_category_decision WHERE subject_id = %s",
                                  (fresh[0],)).fetchone()[0]
    revoke_decision(gconn, auto_decision, USER, reason="to nie ZUS, to PFRON")
    current = lifecycle.current_version_id(gconn, odm.id, rule_key)
    rule = lifecycle.rule_row(gconn, current)
    assert rule["stage_text"] == "suspended" and rule["post_mortem"].startswith("Post-mortem (szkic)")
    assert apply_category_rules(gconn, odm).applied == 0


def test_e_audit_draws_two_percent_min_twenty_of_the_automation(gconn, odm):
    automatic = gconn.execute("SELECT count(*) FROM l3_decision_log WHERE company_id = %s AND method = 'rule'",
                              (odm.id,)).fetchone()[0]
    assert automatic == 62                                          # 41 matches + 21 allocations (signal A)
    report = audit.sample(gconn, odm, "2026-10", seed=11)
    # subjects of October (2 allocated lines are September's documents); 2% < 20 -> 20
    september = audit.population(gconn, odm.id, "2026-09")
    assert report.population + len(september) == 62 and report.sample_size == 20
    build.build(gconn, odm)
    cards = [i for i in actions.list_items(gconn, odm.id) if i["card_kind"] == "audit"]
    assert len(cards) == 20 and actions.card(gconn, cards[0]["id"])["label"] == "audyt"


# ---------------------------------------------------------------------------
# (f) undo and the homogeneous batch
# ---------------------------------------------------------------------------


def test_f_undo_brings_the_transfer_back_and_the_invoice_balance(gconn, odm):
    tx, decision, document = gconn.execute(
        """
        SELECT m.transaction_id, m.decision_id, m.document_id FROM l3_match m
          JOIN l3_decision_log d ON d.id = m.decision_id
         WHERE m.company_id = %s AND m.status = 'active' AND d.method = 'human' AND m.document_id IS NOT NULL
         LIMIT 1
        """,
        (odm.id,),
    ).fetchone()

    def open_minor():
        return gconn.execute(
            "SELECT d.gross_minor - coalesce((SELECT sum(coalesce(m.document_amount_minor, m.amount_minor)) "
            "FROM l3_match m WHERE m.document_id = d.id AND m.status = 'active'), 0) FROM l1_document d WHERE d.id = %s",
            (document,)).fetchone()[0]

    before = open_minor()
    actions.undo(gconn, decision, decided_by=USER, reason="pomyłka")
    assert open_minor() > before
    build.build(gconn, odm)
    back = [i for i in actions.list_items(gconn, odm.id) if i["subject_id"] == tx and i["card_kind"] == "match"]
    assert len(back) == 1 and back[0]["suggestion_ids"]
    # the person decides again through the card
    reply = actions.answer(gconn, back[0]["id"], "accept", decided_by=USER)
    assert reply["decision_ids"] and open_minor() == before


def test_f_twelve_orange_payments_as_one_batch_and_one_undo(gconn, odm):
    rule = gconn.execute("SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = "
                         "'builtin:match_invoice_number_same_counterparty'", (odm.id,)).fetchone()[0]
    lifecycle.suspend(gconn, rule, decided_by=USER, reason="sprawdzam faktury Orange sam")
    for (decision,) in gconn.execute(
        "SELECT d.id FROM l3_decision_log d JOIN l1_bank_transaction t ON t.id = d.subject_id "
        "WHERE d.company_id = %s AND d.method = 'rule' AND t.title_raw LIKE 'F/400%%'", (odm.id,)).fetchall():
        revoke_decision(gconn, decision, USER, reason="do kolejki")
    build.build(gconn, odm)
    orange = [i for i in actions.list_items(gconn, odm.id) if i["batch_key"] and "builtin:match_invoice_number"
              in i["batch_key"]]
    assert len(orange) == 12 and {i["badge"] for i in orange} == {"high"}
    card = actions.card(gconn, orange[0]["id"])
    assert card["batch"]["count"] == 12 and card["batch"]["amount"] == "4 380,00 zł"
    reply = actions.answer(gconn, orange[0]["id"], "bulk", decided_by=USER)
    batch = UUID(reply["batch_id"])
    decided = gconn.execute("SELECT id FROM l3_decision_log WHERE batch_id = %s AND kind = 'match'", (batch,)).fetchall()
    assert len(decided) == 12
    actions.undo_batch(gconn, batch, decided_by=USER)
    revoked = gconn.execute("SELECT count(*) FROM l3_decision_log WHERE kind = 'revoke' AND reverts_decision_id = "
                            "ANY(%s)", ([d[0] for d in decided],)).fetchone()[0]
    assert revoked == 12
    assert gconn.execute("SELECT count(*) FROM l3_transaction_status s JOIN l1_bank_transaction t ON "
                         "t.id = s.transaction_id WHERE t.title_raw LIKE 'F/400%%' AND s.status = 'active'"
                         ).fetchone() == (0,)


# ---------------------------------------------------------------------------
# (g) sentinels and the 0.8 s alarm
# ---------------------------------------------------------------------------


def test_g_sentinels_and_the_fast_decision_alarm(clicked, gconn, odm):
    _, log = clicked
    answered = gconn.execute("SELECT count(*), count(*) FILTER (WHERE passed) FROM queue_sentinel "
                             "WHERE company_id = %s AND answered_at IS NOT NULL", (odm.id,)).fetchone()
    assert answered[0] == log.sentinels and answered[0] == answered[1]       # the careful person passed them all
    # a week later the person meets one of her own decisions again - a careless answer fails, nothing tells it
    labels = reference.expected_labels(gconn, odm.id, TRUTH, "transaction")
    later = datetime.now(timezone.utc) + timedelta(days=8)
    build.build(gconn, odm)
    assert not [i for i in actions.list_items(gconn, odm.id) if i["card_kind"] == "sentinel"]   # fresh decisions
    build.build(gconn, odm, now=later)
    sentinel = next((i for i in actions.list_items(gconn, odm.id) if i["card_kind"] == "sentinel"), None)
    if sentinel is None:                       # few cards left: one more sentinel for the test
        build.MIN_FOR_SENTINEL, previous = 0, build.MIN_FOR_SENTINEL
        try:
            build.build(gconn, odm, now=later)
        finally:
            build.MIN_FOR_SENTINEL = previous
        sentinel = next(i for i in actions.list_items(gconn, odm.id) if i["card_kind"] == "sentinel")
    assert gconn.execute("SELECT d.method::text FROM queue_sentinel s JOIN l3_decision_log d ON d.id = s.source_ref "
                         "WHERE s.id = %s", (sentinel["sentinel_id"],)).fetchone() == ("human",)
    label = labels[("transaction", sentinel["subject_id"])]
    wrong_kind = "own_transfer" if label["status"] != "own_transfer" else "to_clarify"
    reply = actions.answer(gconn, sentinel["id"], "other", decided_by=USER, extra={"kind": wrong_kind})
    assert reply["message"] == "Zapisano" and "passed" not in reply
    assert gconn.execute("SELECT passed FROM queue_sentinel WHERE id = %s", (sentinel["sentinel_id"],)
                         ).fetchone() == (False,)
    stats = actions.stats(gconn, odm.id)
    assert stats["sentinels"]["failed"] == 1
    # the clicks of the simulation took milliseconds: every "low" card decided under 0.8 s is an alarm
    assert stats["fast_alarms"] > 0 and any("0,8 s" in w for w in stats["warnings"])


# ---------------------------------------------------------------------------
# (h) truth checks 1-5
# ---------------------------------------------------------------------------


def test_h_truth_checks_green_after_everything(gconn, odm):
    balances = check_balance_continuity(gconn, odm.id, *OCTOBER, record=False)
    assert balances and all(b.ok for b in balances), [b.summary() for b in balances if not b.ok]
    assert check_match_coverage(gconn, odm.id, *OCTOBER, record=False).ok
    shares = check_allocation_shares(gconn, odm.id, record=False)
    assert shares.ok and shares.detail["subjects"] > 80
    ksef = check_ksef_completeness(gconn, odm, MockKsef(), date(2026, 9, 1), date(2026, 10, 31))
    assert ksef.ok
    assert check_mpp(gconn, odm.id, *OCTOBER, record=False).ok


def test_h_queue_kpi_is_minutes_and_errors_weighted_by_amount(gconn, odm):
    stats = actions.stats(gconn, odm.id)
    assert stats["decided_cards"] > 100 and stats["work_minutes"] >= 0
    assert stats["errors_weighted_minor"] == 0
    assert "percent" not in " ".join(stats) and "ai" not in {k.lower() for k in stats}
    text = actions.render_stats(stats)
    assert "min" in text and "Błędy ważone kwotą" in text


def test_subtype_labels_of_the_reference_file_are_complete():
    """Every "no document" of the reference file has a subtype (review of 4A/4B, point 11)."""
    for t in TRUTH["transactions"]:
        if t["expected_status"] == "no_document":
            assert SUBTYPE_BY_CATEGORY.get(t["no_document_category"]), t["scenario_key"]


def test_decide_other_and_accept_are_the_only_paths_of_the_simulation(gconn, odm):
    """The simulation never called apply_* directly: every human decision came from the queue (source)."""
    rows = gconn.execute("SELECT DISTINCT source FROM l3_decision_log WHERE method = 'human' AND company_id = %s",
                         (odm.id,)).fetchall()
    assert rows == [("queue",)]
    assert accept_suggestion and decide_other
