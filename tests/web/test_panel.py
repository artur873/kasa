"""The panel: create a set, a card without any proposal, labeling (HTML and API), sentinel, export, access."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from kasa.core import control_sets as cs
from tests.semantics.helpers import ground_truth

from .conftest import app_on

FORBIDDEN = ("propozycja", "propozycje", "sugestia", "sugestie", "score", "sentinel", "wartownik", "pewnoś",
             "intent", "mock-heuristic")


def _clean(html: str) -> list[str]:
    lower = html.lower()
    return [word for word in FORBIDDEN if word in lower]


def _create(client: TestClient, kind: str = "transaction", size: int = 600) -> str:
    response = client.post("/control-sets", data={"company": "odm", "kind": kind, "size": str(size)},
                           follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def test_card_shows_facts_and_no_proposal(client, conn, loaded):
    from kasa.core.semantics.run import run_semantics

    run_semantics(conn, loaded)                       # readings exist - and still are not shown
    page = client.get("/control-sets")
    assert page.status_code == 200 and "Zbiory kontrolne" in page.text and not _clean(page.text)
    set_id = _create(client)
    card = client.get(f"/control-sets/{set_id}/card")
    assert card.status_code == 200
    assert "1/83" in card.text and "Status końcowy" in card.text and 'name="viewport"' in card.text
    assert not _clean(card.text), _clean(card.text)
    item = conn.execute("SELECT subject_id FROM control_set_item WHERE set_id = %s AND order_no = 1",
                        (set_id,)).fetchone()[0]
    title, counterparty = conn.execute(
        "SELECT title_raw, counterparty_name_raw FROM l1_bank_transaction WHERE id = %s", (item,)).fetchone()
    assert title.split(" | ")[0] in card.text.replace("&#39;", "'")      # plain data for the owner
    if counterparty:
        assert counterparty in card.text
    # every card of the set is clean (no reading, no rule, no ranking)
    for order_no in range(1, 84):
        item_id = conn.execute("SELECT id FROM control_set_item WHERE set_id = %s AND order_no = %s",
                               (set_id, order_no)).fetchone()[0]
        html = client.get(f"/control-sets/{set_id}/items/{item_id}").text
        assert not _clean(html), (order_no, _clean(html))


def test_labeling_through_the_page_and_the_api(client, conn):
    set_id = _create(client)
    html = client.get(f"/control-sets/{set_id}/card").text
    action = re.search(r'method="post" action="(/control-sets/[^"]+/items/[^"]+)"', html).group(1)
    refused = client.post(action, data={"status": "matched"}, follow_redirects=False)
    assert refused.status_code == 422 and "wymaga dokumentu" in refused.text
    saved = client.post(action, data={"status": "no_document", "subtype": "fee", "note": "test"},
                        follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"].endswith("after=1")
    assert "2/83" in client.get(saved.headers["location"]).text
    item_id = action.rsplit("/", 1)[1]
    label, by, duration = conn.execute(
        "SELECT label, labeled_by, duration_ms FROM control_set_item WHERE id = %s", (item_id,)).fetchone()
    assert label == {"status": "no_document", "subtype": "fee", "documents": [], "other_document": False,
                     "note": "test"}
    assert by == "artur" and duration is not None and duration >= 0
    # the API: next card (no sentinel flag, no expected answer), documents of the counterparty, a label
    nxt = client.get(f"/api/control-sets/{set_id}/next").json()
    assert nxt["done"] is False and set(nxt["item"]) == {"id", "order_no", "subject_type", "subject_id", "label",
                                                          "labeled_at", "progress"}
    assert "sentinel" not in str(nxt).lower()
    item = nxt["item"]["id"]
    search = client.get(f"/api/control-sets/{set_id}/items/{item}/documents", params={"q": "FV 5/09/2026"}).json()
    assert search and all("score" not in d for d in search)
    doc = search[0]
    labeled = client.post(f"/api/control-sets/{set_id}/items/{item}/label",
                          json={"label": {"status": "matched", "documents": [{"type": doc["type"], "id": doc["id"]}]}})
    assert labeled.status_code == 200 and labeled.json()["status"]["labeled"] == 2
    bad = client.post(f"/api/control-sets/{set_id}/items/{item}/label", json={"label": {"status": "approve"}})
    assert bad.status_code == 422
    assert client.get(f"/api/control-sets/{set_id}").json()["progress"] == "2/83"


def test_search_by_amount_and_open_invoices_of_the_counterparty(client, conn, loaded):
    set_id = _create(client)
    item_id = conn.execute(
        """
        SELECT i.id FROM control_set_item i JOIN l1_bank_transaction t ON t.id = i.subject_id
         WHERE i.set_id = %s AND t.entry_reference = '202610289276403991'
        """,
        (set_id,),
    ).fetchone()[0]                                     # Scena Pro: one transfer for three invoices
    html = client.get(f"/control-sets/{set_id}/items/{item_id}").text
    for number in ("SP/2026/10/081", "SP/2026/10/093", "SP/2026/10/101"):
        assert number in html
    by_amount = client.get(f"/control-sets/{set_id}/items/{item_id}", params={"q": "400,00"}).text
    assert "(arkusz)" in by_amount                       # sheet rows of 400,00 can be picked too
    # the page lists them in a neutral order and preselects nothing
    assert not re.findall(r"<input[^>]*\schecked[\s>]", html)


def test_cost_card_and_sentinel_flow_to_export(client, conn, loaded):
    set_id = _create(client, kind="cost", size=3)
    html = client.get(f"/control-sets/{set_id}/card").text
    assert "Kategoria" in html and "UNASSIGNED" in html and "Realizacja eventu / Logistyczne" in html and not _clean(html)
    items = conn.execute("SELECT id, is_sentinel, sentinel_expected FROM control_set_item WHERE set_id = %s "
                         "ORDER BY order_no", (set_id,)).fetchall()
    assert sum(1 for i in items if i[1]) == 1
    for item_id, is_sentinel, expected in items:
        form = {"category": "Inne / Inne / do wyjaśnienia", "unassigned": "1"}
        if is_sentinel and expected.get("cost_object"):
            form = {"category": "Inne / Inne / do wyjaśnienia", "cost_object": expected["cost_object"]}
        elif is_sentinel and expected.get("split"):
            form = {"category": "Inne / Inne / do wyjaśnienia", "split": "\n".join(f"{s['name']}; {float(s['share']) * 100:.4f}%"
                                                           for s in expected["split"])}
        response = client.post(f"/control-sets/{set_id}/items/{item_id}", data=form, follow_redirects=False)
        assert response.status_code == 303, response.text
    done = client.get(f"/control-sets/{set_id}/done")
    assert "Zbiór zakończony" in done.text and "zgodnych 1 z 1" in done.text
    exported = client.get(f"/api/control-sets/{set_id}/export").json()
    assert exported["status"]["sentinel_results"] == {"passed": 1, "failed": 0}
    assert len(exported["costs"]) == 3 and all(c["final_allocation"] for c in exported["costs"])


def test_creating_through_the_api_and_listing(client):
    created = client.post("/api/control-sets", json={"company": "odm", "kind": "transaction", "size": 20, "seed": 9})
    assert created.status_code == 201 and created.json()["total"] == 20 and created.json()["sentinels"] == 1
    listed = client.get("/api/control-sets", params={"company": "odm"}).json()
    assert [s["total"] for s in listed] == [20]
    assert client.post("/api/control-sets", json={"company": "odm", "kind": "x", "size": 1}).status_code == 422
    assert client.get("/api/control-sets/00000000-0000-0000-0000-000000000000").status_code == 404


def test_a_token_protects_the_panel_outside_localhost(conn, loaded):
    with TestClient(app_on(conn, token="sekret", company="odm")) as protected:
        assert protected.get("/control-sets").status_code == 401
        assert protected.get("/api/control-sets").status_code == 401
        first = protected.get("/control-sets", params={"token": "sekret"})
        assert first.status_code == 200 and protected.cookies.get("kasa_token") == "sekret"
        assert protected.get("/control-sets").status_code == 200           # the cookie is enough now
        assert protected.get("/api/control-sets", headers={"Authorization": "Bearer sekret"}).status_code == 200
    assert cs.FAST_LABEL_MS == 800 and ground_truth()
