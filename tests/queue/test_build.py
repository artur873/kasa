"""Building the queue: which cards, in which order and why; sentinels; first-shown time; deferral."""

from __future__ import annotations

from datetime import date, timedelta

from kasa.core.queue import actions, build, state


def test_queue_has_every_kind_of_open_work_ordered_by_what_blocks_the_metrics(qconn, odm):
    items = actions.list_items(qconn, odm.id)
    kinds = {}
    for item in items:
        kinds[item["card_kind"]] = kinds.get(item["card_kind"], 0) + 1
    # 35 transfers for a person (4A), 5 cost object candidates (4B), allocation proposals, 1 sensitive title
    assert kinds["match"] == 35 and kinds["cost_object_candidate"] == 5 and kinds["sensitive"] == 1
    assert kinds["allocation"] > 50 and kinds["sentinel"] >= 1
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


def test_sentinel_every_fifty_cards_looks_like_any_card(qconn, odm):
    items = actions.list_items(qconn, odm.id, include_deferred=False)
    sentinels = [i for i in items if i["card_kind"] == "sentinel"]
    regular = len(items) - len(sentinels)
    assert len(sentinels) == max(1, round(regular / build.SENTINEL_EVERY))
    positions = sorted(i["position"] for i in sentinels)
    assert all(abs(p - round((n + 1) * len(items) / (len(sentinels) + 1))) <= len(sentinels) + 1
               for n, p in enumerate(positions))
    card = actions.card(qconn, sentinels[0]["id"])
    assert card["kind"] == "match" and "sentinel" not in str({k: v for k, v in card.items() if k != "id"})
    known = qconn.execute("SELECT expected, expected_source FROM queue_sentinel WHERE id = %s",
                          (sentinels[0]["sentinel_id"],)).fetchone()
    assert known[0]["final_status"] and known[1] in ("decision", "control_set")


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
