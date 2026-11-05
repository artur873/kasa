"""Gate of phase 5 (docs/FAZY.md): metrics with versions, the agent through MCP, the dashboard from metrics.

A fresh database runs the whole pipeline of phase 4 (``tests/queue/pipeline.py``); then two states:

* "before clicking" (``partial_database``): the person approved the five cost object candidates on their
  cards, the automation of signal A ran, everything else waits in open suggestions;
* "after clicking" (``clicked_database``): the person answered the whole queue as the reference truth says,
  the truth checks of October were recorded.

The questions of ``docs/PYTANIA_AGENTA.md`` are asked through the MCP tools by the in-process test client (no
model) - and checked:

(a) 100% hits against ``ground_truth.json`` on receivables (amounts and ages), cash (change of the balances,
    balances of the accounts) and margin (5 cost objects to the grosz);
(b) 0 numbers "from the head": every amount and percentage of an answer appears in the results of its tool
    calls, and the client has no other way (no database, no metric code - ``tests/agent/test_client.py``);
(c) 100% correct refusals: CIT (refused + metric proposal), pay of a person, "approve all" (no tool),
    bailiff (sensitive: only the queue);
(d) every answer carries the version of the metric, the state of the data and the unconfirmed share -
    "30% of the costs are still in suggestions" before clicking, 0% after;
(e) two paths, one number: the dashboard (owner role) = the agent (role ``kasa_agent_ro``);
(f) the roles of the database refuse what the agent must not do.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import psycopg
import pytest
from fastapi.testclient import TestClient

from kasa.core.agent.client import AgentClient, Answer
from kasa.core.agent.server import build_server, create_agent
from kasa.core.metrics import registry
from kasa.web.app import create_app
from tests.metrics.helpers import clicked_database, minor, partial_database, truth

AMOUNT = re.compile(r"-?\d{1,3}(?: \d{3})*,\d{2} (?:zł|EUR|USD)")
PERCENT = re.compile(r"\d+,\d%")
AS_OF = "2026-10-31"
TRUTH = truth()


def from_tools(answer: Answer) -> list[str]:
    """Numbers of the answer that do not come from its tool results (must be empty)."""
    source = answer.results_json
    return [n for n in AMOUNT.findall(answer.text) + PERCENT.findall(answer.text) if n not in source]


@pytest.fixture(scope="module")
def after_url() -> str:
    return clicked_database()


@pytest.fixture(scope="module")
def before_url() -> str:
    return partial_database()


@pytest.fixture(scope="module")
def agent(after_url):
    return AgentClient(build_server(create_agent("odm", database_url=after_url, client="gate5", model="test")))


@pytest.fixture(scope="module")
def agent_before(before_url):
    return AgentClient(build_server(create_agent("odm", database_url=before_url, client="gate5", model="test")))


@pytest.fixture
def owner(after_url) -> Iterator[psycopg.Connection]:
    with psycopg.connect(after_url) as conn, conn.transaction(force_rollback=True):
        yield conn


def ask(client: AgentClient, question: str) -> Answer:
    answer = client.ask(question)
    assert from_tools(answer) == [], (question, from_tools(answer))
    return answer


def metric_answer(client: AgentClient, question: str) -> tuple[Answer, dict]:
    answer = ask(client, question)
    result = answer.calls[-1].result
    assert not result.get("refused"), (question, result.get("reason"))
    # (d) the metadata of every answer, in the data and in the text
    assert result["metric"]["ref"] in answer.text and "dane na dzień: 2026-10-31" in answer.text
    assert "niepotwierdzone" in answer.text and result["unconfirmed_share_pl"] in answer.text
    assert result["data_state"]["as_of"] == AS_OF and 0 <= result["unconfirmed_share"] <= 1
    return answer, result


# ---------------------------------------------------------------------------
# (a) receivables and cash - questions 1-6 of docs/PYTANIA_AGENTA.md
# ---------------------------------------------------------------------------


def test_q1_who_owes_and_since_when(agent):
    answer, result = metric_answer(agent, f"receivables_open as_of={AS_OF}")
    expected = TRUTH["receivables_open"]
    assert result["value"] == minor(expected["total"]) and result["value_pl"] in answer.text
    got = {r["number"]: r for r in result["values"]}
    assert set(got) == {i["invoice_number"] for i in expected["items"]}
    for item in expected["items"]:
        row = got[item["invoice_number"]]
        assert (row["open_minor"], row["age_days"], row["bucket"], row["due_date_missing"]) == (
            minor(item["open"]), item["age_days"], item["bucket"], item["due_date_missing"]), item["invoice_number"]
        assert row["open_pl"] in answer.text
    assert [r["open_minor"] for r in result["values"]] == sorted((r["open_minor"] for r in result["values"]),
                                                                  reverse=True)


def test_q2_balances_of_the_accounts_and_the_vat_account(agent):
    answer, result = metric_answer(agent, f"account_balances as_of={AS_OF}")
    accounts = {a["iban"]: a for a in TRUTH["accounts"]}
    assert result["value"] == sum(minor(a["closing_balance"]) for a in TRUTH["accounts"])
    for row in result["values"]:
        assert row["balance_minor"] == minor(accounts[row["iban"]]["closing_balance"])
        assert row["balance_pl"] in answer.text
    vat = next(a for a in TRUTH["accounts"] if a["kind"] == "vat")
    assert result["detail"]["vat_minor"] == minor(vat["closing_balance"])


def test_q3_unpaid_sales_invoices_of_october(agent):
    _, result = metric_answer(agent, f"receivables_open as_of={AS_OF} filter.issue_month=2026-10")
    expected = {i["invoice_number"] for i in TRUTH["receivables_open"]["items"] if i["issue_date"].startswith("2026-10")}
    assert {r["number"] for r in result["values"]} == expected


def test_q4_inflows_outflows_and_the_change_of_the_balances(agent):
    answer, result = metric_answer(agent, "cash_flow period=2026-10")
    rows = {r["iban"]: r for r in result["values"]}
    for account in TRUTH["accounts"]:
        assert rows[account["iban"]]["inflows_minor"] == minor(account["sum_credits"])
        assert rows[account["iban"]]["outflows_minor"] == minor(account["sum_debits"])
    change = minor(TRUTH["metrics_expected"]["cash_report_2026_10"]["balance_change"])
    assert result["value"] == result["detail"]["balance_change_minor"] == change
    assert result["detail"]["inflows_minor"] + result["detail"]["outflows_minor"] == change
    assert result["detail"]["own_net_minor"] == 0                       # the difference: own transfers net 0
    assert result["value_pl"] in answer.text


def test_q5_did_acme_pay_the_invoices_of_the_gala(agent):
    found = agent.call("search_documents", query="ACME", direction="sales")
    gala = {i["number"] for i in TRUTH["events"][0]["revenue_items"]}
    documents = {r["number"]: r for r in found["rows"]}
    assert gala <= set(documents)
    invoices = {i["invoice_number"]: i for i in TRUTH["invoices"] if i["direction"] == "sales"}
    for number in gala:
        detail = agent.call("get_document", document_id=documents[number]["id"])
        assert detail["paid_minor"] == minor(invoices[number]["settled_doc_currency"]), number
        refs = {p["entry_reference"] for p in detail["payments"]}
        assert refs == set(invoices[number]["settled_by"]), number
        assert all(p["booking_date"] and p["decided_by"] for p in detail["payments"])


def test_q6_dso_of_the_last_90_days(agent):
    _, result = metric_answer(agent, f"dso_90 as_of={AS_OF}")
    window = (date(2026, 8, 3), date(2026, 10, 31))
    sales = sum(minor(i["gross_pln"]) for i in TRUTH["invoices"] if i["direction"] == "sales"
                and window[0] <= date.fromisoformat(i["issue_date"]) <= window[1])
    sales += sum(minor(d["net"]) + minor(d["vat"]) for d in TRUTH["history_only_documents"] if d["section"] == "sales"
                 and window[0] <= date.fromisoformat(d["issue_date"]) <= window[1])
    assert result["detail"]["sales_90_minor"] == sales
    assert result["value"] == round(minor(TRUTH["receivables_open"]["total"]) / sales * 90, 1)


# ---------------------------------------------------------------------------
# (a) margin and cost objects - questions 7-11
# ---------------------------------------------------------------------------


def test_q7_margin_of_the_gala_since_the_beginning_of_the_year(agent):
    answer, result = metric_answer(agent, 'margin_accrual period=2026 "filter.cost_object=Gala ACME 2026"')
    gala = TRUTH["events"][0]
    assert result["value"] == minor(gala["margin_net_pln"]) and result["unconfirmed_share"] == 0
    assert result["value_pl"] in answer.text and "0,0%" in answer.text


def test_q7_all_five_cost_objects_to_the_grosz(agent):
    _, result = metric_answer(agent, "margin_accrual")
    rows = {r["cost_object"]: r for r in result["values"]}
    for event in TRUTH["events"]:
        row = rows[event["name"]]
        assert (row["revenue_minor"], row["cost_minor"], row["margin_minor"]) == (
            minor(event["revenue_net_pln"]), minor(event["cost_net_pln"]), minor(event["margin_net_pln"])), event["id"]
    assert rows["UNASSIGNED"]["is_unassigned"] is True               # apart, never in the margin of the objects
    cash = agent.call("get_metric", key="cash_contribution", period="2026-10")
    by_name = {r["cost_object"]: r["cash_minor"] for r in cash["values"]}
    for event in TRUTH["events"]:
        assert by_name[event["name"]] == minor(event["cash_contribution_pln"]), event["id"]


def test_q8_most_and_least_profitable_object_of_october(agent):
    _, result = metric_answer(agent, "margin_accrual period=2026-10")
    events = sorted(TRUTH["events"], key=lambda e: minor(e["margin_net_pln"]))
    assert result["detail"]["highest"]["cost_object"] == events[-1]["name"]
    assert result["detail"]["lowest"]["cost_object"] == events[0]["name"]


def test_q9_costs_of_october_not_assigned_yet(agent, agent_before):
    _, after = metric_answer(agent, "allocation_coverage period=2026-10")
    assert after["detail"]["costs"]["not_looked_minor"] == 0
    _, before = metric_answer(agent_before, "allocation_coverage period=2026-10")
    assert before["detail"]["costs"]["not_looked_minor"] > 0
    assert before["detail"]["costs"]["in_suggestions_minor"] > 0


def test_q10_hotels_of_the_roadshow_in_all_three_cities(agent):
    answer = ask(agent, "query_view view=allocations filter.cost_object=Roadshow filter.counterparty=hotel")
    result = answer.calls[-1].result
    expected = sum(minor(c["amount_pln"]) for e in TRUTH["events"] if e["name"].startswith("Roadshow")
                   for c in e["cost_items"] if "hotel" in c["party"].lower())
    assert result["totals"]["amount_sum_minor"] == expected


def test_q11_margin_as_the_office_and_as_the_bank_see_october(agent):
    answer, result = metric_answer(agent, "company_result period=2026-10")
    expected = TRUTH["metrics_expected"]
    cash = expected["cash_report_2026_10"]
    assert result["detail"]["revenue_minor"] == minor(expected["revenue_memorial_net_2026_10"])
    assert result["detail"]["balance_change_minor"] == minor(cash["balance_change"])
    rows = {r["row"]: r for r in result["values"]}
    assert rows["objects"]["cash_minor"] == minor(cash["events"])
    assert rows["unassigned"]["cash_minor"] == minor(cash["unassigned"])
    assert rows["unallocated"]["cash_minor"] == minor(cash["unmatched"])
    assert result["detail"]["memorial_difference_minor"] == result["detail"]["cash_difference_minor"] == 0
    assert rows["control"]["memorial_pl"] in answer.text and rows["control"]["cash_pl"] in answer.text


# ---------------------------------------------------------------------------
# costs and documents - questions 12-15
# ---------------------------------------------------------------------------


def test_q12_outflows_over_100_without_a_document(agent):
    answer, result = metric_answer(agent, "costs_without_document period=2026-10")
    expected = TRUTH["metrics_expected"]["no_document_outflows_over_100"]
    assert result["value"] == -sum(minor(e["amount"]) for e in expected)
    listed = {r.get("entry_reference"): r for r in result["values"]}
    for item in expected:
        if item["category"] not in ("Wynagrodzenia", "Zajęcie komornicze"):
            assert listed[item["entry_reference"]]["amount_minor"] == minor(item["amount"])
    payroll = next(r for r in result["values"] if r.get("group") == "payroll")
    assert payroll["amount_minor"] == sum(minor(e["amount"]) for e in expected if e["category"] == "Wynagrodzenia")
    assert "osoby niewidoczne" in answer.text


def test_q13_monthly_subscriptions(agent):
    answer = ask(agent, "query_view view=documents filter.month=2026-10 filter.direction=purchase "
                        '"filter.counterparty=Google|Anthropic|Notion"')
    result = answer.calls[-1].result
    expected = sum(minor(d["gross_pln"]) for d in TRUTH["email_documents"]
                   if any(v in d["seller"] for v in ("Google", "Anthropic", "Notion")))
    assert result["totals"]["amount_sum_minor"] == expected and result["totals"]["amount_sum_pl"] in answer.text


def test_q14_double_payments_and_refunds_of_the_month(agent):
    answer = ask(agent, "query_view view=transactions filter.related=true filter.month=2026-10")
    refs = {r["entry_reference"] for r in answer.calls[-1].result["rows"]}
    keys = {t["scenario_key"]: t["entry_reference"] for t in TRUTH["transactions"]}
    assert refs == {keys["MB-ORBITA-2"], keys["MB-ORBITA-ZWROT"]}


def test_q15_what_waits_in_the_queue_and_what_blocks_the_numbers(agent, owner):
    answer = ask(agent, "list_queue limit=10")
    summary = answer.calls[-1].result["summary"]
    open_cards = owner.execute("SELECT count(*) FROM queue_state q JOIN company c ON c.id = q.company_id WHERE "
                               "c.slug = 'odm' AND q.closed_at IS NULL AND q.card_kind <> 'sentinel'").fetchone()[0]
    assert summary["value"] == open_cards and "blokuje liczby" in answer.text


# ---------------------------------------------------------------------------
# (c) traps - questions 16-20: refuse or reserve
# ---------------------------------------------------------------------------


def test_q16_cit_is_refused_and_a_metric_is_proposed(agent, after_url):
    with psycopg.connect(after_url) as conn:
        before = conn.execute("SELECT count(*) FROM metric_proposal").fetchone()[0]
    answer = ask(agent, "cit_2026 question=Ile_zapłacimy_CIT_za_2026")
    result = answer.calls[-1].result
    assert answer.refused and result["refused"] and result["proposal_logged"] is True
    assert "value" not in result and not AMOUNT.search(answer.text)
    with psycopg.connect(after_url) as conn:
        assert conn.execute("SELECT count(*) FROM metric_proposal").fetchone()[0] == before + 1


def test_q17_pay_of_a_person_is_not_available(agent):
    answer = ask(agent, "salary_per_person filter.person=Kasia")
    assert answer.refused and "płace per osoba" in answer.text and not AMOUNT.search(answer.text)
    view = agent.call("query_view", view="transactions", filters={"person": "Kasia"})
    assert view["refused"] is True
    # the margin of a cost object is available - of the object, not of a person
    assert not agent.call("get_metric", key="margin_accrual").get("refused")


def test_q18_approve_everything_has_no_tool(agent):
    answer = ask(agent, "zatwierdz_wszystkie_dopasowania")
    assert answer.refused and "nie mam narzędzia" in answer.text
    assert not [t for t in agent.tools() if t.split("_")[0] in ("approve", "accept", "zatwierdz", "pay", "send",
                                                                 "set", "ustaw")]


def test_q19_the_collective_invoice_counts_by_the_approved_split(agent):
    _, result = metric_answer(agent, 'margin_accrual "filter.cost_object=Gala ACME 2026"')
    assert result["value"] == minor(TRUTH["events"][0]["margin_net_pln"])
    split = agent.call("query_view", view="allocations", filters={"label": "FV 10/10/2026"})
    shares = {r["cost_object"]: r["share"] for r in split["rows"]}
    assert set(shares) == {"Roadshow Nordis Gdańsk", "Roadshow Nordis Wrocław", "Roadshow Nordis Łódź"}
    assert sorted(shares.values()) == [0.2, 0.3, 0.5]
    # no way to impose a split of one's own: no such filter, no formula
    assert agent.call("get_metric", key="margin_accrual", filters={"split": "100% Gala"})["refused"] is True


def test_q20_transfers_of_the_bailiff_only_in_the_queue(agent):
    answer = ask(agent, "search_transactions query=komornik")
    assert answer.refused and "kolejce" in answer.text and not AMOUNT.search(answer.text)


# ---------------------------------------------------------------------------
# (d) "30% of the costs are still in suggestions" before clicking, 0% after
# ---------------------------------------------------------------------------


def test_d_unconfirmed_share_before_and_after_the_clicks(agent, agent_before):
    _, before = metric_answer(agent_before, 'margin_accrual "filter.cost_object=Gala ACME 2026"')
    _, after = metric_answer(agent, 'margin_accrual "filter.cost_object=Gala ACME 2026"')
    assert 0 < before["unconfirmed_share"] < 1 and before["detail"]["cost_pending_share"] > 0
    assert after["unconfirmed_share"] == 0 and after["detail"]["cost_pending_share"] == 0
    assert before["value"] != after["value"] == minor(TRUTH["events"][0]["margin_net_pln"])
    assert before["provenance"]["suggestions"] > 0 and after["provenance"]["suggestions"] == 0
    assert after["provenance"]["decisions"] > 0


# ---------------------------------------------------------------------------
# (e) two paths, one number
# ---------------------------------------------------------------------------


def test_e_dashboard_and_agent_give_the_same_numbers(agent, owner):
    @contextmanager
    def factory():
        with owner.transaction():
            yield owner

    with TestClient(create_app(connection_factory=factory, user="artur", company="odm", token="")) as web:
        cards = web.get("/api/dashboard").json()["cards"]
    assert len(cards) >= 10
    for card in cards:
        key, version = card["ref"].split("@")
        cls = registry.get(key, int(version))
        spec = card["spec"]
        via_agent = agent.call("get_metric", key=key, version=int(version),
                               period=None if cls.period_mode in ("as_of", "none") else "2026-10",
                               dimensions=spec["dimensions"] or None, filters=spec["filters"] or None)
        assert via_agent["value"] == card["result"]["value"], card["ref"]
        assert via_agent["unconfirmed_share"] == card["result"]["unconfirmed_share"], card["ref"]
        assert via_agent["metric"]["code_sha256"] == card["result"]["metric"]["code_sha256"], card["ref"]
        numbers = [{k: v for k, v in r.items() if k.endswith("_minor")} for r in via_agent["values"]]
        assert numbers == [{k: v for k, v in r.items() if k.endswith("_minor")} for r in card["result"]["values"]]


# ---------------------------------------------------------------------------
# (f) the roles of the database
# ---------------------------------------------------------------------------


def test_f_roles_refuse_what_the_agent_must_not_do(owner):
    company = owner.execute("SELECT id FROM company WHERE slug = 'odm'").fetchone()[0]
    owner.execute("SET LOCAL ROLE kasa_agent_ro")
    owner.execute("SET LOCAL search_path = evidence, views, metrics, public")
    owner.execute("SELECT set_config('kasa.company_id', %s, true)", (str(company),))
    for query in ("SELECT 1 FROM public.l1_bank_transaction", "SELECT 1 FROM public.pseudonym_key",
                  "INSERT INTO public.agent_call_log (company_id, tool, status) VALUES (gen_random_uuid(), 'x', 'ok')",
                  "SELECT kasa_purge_raw(gen_random_uuid(), 'x')"):
        with pytest.raises(psycopg.Error) as err, owner.transaction():
            owner.execute(query)
        assert err.value.sqlstate == "42501", query
    row = owner.execute("SELECT title FROM views.transactions LIMIT 1").fetchone()[0]
    assert set(row) == {"untrusted_text"}
    with pytest.raises(psycopg.Error) as err, owner.transaction():
        owner.execute("SELECT title_raw FROM views.transactions")
    assert err.value.sqlstate == "42703"
