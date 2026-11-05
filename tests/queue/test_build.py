"""Building the queue: which cards, in which order and why; sentinels; first-shown time; deferral."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from kasa.core.queue import actions, build, state
from tests.queue.pipeline import answer_match_cards


def test_queue_has_every_kind_of_open_work_ordered_by_what_blocks_the_metrics(qconn, odm):
    items = actions.list_items(qconn, odm.id)
    kinds = {}
    for item in items:
        kinds[item["card_kind"]] = kinds.get(item["card_kind"], 0) + 1
    # 39 transfers for a person (4A; 4 of them an invoice number with a counterparty agreeing only by name),
    # 5 cost object candidates (4B), allocation proposals, 1 sensitive title
    assert kinds["match"] == 39 and kinds["cost_object_candidate"] == 5 and kinds["sensitive"] == 1
    assert kinds["allocation"] > 50
    # no decision of the person yet: no sentinel (they repeat the person's own decisions older than 7 days)
    assert "sentinel" not in kinds
    ordered = [i for i in items if i["card_kind"] != "sentinel"]
    # candidates block the allocations -> first; then the largest outflows without a document
    assert [i["card_kind"] for i in ordered[:5]] == ["cost_object_candidate"] * 5
    assert ordered[5]["card_kind"] == "match" and ordered[5]["rank_reason"].startswith("duży wypływ bez dokumentu")
    assert all(a["priority"] >= b["priority"] for a, b in zip(ordered, ordered[1:], strict=False))
    # the reason is written in Polish for every card
    assert all(i["rank_reason"] and " " in i["rank_reason"] for i in items)
    receivable = [i for i in items if i["rank_reason"].startswith("przeterminowana należność")]
    assert receivable, "Fala: częściowa zapłata FV 4/09/2026 po terminie"


def test_rebuild_is_idempotent_and_closes_what_was_decided_elsewhere(qconn, odm):
    before = {(i["card_kind"], i["subject_id"]): i["id"] for i in actions.list_items(qconn, odm.id)}
    report = build.build(qconn, odm)
    after = {(i["card_kind"], i["subject_id"]): i["id"] for i in actions.list_items(qconn, odm.id)}
    assert before == after and report.created == 0 and report.sentinels_added == 0
    # a decision outside the queue (CLI) closes the card at the next build
    match = next(i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "match")
    from kasa.core.matching.decide import apply_match

    apply_match(qconn, match["suggestion_ids"][0], decided_by="artur", method="human")
    report = build.build(qconn, odm)
    assert report.closed == 1
    assert state.get(qconn, match["id"])["outcome"] == "resolved_elsewhere"


def test_sentinels_repeat_the_persons_own_older_decisions_only(qconn, odm):
    """Review of 4C/5, point 5: never a transfer the automation closed, never a control-set label - a repeat of
    the person's own decision older than 7 days; hidden from the history and the agent until answered."""
    decided = answer_match_cards(qconn, odm, 12)
    assert len(decided) == 12
    build.build(qconn, odm)                                        # the decisions are fresh: still no sentinel
    assert not [i for i in actions.list_items(qconn, odm.id) if i["card_kind"] == "sentinel"]
    # the clicks with their date moved back by 8 days (the build looks from 8 days later)
    report = build.build(qconn, odm, now=datetime.now(timezone.utc) + timedelta(days=8))
    items = actions.list_items(qconn, odm.id, include_deferred=False)
    sentinels = [i for i in items if i["card_kind"] == "sentinel"]
    regular = len(items) - len(sentinels)
    assert report.sentinels_added == len(sentinels) == min(len(decided), max(1, round(regular / build.SENTINEL_EVERY)))
    assert {s["subject_id"] for s in sentinels} <= set(decided)
    known = qconn.execute("SELECT expected, expected_source, source_ref FROM queue_sentinel WHERE company_id = %s",
                          (odm.id,)).fetchall()
    assert known and all(k[1] == "decision" and k[0]["final_status"] for k in known)
    sources = {k[2] for k in known}
    assert qconn.execute("SELECT count(*) FROM l3_decision_log WHERE id = ANY(%s) AND method = 'human' AND "
                         "decided_at < now() + interval '1 day'", (list(sources),)).fetchone()[0] == len(sources)
    positions = sorted(i["position"] for i in sentinels)
    assert all(abs(p - round((n + 1) * len(items) / (len(sentinels) + 1))) <= len(sentinels) + 1
               for n, p in enumerate(positions))
    card = actions.card(qconn, sentinels[0]["id"])
    assert card["kind"] == "match" and "sentinel" not in str({k: v for k, v in card.items() if k != "id"})
    # the decision it repeats is not in the history until the sentinel is answered
    history = {h["id"] for h in actions.history(qconn, odm.id, limit=500, include_auto=True)}
    assert not (sources & history) and len(history) >= len(decided) - len(sources)
    # ... and the agent sees neither the card nor its known answer
    qconn.execute("SET LOCAL ROLE kasa_agent_ro")
    qconn.execute("SELECT set_config('kasa.company_id', %s, true)", (str(odm.id),))
    assert qconn.execute("SELECT count(*) FROM views.queue WHERE card_kind = 'sentinel'").fetchone() == (0,)
    assert qconn.execute("SELECT count(*) FROM evidence.queue_state WHERE card_kind = 'sentinel' OR sentinel_id "
                         "IS NOT NULL").fetchone() == (0,)
    qconn.execute("RESET ROLE")
    # answered: the decision is back in the history (and the card is gone from the queue)
    actions.answer(qconn, sentinels[0]["id"], "accept", decided_by="artur")
    history = {h["id"] for h in actions.history(qconn, odm.id, limit=500)}
    answered = qconn.execute("SELECT source_ref FROM queue_sentinel WHERE id = %s",
                             (sentinels[0]["sentinel_id"],)).fetchone()[0]
    assert answered in history and not (sources - {answered}) & history
    # the decision a waiting sentinel repeats is revoked: the sentinel leaves the queue unanswered
    if len(sentinels) > 1:
        source = qconn.execute("SELECT source_ref FROM queue_sentinel WHERE id = %s",
                               (sentinels[1]["sentinel_id"],)).fetchone()[0]
        actions.undo(qconn, source, decided_by="artur")
        build.build(qconn, odm, now=datetime.now(timezone.utc) + timedelta(days=8))
        assert state.get(qconn, sentinels[1]["id"])["outcome"] == "resolved_elsewhere"


def test_first_shown_time_is_kept_and_defer_moves_the_card_to_the_end(qconn, odm):
    first = actions.next_item(qconn, odm.id)
    shown = first["shown_at"]
    again = actions.next_item(qconn, odm.id)
    assert again["id"] == first["id"] and again["shown_at"] == shown          # a reload never resets the clock
    actions.answer(qconn, first["id"], "defer", decided_by="artur", until=date.today() + timedelta(days=3))
    build.build(qconn, odm)
    moved = state.get(qconn, first["id"])
    assert moved["deferred_until"] is not None and moved["closed_at"] is None
    assert actions.next_item(qconn, odm.id, mark_shown=False)["id"] != first["id"]
    last = max(i["position"] for i in actions.list_items(qconn, odm.id))
    assert moved["position"] == last
