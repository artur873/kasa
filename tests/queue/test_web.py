"""The queue on the phone (HTML) and its JSON API: one card, big buttons, toast with undo, history, KPI."""

from __future__ import annotations

import re
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from kasa.web.app import create_app


@pytest.fixture
def client(qconn):
    @contextmanager
    def factory():
        with qconn.transaction():
            yield qconn

    with TestClient(create_app(connection_factory=factory, user="artur", company="odm")) as test_client:
        yield test_client


def _text(html: str) -> str:
    body = html.split("<main>", 1)[-1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_home_is_the_queue_one_card_big_buttons_no_percentage(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/queue"
    page = client.get("/queue")
    assert page.status_code == 200
    text = _text(page.text)
    assert "kandydat na obiekt kosztowy" in text and "Zatwierdź" in text and "Podziel" in text
    assert "Połącz z" in text and "Na pewno różne" in text and "Odrzuć" in text and "Odłóż" in text
    assert not re.search(r"\d+[.,]\d+\s?%|\b0[.,]9\d+\b", text)


def test_accept_toast_undo_history_and_stats(client, qconn):
    items = client.get("/api/queue").json()
    match = next(i for i in items if i["card_kind"] == "match")
    page = client.get(f"/queue/items/{match['id']}")
    text = _text(page.text)
    assert "Akceptuj" in text and "Inne…" in text and "Odrzuć" in text and "Odłóż" in text and "odznaka" in text
    saved = client.post(f"/queue/items/{match['id']}/answer", data={"action": "accept"}, follow_redirects=False)
    assert saved.status_code == 303
    query = parse_qs(urlparse(saved.headers["location"]).query)
    assert query["undo"] and query["until"]
    toast = client.get(saved.headers["location"])
    assert "Cofnij" in toast.text and 'id="toast"' in toast.text and "setTimeout" in toast.text
    undone = client.post("/queue/undo", data={"decision": query["undo"][0]}, follow_redirects=False)
    assert undone.status_code == 303 and "Cofni" in undone.headers["location"].replace("%C4%99", "ę")
    history = client.get("/queue/history")
    assert history.status_code == 200 and "cofnięta" in history.text
    stats = client.get("/queue/stats")
    assert "min" in stats.text and "Błędy ważone kwotą" in stats.text and "procent obsłużony przez AI" in stats.text


def test_other_form_requires_subtype_for_no_document(client):
    items = client.get("/api/queue").json()
    match = next(i for i in items if i["card_kind"] == "match")
    form = client.get(f"/queue/items/{match['id']}/other")
    assert form.status_code == 200 and "Rodzaj (obowiązkowy)" in form.text
    refused = client.post(f"/queue/items/{match['id']}/other", data={"kind": "no_document"}, follow_redirects=False)
    assert "error=" in refused.headers["location"]
    saved = client.post(f"/queue/items/{match['id']}/other", data={"kind": "no_document", "subtype": "other"},
                        follow_redirects=False)
    assert saved.headers["location"].startswith("/queue?company=odm&msg=")


def test_api_for_every_action(client):
    nxt = client.get("/api/queue/next").json()
    assert nxt["done"] is False and nxt["card"]["kind"] == "cost_object_candidate"
    assert "score" not in str(nxt["card"])
    items = client.get("/api/queue").json()
    match = [i for i in items if i["card_kind"] == "match"]
    card = client.get(f"/api/queue/items/{match[0]['id']}").json()
    accepted = client.post(f"/api/queue/items/{match[0]['id']}/accept",
                           json={"suggestion_id": card["proposals"][0]["suggestion_id"], "reason": "test"}).json()
    assert accepted["decision_ids"] and accepted["undo_until"]
    assert client.post(f"/api/queue/items/{match[1]['id']}/defer", json={}).json()["message"].startswith("Odłożone")
    rejected = client.post(f"/api/queue/items/{match[2]['id']}/reject", json={})
    assert rejected.status_code == 200
    bad = client.post(f"/api/queue/items/{match[3]['id']}/other", json={"kind": "no_document"})
    assert bad.status_code == 422 and "podtyp" in bad.json()["detail"]
    ok = client.post(f"/api/queue/items/{match[3]['id']}/other", json={"kind": "to_clarify"})
    assert ok.status_code == 200
    docs = client.get(f"/api/queue/items/{match[4]['id']}/documents", params={"q": "FV"})
    assert docs.status_code == 200
    undo = client.post("/api/queue/undo", json={"decision_id": accepted["decision_ids"][0]})
    assert undo.json()["revoke_ids"]
    assert client.post("/api/queue/undo", json={"decision_id": accepted["decision_ids"][0]}).status_code == 422
    history = client.get("/api/queue/history").json()
    assert any(h["kind"] == "revoke" for h in history) and any(h["revoked_by"] for h in history)
    stats = client.get("/api/queue/stats").json()
    assert {"work_minutes", "errors_weighted_minor", "sentinels", "audit", "fast_alarms"} <= set(stats)
    rebuilt = client.post("/api/queue/build", json={}).json()
    assert rebuilt["items"] > 0
    assert client.get("/api/docs").status_code == 200


def test_queue_is_protected_by_the_token_outside_localhost(qconn):
    @contextmanager
    def factory():
        with qconn.transaction():
            yield qconn

    with TestClient(create_app(connection_factory=factory, user="artur", company="odm", token="sekret")) as c:
        assert c.get("/queue").status_code == 401 and c.get("/api/queue").status_code == 401
        assert c.get("/queue", params={"token": "sekret"}).status_code == 200
