"""Audit of the automation: 2% a month (min. 20), a confirmed error suspends the rule with a post-mortem."""

from __future__ import annotations

from kasa.core.queue import actions, build
from kasa.core.rules import audit, lifecycle


def test_sample_two_percent_min_twenty_reproducible_and_once_per_month(qconn, odm):
    population = audit.population(qconn, odm.id, "2026-10")
    assert len(population) == 45                    # the 45 matches of the automation in October (objects not approved)
    assert audit.sample_size(45) == 20 and audit.sample_size(1500) == 30 and audit.sample_size(7) == 7
    report = audit.sample(qconn, odm, "2026-10", seed=7)
    assert report.population == 45 and report.sample_size == 20 and len(report.drawn) == 20
    again = audit.sample(qconn, odm, "2026-10", seed=8)
    assert again.existing and again.sample_size == 20
    rows = audit.list_samples(qconn, odm.id, month="2026-10")
    assert len({r["decision_id"] for r in rows}) == 20 and all(r["verdict"] is None for r in rows)
    assert audit.sample(qconn, odm, "2026-09").population == 0     # no automation in September


def test_audit_cards_in_the_queue_confirm_and_error(qconn, odm):
    audit.sample(qconn, odm, "2026-10", seed=3)
    build.build(qconn, odm)
    cards = [i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "audit"]
    assert len(cards) == 20 and all("audyt" in c["rank_reason"] for c in cards)
    card = actions.card(qconn, cards[0]["id"])
    assert card["label"] == "audyt" and card["audit"]["decision"]
    actions.answer(qconn, cards[0]["id"], "confirmed", decided_by="artur")
    # an error: the automatic decision is revoked and its rule suspended with a post-mortem draft
    second = actions.card(qconn, cards[1]["id"])
    sample = qconn.execute("SELECT decision_id, rule_id FROM l2_audit_sample WHERE id = %s",
                           (cards[1]["audit_sample_id"],)).fetchone()
    reply = actions.answer(qconn, cards[1]["id"], "error", decided_by="artur", reason="zła faktura")
    assert reply["decision_ids"] and second
    assert qconn.execute("SELECT count(*) FROM l3_decision_log WHERE reverts_decision_id = %s",
                         (sample[0],)).fetchone() == (1,)
    rule = lifecycle.rule_row(qconn, sample[1])
    assert rule["stage_text"] == "suspended" and "Post-mortem" in rule["post_mortem"]
    stats = actions.stats(qconn, odm.id)
    assert stats["audit"]["errors"] == 1 and stats["audit"]["confirmed"] == 1
    assert stats["errors_weighted_minor"] > 0
