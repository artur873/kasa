"""The starting metrics (briefing, step 8) on the clicked pipeline database = the reference truth, to the grosz."""

from __future__ import annotations

from collections import Counter

from kasa.core.decisions import revoke_decision
from kasa.core.metrics import registry
from tests.metrics.helpers import minor, truth

AS_OF = "2026-10-31"


def compute(conn, company, key, **kwargs):
    return registry.compute(conn, company.id, key, **kwargs)


def test_receivables_open_and_ageing_equal_the_ground_truth(mconn, modm):
    result = compute(mconn, modm, "receivables_open", as_of=AS_OF)
    expected = truth()["receivables_open"]
    assert result.value == minor(expected["total"])
    got = {i["number"]: i for i in result.values}
    assert set(got) == {i["invoice_number"] for i in expected["items"]}
    for item in expected["items"]:
        row = got[item["invoice_number"]]
        assert row["open_minor"] == minor(item["open"]) and row["settled_minor"] == minor(item["settled"])
        assert row["age_days"] == item["age_days"] and row["due_date_missing"] is item["due_date_missing"]
        assert row["age_basis"] == item["age_basis"]
        bucket = item["bucket"]
        assert row["bucket"] == bucket
    for bucket, amount in expected["by_bucket"].items():
        assert result.detail["by_bucket_minor"][bucket] == minor(amount)
    assert [r["open_minor"] for r in result.values] == sorted((r["open_minor"] for r in result.values), reverse=True)
    # documents only in the JPK of September (before the bank history) are not receivables - listed apart
    assert {e["number"] for e in result.detail["excluded_history"]} == {
        d["number"] for d in truth()["history_only_documents"] if d["section"] == "sales"}


def test_payables_open_keep_both_invoices_of_a_tie_open(mconn, modm):
    result = compute(mconn, modm, "payables_open", as_of=AS_OF)
    expected = truth()["payables_open"]
    assert result.value == minor(expected["total"])
    got = {i["number"]: i for i in result.values}
    for item in expected["items"]:
        assert got[item["invoice_number"]]["open_minor"] == minor(item["open"])
        assert got[item["invoice_number"]]["unresolved_payments"] == item["unresolved_payments"]


def test_balances_of_the_accounts_and_cash_flow(mconn, modm):
    balances = compute(mconn, modm, "account_balances", as_of=AS_OF)
    by_iban = {r["iban"]: r for r in balances.values}
    for account in truth()["accounts"]:
        assert by_iban[account["iban"]]["balance_minor"] == minor(account["closing_balance"])
        assert by_iban[account["iban"]]["available_minor"] == minor(account["available_balance"])
    assert balances.detail["vat_minor"] == minor(next(a["closing_balance"] for a in truth()["accounts"]
                                                      if a["kind"] == "vat"))
    flow = compute(mconn, modm, "cash_flow", period="2026-10")
    rows = {r["iban"]: r for r in flow.values}
    for account in truth()["accounts"]:
        assert rows[account["iban"]]["inflows_minor"] == minor(account["sum_credits"])
        assert rows[account["iban"]]["outflows_minor"] == minor(account["sum_debits"])
    expected = truth()["metrics_expected"]["cash_report_2026_10"]
    assert flow.value == flow.detail["balance_change_minor"] == minor(expected["balance_change"])
    assert flow.detail["own_net_minor"] == 0


def test_revenue_accrual_and_cash(mconn, modm):
    expected = truth()["metrics_expected"]
    accrual = compute(mconn, modm, "revenue_accrual_net", period="2026-10")
    assert accrual.value == minor(expected["revenue_memorial_net_2026_10"])
    cash = compute(mconn, modm, "revenue_cash_gross", period="2026-10")
    assert cash.value == minor(expected["revenue_cash_gross_2026_10"])
    assert cash.detail["advances_minor"] > 0                    # "w tym zaliczki"
    by_object = compute(mconn, modm, "revenue_accrual_net", period="2026-10", dimensions=["cost_object"])
    assert sum(r["revenue_minor"] for r in by_object.values) == accrual.value


def test_margin_per_object_and_cash_contribution_equal_the_ground_truth(mconn, modm):
    margin = compute(mconn, modm, "margin_accrual")
    rows = {r["cost_object"]: r for r in margin.values}
    for event in truth()["events"]:
        assert rows[event["name"]]["margin_minor"] == minor(event["margin_net_pln"]), event["id"]
        assert rows[event["name"]]["cost_minor"] == minor(event["cost_net_pln"]), event["id"]
    assert rows["UNASSIGNED"]["is_unassigned"] and not rows["Gala ACME 2026"]["is_unassigned"]
    assert margin.value == sum(minor(e["margin_net_pln"]) for e in truth()["events"])     # without UNASSIGNED
    gala = compute(mconn, modm, "margin_accrual", period="2026", filters={"cost_object": "Gala ACME 2026"})
    assert gala.value == minor(truth()["events"][0]["margin_net_pln"]) and gala.unconfirmed_share == 0
    cash = compute(mconn, modm, "cash_contribution", period="2026-10")
    expected = truth()["metrics_expected"]["cash_report_2026_10"]
    by_name = {r["cost_object"]: r["cash_minor"] for r in cash.values}
    for event in truth()["events"]:
        assert by_name[event["name"]] == minor(event["cash_contribution_pln"])
    rows_minor = cash.detail["rows_minor"]
    assert rows_minor["unmatched"] == minor(expected["unmatched"])
    assert sum(rows_minor.values()) == cash.detail["balance_change_minor"] == minor(expected["balance_change"])
    assert cash.detail["control_difference_minor"] == 0


def test_company_result_is_the_composition_of_the_report(mconn, modm):
    result = compute(mconn, modm, "company_result", period="2026-10")
    expected = truth()["metrics_expected"]
    assert result.detail["revenue_minor"] == minor(expected["revenue_memorial_net_2026_10"])
    assert result.detail["memorial_difference_minor"] == 0 and result.detail["cash_difference_minor"] == 0
    assert result.detail["balance_change_minor"] == minor(expected["cash_report_2026_10"]["balance_change"])
    assert result.value == result.detail["memorial"]["control"]["result_minor"]


def test_fully_loaded_margin_is_a_separate_definition(mconn, modm):
    loaded = compute(mconn, modm, "margin_fully_loaded", period="2026-10")
    accrual = {r["cost_object"]: r for r in compute(mconn, modm, "margin_accrual").values}
    assert sum(r["overhead_minor"] for r in loaded.values) == loaded.detail["overhead_unassigned_cost_minor"]
    for row in loaded.values:
        assert row["fully_loaded_minor"] == row["result_minor"] - row["overhead_minor"]
        assert row["fully_loaded_minor"] != accrual[row["cost_object"]]["margin_minor"]


def test_costs_without_document_over_100_with_payroll_and_sensitive_only_in_aggregate(mconn, modm):
    result = compute(mconn, modm, "costs_without_document", period="2026-10")
    expected = truth()["metrics_expected"]["no_document_outflows_over_100"]
    assert result.value == -sum(minor(e["amount"]) for e in expected)
    listed = {r["entry_reference"]: r for r in result.values if "entry_reference" in r}
    groups = {r["group"]: r for r in result.values if "group" in r}
    for item in expected:
        if item["category"] == "Wynagrodzenia" or item["category"] == "Zajęcie komornicze":
            assert item["entry_reference"] not in listed           # never a person and an amount together
        else:
            assert listed[item["entry_reference"]]["amount_minor"] == minor(item["amount"])
    salaries = [e for e in expected if e["category"] == "Wynagrodzenia"]
    assert groups["payroll"]["amount_minor"] == sum(minor(e["amount"]) for e in salaries)
    assert groups["payroll"]["count"] == len(salaries)
    assert groups["sensitive"]["amount_minor"] == minor(next(e["amount"] for e in expected
                                                             if e["category"] == "Zajęcie komornicze"))
    assert result.detail["to_clarify"]["count"] == Counter(t["expected_status"] for t in truth()["transactions"]
                                                           )["to_clarify"]
    assert result.unconfirmed_share == 0


def test_allocation_coverage_has_unassigned_and_not_looked_apart(mconn, modm):
    result = compute(mconn, modm, "allocation_coverage", period="2026-10")
    detail = result.detail
    assert detail["not_looked_minor"] == 0 and detail["unassigned_minor"] > 0
    assert result.value == round(detail["objects_minor"] / detail["total_minor"], 4)


def test_dso_90_is_receivables_over_sales_of_90_days(mconn, modm):
    result = compute(mconn, modm, "dso_90", as_of=AS_OF)
    detail = result.detail
    assert detail["receivables_minor"] == minor(truth()["receivables_open"]["total"])
    assert result.value == round(detail["receivables_minor"] / detail["sales_90_minor"] * 90, 1)
    assert result.as_dict()["metrics_used"] == [{"key": "dso_90", "version": 1}, {"key": "receivables_open",
                                                                                   "version": 1}]


def test_own_transfer_without_a_decision_is_unmatched_with_its_detail(mconn, modm):
    """Review of 4A/4B, point 9: an own transfer counts as own only with a decision in force."""
    before = compute(mconn, modm, "cash_contribution", period="2026-10")
    assert before.detail["unmatched_detail_minor"]["unmatched_own"] == 0
    decisions = mconn.execute(
        "SELECT s.decision_id, t.amount_minor FROM l3_transaction_status s JOIN l1_bank_transaction t "
        "ON t.id = s.transaction_id WHERE s.company_id = %s AND s.final_status = 'own_transfer' AND s.status = 'active' "
        "AND t.amount_minor < 0 ORDER BY t.booking_date LIMIT 1", (modm.id,)).fetchall()
    for decision_id, _ in decisions:
        revoke_decision(mconn, decision_id, "test", reason="sprawdzam")
    after = compute(mconn, modm, "cash_contribution", period="2026-10")
    revoked = sum(amount for _, amount in decisions)
    assert after.detail["unmatched_detail_minor"]["unmatched_own"] == revoked != 0
    assert after.detail["rows_minor"]["own"] == before.detail["rows_minor"]["own"] - revoked
    assert after.detail["control_difference_minor"] == 0


def test_data_state_and_queue_state(mconn, modm):
    state = compute(mconn, modm, "data_state", period="2026-10")
    assert state.value == 0 and state.detail["truth_checks_green"] and state.detail["as_of"] == AS_OF
    assert set(state.detail["last_imports"]) >= {"bank", "ksef", "file", "pdf"}
    assert {c["check"] for c in state.values} >= {"balance_continuity", "mpp_vat", "match_coverage",
                                                   "allocation_shares"}
    queue = compute(mconn, modm, "queue_state")
    open_cards = mconn.execute("SELECT count(*) FROM queue_state WHERE company_id = %s AND closed_at IS NULL AND "
                               "card_kind <> 'sentinel'", (modm.id,)).fetchone()[0]
    assert queue.value == open_cards


def test_margin_v2_lists_a_candidate_waiting_for_its_card():
    """Before the person approved the candidates (the phase 4 pipeline): the object is listed, no number invented."""
    import psycopg

    from kasa.core.companies import get_company
    from tests.queue.pipeline import pipeline_database

    with psycopg.connect(pipeline_database()) as conn, conn.transaction(force_rollback=True):
        registry.sync_definitions(conn, refresh_views=False)
        company = get_company(conn, "odm")
        result = registry.compute(conn, company.id, "margin_accrual", filters={"cost_object": "Gala ACME 2026"})
        assert [r["status"] for r in result.values] == ["candidate"] and result.value is None
        assert any("czeka na zatwierdzenie" in w for w in result.warnings)
        assert result.as_dict()["value_pl"].startswith("brak liczby")
        v2 = registry.compute(conn, company.id, "margin_accrual", version=2, filters={"cost_object": "Gala ACME 2026"})
        assert v2.value == 0                    # the old version answers as it did (reproducible)
