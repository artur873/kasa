"""Audit of the automation: 2% a month (min. 20), a confirmed error suspends the rule with a post-mortem."""

from __future__ import annotations

from kasa.core.queue import actions, build
from kasa.core.rules import audit, lifecycle


def test_sample_two_percent_min_twenty_reproducible_and_once_per_month(qconn, odm):
    population = audit.population(qconn, odm.id, "2026-10")
    assert len(population) == 41                    # the 41 matches of the automation in October (objects not approved)
    assert audit.sample_size(41) == 20 and audit.sample_size(1500) == 30 and audit.sample_size(7) == 7
    report = audit.sample(qconn, odm, "2026-10", seed=7)
    assert report.population == 41 and report.sample_size == 20 and len(report.drawn) == 20
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


def test_audit_card_shows_the_evidence_and_the_alternatives_of_the_moment_of_the_decision(qconn, odm):
    """Review of 4C/5, point 6: the person judges the choice, not only its result - the facts the automation
    had (``l3_decision_log.evidence``) and every other proposal it rejected then (``resolved_by_decision``)."""
    audit.sample(qconn, odm, "2026-10", seed=3)
    build.build(qconn, odm)
    cards = [i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "audit"]
    shown = []
    for item in cards:
        card = actions.card(qconn, item["id"])["audit"]
        decision, evidence = qconn.execute(
            "SELECT d.id, d.evidence FROM l2_audit_sample s JOIN l3_decision_log d ON d.id = s.decision_id "
            "WHERE s.id = %s", (item["audit_sample_id"],)).fetchone()
        assert card["facts"] and card["facts"] == evidence["facts"]
        rejected = qconn.execute("SELECT count(*) FROM l2_suggestion WHERE resolved_by_decision = %s AND "
                                 "status = 'rejected'", (decision,)).fetchone()[0]
        assert len(card["alternatives"]) == rejected
        if card["alternatives"]:
            shown.append((item, card))
    assert shown, "w próbce jest decyzja automatu, która odrzuciła inne propozycje"
    item, card = shown[0]
    assert all(a["label"] and a["badge_pl"] for a in card["alternatives"])
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from kasa.web.app import create_app

    @contextmanager
    def factory():
        with qconn.transaction():
            yield qconn

    with TestClient(create_app(connection_factory=factory, user="artur", company="odm")) as client:
        page = client.get(f"/queue/items/{item['id']}").text
    assert "Dowody z chwili decyzji" in page and "Inne propozycje, które automat wtedy odrzucił" in page
    assert card["facts"][0] in page and card["alternatives"][0]["label"].split(" · ")[0] in page


def test_too_fast_on_a_card_without_a_badge_is_an_alarm(qconn, odm):
    """Review of 4C/5, point 12: under 1.5 s on a card without a badge (candidate, rule, audit) - as under 0.8 s
    on a "low" card."""
    from kasa.core.queue import state

    audit.sample(qconn, odm, "2026-10", seed=3)
    build.build(qconn, odm)
    fast, slow = [i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "audit"][:2]
    for item in (fast, slow):
        actions.card(qconn, item["id"])
    qconn.execute("UPDATE queue_state SET shown_at = shown_at - interval '1 second' WHERE id = %s", (fast["id"],))
    qconn.execute("UPDATE queue_state SET shown_at = shown_at - interval '2 seconds' WHERE id = %s", (slow["id"],))
    for item in (fast, slow):
        actions.answer(qconn, item["id"], "confirmed", decided_by="artur")
    assert state.get(qconn, fast["id"])["fast_alarm"] is True           # 1 s on a card without a badge
    assert state.get(qconn, slow["id"])["fast_alarm"] is False          # 2 s
    stats = actions.stats(qconn, odm.id)
    assert stats["fast_alarms"] == 1 and any("1,5 s" in w for w in stats["warnings"])
