"""The dashboard (briefing, step 8): cards "metric@version + dimensions + filters" from the extension's layout,
each with the day of the data, the version, the share in suggestions and the truth checks - and no formula."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from kasa.core.metrics import registry
from kasa.web.app import create_app
from kasa.web.routers.dashboard import CardSpec, load_layout, parse_layout


@pytest.fixture
def web(mconn):
    @contextmanager
    def factory():
        with mconn.transaction():
            yield mconn

    with TestClient(create_app(connection_factory=factory, user="artur", company="odm", token="")) as client:
        yield client


def test_layout_is_metrics_with_versions_and_nothing_that_computes():
    layout = load_layout("odm")
    assert len(layout.cards) >= 10
    assert all("@" in card.metric for card in layout.cards)
    assert set(CardSpec.model_fields) == {"metric", "title", "dimensions", "filters", "period", "chart", "bar_field",
                                          "label_field"}
    for bad in ("formula: revenue * 2", "sql: SELECT 1", "expression: x"):
        with pytest.raises(ValueError, match="zły układ"):
            parse_layout(f"cards:\n  - metric: receivables_open@1\n    title: x\n    {bad}\n")
    with pytest.raises(ValueError, match="klucz@wersja"):
        parse_layout("cards:\n  - metric: receivables_open\n    title: x\n")


def test_dashboard_page_shows_the_metadata_of_every_card(web):
    page = web.get("/dashboard")
    assert page.status_code == 200
    html = page.text
    for ref in ("receivables_open@1", "margin_accrual@3", "account_balances@1", "costs_without_document@1"):
        assert ref in html
    assert "dane na 2026-10-31" in html and "w sugestiach:" in html and "kontrole prawdy: zielone" in html
    assert "75 565,00 zł" in html and "<svg" in html
    # nothing to type a formula into: the only input is the month
    assert html.count("<input") == 1 and 'type="month"' in html and "<textarea" not in html
    assert "formu" not in html.lower().replace("formuły nie da się tu wpisać", "")


def test_dashboard_and_agent_read_the_same_registry(web, mconn, modm):
    data = web.get("/api/dashboard").json()
    for card in data["cards"]:
        key, version = card["ref"].split("@")
        spec = card["spec"]
        cls = registry.get(key, int(version))
        same = registry.compute(mconn, modm.id, key, version=int(version),
                                period=None if cls.period_mode in ("as_of", "none") else "2026-10",
                                dimensions=spec["dimensions"], filters=spec["filters"]).as_dict()
        assert card["result"]["value"] == same["value"], card["ref"]
        assert card["result"]["unconfirmed_share"] == same["unconfirmed_share"], card["ref"]
    assert web.get("/api/dashboard?month=październik").status_code == 422
