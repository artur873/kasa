"""What a card shows: facts, 1-3 proposals with evidence, a badge with precedents - never a percentage."""

from __future__ import annotations

import json
import re

from kasa.core.queue import actions, cards


def _card_of(conn, company_id, kind, predicate=lambda c: True):
    for item in actions.list_items(conn, company_id):
        if item["card_kind"] == kind:
            card = actions.card(conn, item["id"], mark_shown=False)
            if predicate(card):
                return item, card
    raise AssertionError(f"brak karty {kind}")


def test_match_card_facts_badge_precedents_and_no_percentage(qconn, odm):
    item, card = _card_of(qconn, odm.id, "match", lambda c: c["subject"]["counterparty"].startswith("Agencja Fala"))
    s = card["subject"]
    assert s["amount"].endswith("zł") and s["date"] and s["title"] and s["counterparty"]
    assert 1 <= len(card["proposals"]) <= 3
    top = card["proposals"][0]
    assert top["facts"] and top["badge"] in ("high", "medium", "low") and top["badge_pl"]
    assert "FV 4/09/2026" in top["label"]
    text = json.dumps(card, default=str, ensure_ascii=False)
    assert "score" not in text and "confidence" not in text
    assert not re.search(r"\d+([.,]\d+)?\s?%", " ".join(p["label"] + " ".join(p["facts"]) for p in card["proposals"]))


def test_no_candidate_first_proposal_is_no_document_and_narration_is_under_details(qconn, odm):
    _, card = _card_of(qconn, odm.id, "match", lambda c: c["subject"]["counterparty"] == "Anna Mazur"
                       and "Wynagrodzenie" in c["subject"]["title"])
    assert card["proposals"][0]["label"].startswith("bez dokumentu: wynagrodzenie")
    assert card["details"].get("narration")


def test_tie_card_lists_every_equivalent_invoice(qconn, odm):
    """Kadr Studio: 4 500 zł, two invoices of 4 500 open - and a third one added: all three on the card."""
    item, card = _card_of(qconn, odm.id, "match", lambda c: c["subject"]["title"] == "zaplata")
    assert card["proposals"][0]["label"].startswith("niejednoznaczne")
    assert len(card["tie_documents"]) == 2
    doc = qconn.execute("SELECT * FROM l1_document WHERE number_raw = 'FV 14/2026' AND superseded_by IS NULL"
                        ).fetchone()
    assert doc
    qconn.execute(
        """
        INSERT INTO l1_document (company_id, direction, number_raw, number_normalized, kind, issue_date, due_date,
                                 seller_nip, buyer_nip, seller_name, currency, gross_minor, gross_pln_minor,
                                 counterparty_id, source, method, created_by)
        SELECT company_id, direction, 'FV 15/2026', 'FV152026', kind, issue_date, due_date, seller_nip, buyer_nip,
               seller_name, currency, gross_minor, gross_pln_minor, counterparty_id, 'test', 'code', 'pytest'
          FROM l1_document WHERE number_raw = 'FV 14/2026' AND superseded_by IS NULL
        """
    )
    again = actions.card(qconn, item["id"], mark_shown=False)
    assert len(again["tie_documents"]) == 3 and "FV 15/2026" in {d["number"] for d in again["tie_documents"]}


def test_candidate_card_has_records_weakest_link_revenue_and_costs(qconn, odm):
    _, card = _card_of(qconn, odm.id, "cost_object_candidate", lambda c: c["candidate"]["name"] == "Gala ACME 2026")
    c = card["candidate"]
    assert c["client"].startswith("ACME") and c["city"] == "Kraków" and c["date_start"] == "2026-10-08"
    assert len(c["members"]) == 29 and c["members"][0]["anchor"] and all(m["member_id"] for m in c["members"])
    assert {m["type"] for m in c["members"]} >= {"pozycja faktury", "wiersz arkusza", "przelew"}
    assert c["weakest_link"] and c["revenue"] == "90 000,00 zł" and c["cost"].endswith("zł")
    assert len(c["others"]) == 4


def test_sensitive_card_never_shows_a_model_narration(qconn, odm):
    _, card = _card_of(qconn, odm.id, "sensitive")
    assert card["subject"]["sensitive"] and "narration" not in card["details"]
    assert card["proposals"][0]["label"] == "nadaj kategorię"


def test_proposal_labels(qconn):
    assert cards.proposal_label(qconn, "match", {"final_status": "no_document", "subtype": "zus"}, {}) == \
        "bez dokumentu: ZUS"
    assert cards.proposal_label(qconn, "match", {"final_status": "own_transfer"}, {}) == "transfer własny"
    assert cards.proposal_label(qconn, "allocation", {"shares": [
        {"cost_object_name": "Gdańsk", "share": 0.2}, {"cost_object_name": "Łódź", "share": 0.8}]},
        {"partial": True}) == "Gdańsk 20% · Łódź 80% (częściowa - uzupełnij)"
