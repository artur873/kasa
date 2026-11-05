"""Rules learned from clicks: mining, stages derived from observations, versions, suspension, simulation."""

from __future__ import annotations

from datetime import date

import pytest

from kasa.core.decisions import accept_suggestion, decide_category, decide_other, revoke_decision
from kasa.core.rules import lifecycle, mine
from kasa.core.rules.auto import apply_category_rules
from kasa.core.rules.simulate import simulate

from .helpers import counterparty, decide_category_by_person, months, synthetic_transfers


def _candidate(conn, company_id, question_like):
    return conn.execute(
        """
        SELECT r.id, r.rule_key, s.id, s.proposal, s.evidence FROM l3_rule r
          JOIN l2_suggestion s ON s.subject_id = r.id AND s.kind = 'rule_candidate' AND s.status = 'open'
         WHERE r.company_id = %s AND r.stage = 'candidate' AND r.question LIKE %s
        """,
        (company_id, question_like),
    ).fetchone()


def _state(conn, rule_key):
    return conn.execute(
        "SELECT effective_stage::text, observations, agreed, ready_for_auto, stored_stage::text FROM rule_stage_effective "
        "WHERE rule_key = %s", (rule_key,)).fetchone()


def test_candidate_needs_five_cases_in_two_months_and_only_human_decisions(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    # 4 cases in two months: below the threshold
    first = synthetic_transfers(qconn, odm.id, zus, months(date(2026, 8, 1), 2, per_month=2))
    decide_category_by_person(qconn, odm.id, first, "ZUS")
    assert _candidate(qconn, odm.id, "%ZUS%") is None
    # the automation's decisions never count: a rule-made category on a fifth transfer changes nothing
    # (a decision of the automation needs a rule in auto; it is not evidence for mining)
    fifth = synthetic_transfers(qconn, odm.id, zus, [date(2026, 9, 20)])
    decide_category_by_person(qconn, odm.id, fifth, "ZUS")            # the 5th human case
    row = _candidate(qconn, odm.id, "%ZUS%")
    assert row is not None
    rule_id, rule_key, card_id, proposal, evidence = row
    assert proposal["hits"] == 5 and proposal["total"] == 5 and proposal["counterparties"] == 1
    assert proposal["question"].startswith("Czy ") and "ZUS" in proposal["question"]
    assert len(proposal["examples"]) == 3 and proposal["exceptions"] == []
    assert proposal["months"] == ["2026-08", "2026-09"]
    assert evidence["badge"] in ("medium", "high") and "Twoje decyzje" in " ".join(evidence["facts"])
    rule = lifecycle.rule_row(qconn, rule_id)
    assert rule["method_text"] == "code" and rule["stage_text"] == "candidate"
    assert rule["scope"]["counterparties"] == [str(zus)] and rule["scope"]["subject_types"] == ["transaction"]
    # a second run with nothing new: idempotent
    assert mine.mine(qconn, odm).created == []


def test_one_month_of_data_needs_min_months_1(qconn, odm):
    orange = counterparty(qconn, odm.id, "Orange%")
    txs = synthetic_transfers(qconn, odm.id, orange, [date(2026, 7, d) for d in (3, 6, 9, 12, 15, 18)],
                              amount_minor=-39_000, title="F/4009{month}", op_type="transfer")
    for tx in txs:
        decide_category(qconn, odm.id, "transaction", tx, "IT/SaaS", decided_by="artur")
    assert mine.mine(qconn, odm).created == []                       # one month only
    report = mine.mine(qconn, odm, min_months=1)
    assert len(report.created) == 1 and "Orange" in report.created[0]
    # the exceptions are ALL listed: one Orange transfer decided differently
    other = synthetic_transfers(qconn, odm.id, orange, [date(2026, 7, 21)], amount_minor=-12_000,
                                title="F/40099/07/26", op_type="transfer")
    decide_category(qconn, odm.id, "transaction", other[0], "Inne", decided_by="artur")
    report = mine.mine(qconn, odm, min_months=1)
    assert len(report.refreshed) == 1
    row = _candidate(qconn, odm.id, "%Orange%")
    assert row[3]["hits"] == 6 and row[3]["total"] == 7 and len(row[3]["exceptions"]) == 1


def test_lifecycle_hint_shadow_ready_auto_then_one_correction_suspends(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    history = synthetic_transfers(qconn, odm.id, zus, months(date(2026, 4, 1), 3, per_month=2))
    decide_category_by_person(qconn, odm.id, history, "ZUS")
    rule_id, rule_key, card_id, _, _ = _candidate(qconn, odm.id, "%ZUS%")
    # accept through the card of the queue: a NEW version in stage hint (v1 stays as it was)
    result = accept_suggestion(qconn, card_id, decided_by="artur")
    assert _state(qconn, rule_key)[0] == "hint"
    versions = qconn.execute("SELECT version, stage::text, method::text FROM l3_rule WHERE rule_key = %s "
                             "ORDER BY version", (rule_key,)).fetchall()
    assert versions == [(1, "candidate", "code"), (2, "hint", "human")] and result.decision_id
    with pytest.raises(lifecycle.RuleDecisionError):
        lifecycle.enable_auto(qconn, lifecycle.current_version_id(qconn, odm.id, rule_key), decided_by="artur")
    # 30 observations of the person's decisions -> shadow (derived, nothing written by code)
    more = synthetic_transfers(qconn, odm.id, zus, months(date(2025, 1, 1), 15, per_month=2))
    decide_category_by_person(qconn, odm.id, more, "ZUS")
    stage, observations, agreed, ready, stored = _state(qconn, rule_key)
    assert (stage, observations, agreed, ready, stored) == ("shadow", 30, 30, False, "hint")
    # 73 agreeing observations -> ready (lower Wilson bound >= 0.95)
    rest = synthetic_transfers(qconn, odm.id, zus, months(date(2023, 1, 1), 22, per_month=2)[:43])
    decide_category_by_person(qconn, odm.id, rest[:42], "ZUS")
    assert _state(qconn, rule_key)[2:4] == (72, False)
    decide_category_by_person(qconn, odm.id, rest[42:], "ZUS")
    assert _state(qconn, rule_key)[2:4] == (73, True)
    current = lifecycle.current_version_id(qconn, odm.id, rule_key)
    lifecycle.enable_auto(qconn, current, decided_by="artur")
    assert _state(qconn, rule_key)[0] == "auto"
    # the automation categorizes a new ZUS transfer, signed with the rule
    new = synthetic_transfers(qconn, odm.id, zus, [date(2026, 11, 3)])
    applied = apply_category_rules(qconn, odm)
    assert applied.applied >= 1
    method, signed = qconn.execute("SELECT method::text, rule_id FROM l3_category_decision WHERE subject_id = %s "
                                   "AND status = 'active'", (new[0],)).fetchone()
    assert method == "rule" and signed == current
    # one correction in auto (the person decides another category) -> suspended with a post-mortem draft
    other = synthetic_transfers(qconn, odm.id, zus, [date(2026, 11, 4)])
    decide_other(qconn, odm.id, "transaction", other[0], decided_by="artur", kind="category", category="Inne")
    rule = lifecycle.rule_row(qconn, current)
    assert rule["stage_text"] == "suspended" and "poprawka" in rule["suspended_reason"]
    assert rule["post_mortem"].startswith("Post-mortem (szkic)") and rule_key in rule["post_mortem"]
    # the automation stops at once (the database refuses a rule outside auto - KS004)
    assert apply_category_rules(qconn, odm).applied == 0
    # resume as a hint; a correction (new pattern) is a new version, the old one stays
    lifecycle.resume(qconn, current, decided_by="artur")
    _, new_id = lifecycle.correct(qconn, current, decided_by="artur",
                                  pattern={"all": [{"field": "counterparty_id", "op": "eq", "value": str(zus)},
                                                   {"field": "title_regex", "op": "regex", "value": "(?i)skladki"}]})
    assert lifecycle.rule_row(qconn, new_id)["version"] == 3 and _state(qconn, rule_key)[1] == 0


def test_revoking_evidence_takes_the_point_back_and_a_rule_decision_revoked_suspends(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    txs = synthetic_transfers(qconn, odm.id, zus, months(date(2025, 1, 1), 40, per_month=2))
    decisions = decide_category_by_person(qconn, odm.id, txs[:6], "ZUS")
    rule_id, rule_key, card_id, _, _ = _candidate(qconn, odm.id, "%ZUS%")
    accept_suggestion(qconn, card_id, decided_by="artur")
    observed = decide_category_by_person(qconn, odm.id, txs[6:80], "ZUS")
    assert _state(qconn, rule_key)[2] == 74
    revoke_decision(qconn, observed[0], "artur", reason="pomyłka")
    assert _state(qconn, rule_key)[2] == 73                            # the point is taken back
    current = lifecycle.current_version_id(qconn, odm.id, rule_key)
    lifecycle.enable_auto(qconn, current, decided_by="artur")
    revoke_decision(qconn, observed[1], "artur")
    # below the threshold of readiness again -> "może ją zawiesić"
    assert lifecycle.rule_row(qconn, current)["stage_text"] == "suspended"
    assert decisions


def test_rejected_candidate_is_never_proposed_again_and_absence_makes_no_rule(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    txs = synthetic_transfers(qconn, odm.id, zus, months(date(2026, 1, 1), 3, per_month=2))
    decide_category_by_person(qconn, odm.id, txs, "ZUS")
    rule_id, rule_key, card_id, _, _ = _candidate(qconn, odm.id, "%ZUS%")
    from kasa.core.decisions import reject_suggestion

    reject_suggestion(qconn, card_id, decided_by="artur", reason="ZUS księgujemy inaczej")
    latest = qconn.execute("SELECT stage::text, suspended_reason FROM l3_rule WHERE rule_key = %s "
                           "ORDER BY version DESC LIMIT 1", (rule_key,)).fetchone()
    assert latest[0] == "suspended" and latest[1].startswith("odrzucona")
    more = synthetic_transfers(qconn, odm.id, zus, months(date(2026, 5, 1), 2, per_month=3))
    decide_category_by_person(qconn, odm.id, more, "ZUS")
    report = mine.mine(qconn, odm)
    assert report.created == [] and report.skipped_decided >= 1
    # nothing about Orange was decided in two months -> no rule (no negative rule from absence)
    assert qconn.execute("SELECT count(*) FROM l3_rule WHERE question LIKE '%%Orange%%'").fetchone() == (0,)


def test_simulate_shows_what_the_rule_would_do_on_the_history(qconn, odm):
    zus = counterparty(qconn, odm.id, "Zakład Ubezpieczeń%")
    txs = synthetic_transfers(qconn, odm.id, zus, months(date(2026, 1, 1), 3, per_month=2))
    decide_category_by_person(qconn, odm.id, txs, "ZUS")
    rule_id, rule_key, *_ = _candidate(qconn, odm.id, "%ZUS%")
    report = simulate(qconn, rule_id)
    # the 6 decided transfers agree; the October ZUS transfer has no category yet
    assert report.hits == 7 and report.agreed == 6 and report.undecided == 1 and report.disagreed == 0
    assert "Symulacja" in report.summary()
