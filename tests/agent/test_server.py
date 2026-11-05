"""The MCP server (briefing, step 8): exactly 17 read-and-propose tools, safeguards in the database, texts as
untrusted text after the pseudonymization gate, every call logged, refusals instead of invented numbers."""

from __future__ import annotations

import json
import re

import anyio
import pytest
from mcp import Client

from kasa.core.agent.tools import KasaTools
from kasa.core.documents import privacy
from kasa.core.pseudonym import fold
from tests.metrics.helpers import minor, truth
from tests.semantics.helpers import phones, surnames

BRIEFING_TOOLS = ["list_metrics", "describe_metric", "get_metric", "query_view", "data_state", "search_documents",
                  "get_document", "search_transactions", "get_transaction", "explain_fact", "list_queue",
                  "simulate_rule", "propose_match", "propose_category", "propose_allocation", "propose_rule",
                  "draft_payment_reminder"]
FORBIDDEN = {"approve", "accept", "zatwierdz", "zatwierdź", "pay", "zaplac", "zapłać", "send", "wyslij", "wyślij", "set",
             "ustaw", "delete", "update", "execute", "sql", "commit", "revoke", "decide", "apply"}


def last_log(owner, tool):
    return owner.execute("SELECT tool, status, args_pseudonymized, model, prompt_version, client, result_summary, "
                         "created_by FROM agent_call_log WHERE tool = %s ORDER BY created_at DESC LIMIT 1",
                         (tool,)).fetchone()


def test_exactly_the_seventeen_tools_of_the_briefing_and_none_that_decides(client):
    names = client.tools()
    assert names == BRIEFING_TOOLS and len(names) == 17 and list(KasaTools.TOOL_NAMES) == BRIEFING_TOOLS
    assert not [n for n in names if FORBIDDEN & set(n.lower().split("_"))]

    async def schemas():
        async with Client(client.server) as session:
            return {t.name: t.input_schema for t in (await session.list_tools()).tools}

    inputs = anyio.run(schemas)
    assert "sql" not in json.dumps(inputs).lower()
    assert set(inputs["query_view"]["properties"]) == {"view", "filters", "limit"}


def test_get_metric_carries_version_data_state_and_unconfirmed_share(client, owner):
    result = client.call("get_metric", key="receivables_open", as_of="2026-10-31")
    assert result["value"] == minor(truth()["receivables_open"]["total"]) and result["value_pl"] == "75 565,00 zł"
    assert result["metric"]["ref"] == "receivables_open@1" and result["data_state"]["as_of"] == "2026-10-31"
    assert result["unconfirmed_share"] == 0 and result["unconfirmed_share_pl"] == "0,0%"
    assert result["data_state"]["truth_checks_green"] is True
    log = last_log(owner, "get_metric")
    assert log[1] == "ok" and log[3] == "test-model" and log[5] == "pytest" and log[7] == "kasa_agent_suggest"
    assert log[6]["metric"] == "receivables_open@1" and log[6]["value"] == result["value"]


def test_unknown_metric_is_refused_and_proposed_instead_of_a_number(client, owner):
    before = owner.execute("SELECT count(*) FROM metric_proposal").fetchone()[0]
    result = client.call("get_metric", key="cit_2026", question="Ile zapłacimy CIT za 2026?")
    assert result["refused"] is True and result["proposal_logged"] is True and "value" not in result
    assert owner.execute("SELECT count(*) FROM metric_proposal").fetchone()[0] == before + 1
    row = owner.execute("SELECT question, requested_key, proposed_by, status, created_by FROM metric_proposal "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row == ("Ile zapłacimy CIT za 2026?", "cit_2026", "agent", "open", "kasa_agent_suggest")
    assert last_log(owner, "get_metric")[1] == "refused"


def test_pay_per_person_is_never_available(client, owner):
    before = owner.execute("SELECT count(*) FROM metric_proposal").fetchone()[0]
    result = client.call("get_metric", key="salary_per_person", question="Ile zarabia Kasia Nowak?")
    assert result["refused"] is True and result["proposal_logged"] is False and "płace per osoba" in result["reason"]
    assert owner.execute("SELECT count(*) FROM metric_proposal").fetchone()[0] == before
    assert client.call("query_view", view="transactions", filters={"person": "Kasia"})["refused"] is True
    payroll = client.call("search_transactions", query="Wynagrodzenie")
    assert payroll.get("rows") == []                  # salaries of persons are not in any view
    # the arguments were logged after pseudonymization: no name of a person in the log
    args = last_log(owner, "search_transactions")[2]
    assert args["query"] == "Wynagrodzenie"


def test_sensitive_topic_is_answered_only_by_the_queue(client):
    result = client.call("search_transactions", query="komornik")
    assert result["refused"] is True and result.get("sensitive") is True and "rows" not in result
    rows = client.call("query_view", view="transactions", filters={"text": "Komornik"})
    assert rows["rows"] == [] and rows["totals"]["count"] == 0
    hidden = client.call("get_transaction", transaction_id="202610096232801350")
    assert hidden["refused"] is True and hidden["sensitive"] is True


def test_views_return_titles_only_as_untrusted_text(client):
    result = client.call("query_view", view="transactions", filters={"month": "2026-10", "direction": "in"})
    assert result["rows"] and result["totals"]["count"] == len(result["rows"])
    for row in result["rows"]:
        assert "title_raw" not in row and set(row["title"]) == {"untrusted_text"}
    assert client.call("query_view", view="l1_bank_transaction")["refused"] is True
    assert client.call("query_view", view="transactions", filters={"sql": "1=1"})["refused"] is True


def test_nothing_personal_leaves_through_the_tools(client):
    outs = [client.call("get_metric", key=k) for k in ("payables_open", "costs_without_document", "margin_accrual",
                                                         "queue_state")]
    outs += [client.call("query_view", view=v, limit=200) for v in ("transactions", "documents", "allocations", "queue")]
    outs.append(client.call("list_queue", limit=50))
    for doc in client.call("query_view", view="documents", filters={"kind": "bill"})["rows"]:
        outs.append(client.call("get_document", document_id=doc["id"]))
        outs.append(client.call("explain_fact", subject_type="document", subject_id=doc["id"]))
    text = json.dumps(outs, ensure_ascii=False)
    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    assert not (surnames(truth()) & words)
    digits = re.sub(r"\D", "", text)
    assert not [p for p in phones(truth()) if p in digits]
    assert "P-" in text                               # persons are there - as codes


def test_no_gate_no_data(client, monkeypatch, owner):
    monkeypatch.setattr(privacy, "load_pseudonym_module", lambda: None)
    for tool, args in (("get_metric", {"key": "receivables_open"}), ("query_view", {"view": "transactions"}),
                       ("search_documents", {"query": "ACME"})):
        result = client.call(tool, **args)
        assert result["refused"] is True and result.get("gate") == "unavailable", tool
        assert not {"value", "rows", "values"} & set(result)
    assert last_log(owner, "search_documents")[2]["_gate"] == "unavailable"


def test_proposals_go_to_l2_as_the_suggestion_role_with_the_prompt_version(client, owner):
    from kasa.core.extension import get_extension

    category_name = get_extension("odm").categories[-1]
    tx = owner.execute("SELECT t.id FROM l1_bank_transaction t WHERE t.entry_reference = '202610277149922608'"
                       ).fetchone()[0]                # Kadr Studio 4 500 - "do wyjaśnienia" (decided by a person)
    decided = client.call("propose_match", transaction_id=str(tx), document_ids=["FV 14/2026"],
                          prompt_version="agent-v1")
    assert decided["refused"] is True and "tylko człowiek" in decided["reason"]
    doc = client.call("query_view", view="receivables", filters={"counterparty": "Orbita"})["rows"][0]
    category = client.call("propose_category", subject_type="document", subject_id=doc["id"], category=category_name,
                           prompt_version="agent-v1", rationale="faktura bez terminu")
    assert client.call("propose_category", subject_type="document", subject_id=doc["id"], category="Nieistniejąca",
                       prompt_version="agent-v1")["refused"] is True
    assert category["proposed"] is True
    gala = client.call("query_view", view="cost_objects", filters={"name": "Gala"})["rows"][0]
    allocation = client.call("propose_allocation", subject_type="document", subject_id=doc["id"],
                             shares=[{"cost_object": gala["id"], "share": 1}], prompt_version="agent-v1")
    assert allocation["proposed"] is True
    bad = client.call("propose_allocation", subject_type="document", subject_id=doc["id"],
                      shares=[{"cost_object": gala["id"], "share": 0.5}], prompt_version="agent-v1")
    assert bad["refused"] is True and "100%" in bad["reason"]
    rule = client.call("propose_rule", question="Czy Orbita Media to zawsze kategoria Inne?",
                       pattern={"all": [{"field": "counterparty_name", "op": "regex", "value": "Orbita"}]},
                       action={"kind": "category", "category": category_name}, prompt_version="agent-v1")
    assert rule["proposed"] is True
    draft = client.call("draft_payment_reminder", document_id=doc["id"], prompt_version="agent-v1")
    assert draft["proposed"] is True and "FV 9/10/2026" in draft["draft"] and "3 690,00 zł" in draft["draft"]
    missing = client.call("propose_category", subject_type="document", subject_id=doc["id"], category=category_name,
                          prompt_version="")
    assert missing["refused"] is True
    rows = owner.execute(
        "SELECT kind::text, source, method::text, prompt_version, created_by, status::text, rank FROM l2_suggestion "
        "WHERE id = ANY(%s::uuid[]) ORDER BY kind",
        ([category["suggestion_id"], allocation["suggestion_id"], rule["suggestion_id"], draft["suggestion_id"]],),
    ).fetchall()
    assert {r[0] for r in rows} == {"category", "allocation", "rule_candidate", "payment_reminder"}
    assert all(r[1:4] == ("agent", "model", "agent-v1") and r[4] == "agent:pytest" and r[5] == "open" and r[6] is None
               for r in rows)
    # a proposal decides nothing: no new decision of the agent anywhere
    assert owner.execute("SELECT count(*) FROM l3_decision_log WHERE created_by LIKE 'agent%%' OR decided_by "
                         "LIKE 'agent%%'").fetchone()[0] == 0


def test_explain_fact_is_the_chain_source_extraction_suggestion_decision(client):
    legs = client.call("search_transactions", query="Scena Pro", direction="out")["rows"]
    tx = next(r for r in legs if r["account_kind"] == "current")
    chain = client.call("explain_fact", subject_type="transaction", subject_id=tx["id"])
    vat_leg = next(r for r in legs if r["account_kind"] == "vat")
    assert "split payment" in client.call("explain_fact", subject_type="transaction",
                                          subject_id=vat_leg["id"]).get("split_payment", "")
    assert chain["1_source"] and len(chain["1_source"][0]["sha256"]) == 64 and chain["1_source"][0]["channel"] == "bank"
    assert chain["2_fact"]["entry_reference"] == tx["entry_reference"]
    assert chain["3_suggestions"] and chain["4_decisions"] and chain["4_matches"]
    assert client.call("explain_fact", subject_type="osoba", subject_id=tx["id"])["refused"] is True


def test_simulate_rule_writes_nothing(client, owner):
    before = owner.execute("SELECT count(*) FROM l3_decision_log").fetchone()[0]
    builtin = client.call("simulate_rule", rule_key="builtin:own_transfer")
    assert builtin["hits"] == builtin["agreed"] == 6 and builtin["disagreed"] == 0
    proposed = client.call("simulate_rule", pattern={"all": [{"field": "op_type", "op": "eq", "value": "zus"}]},
                           action={"kind": "final_status", "final_status": "no_document", "subtype": "zus"})
    assert proposed["hits"] == 1 and proposed["agreed"] == 1
    assert owner.execute("SELECT count(*) FROM l3_decision_log").fetchone()[0] == before


def test_every_call_is_logged_append_only(client, owner):
    before = owner.execute("SELECT count(*) FROM agent_call_log").fetchone()[0]
    client.call("list_metrics")
    client.call("data_state", period="2026-10")
    client.call("describe_metric", key="margin_accrual")
    assert owner.execute("SELECT count(*) FROM agent_call_log").fetchone()[0] == before + 3
    with pytest.raises(Exception):
        owner.execute("UPDATE agent_call_log SET tool = 'x'")


def test_every_string_of_a_result_goes_through_the_gate_whatever_its_key(client, agent, monkeypatch):
    """Review of 4C/5, point 10: the gate pseudonymized only the values of known keys - a reason of a decision
    (or any text) under another key, at any depth, left kasa as it was. Now every string goes through it."""
    person = truth()["pii"]["persons"][0]
    reason = f"zwrot zaliczki dla {person['name']}, tel. {person['phone_formats'][0]}, konto {person['iban']}"

    def explain_with_reasons(subject_type: str, subject_id: str) -> dict:
        return {"subject": {"type": subject_type, "id": subject_id},
                "4_decisions": [{"decision_id": "00000000-0000-0000-0000-000000000001", "kind": "match",
                                 "reason": reason, "uzasadnienie_decyzji": reason, "decided_by": person["name"],
                                 "amount_pl": f"{person['name']} 120,00 zł",
                                 "status": person["name"].split()[-1]}],
                "notatki": [[reason]], person["name"]: "klucz z danymi"}

    monkeypatch.setattr(agent.tools, "explain_fact", explain_with_reasons)
    result = client.call("explain_fact", subject_type="transaction", subject_id="x")
    text = json.dumps(result, ensure_ascii=False)
    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    assert fold(person["name"].split()[-1]) not in words, text
    assert re.sub(r"\D", "", person["phone"]) not in re.sub(r"\D", "", text)
    assert person["iban"] not in text and person["iban"][2:] not in re.sub(r"\s", "", text)
    assert "P-" in text and result["4_decisions"][0]["decision_id"].endswith("0001")      # ids stay as they are


def test_outbound_skips_only_strings_that_cannot_name_a_person():
    from kasa.core.agent.outbound import Outbound

    for safe in ("5f0c0f3e-8a4f-4f53-9d0e-6c3a3f0b2a11", "2026-10-31", "2026-10-31T09:00:00+00:00", "1 234,56 zł",
                 "30,0%", "P-436a", "receivables_open@1", "12345"):
        assert Outbound.safe(safe), safe
    assert Outbound.safe("matched", "final_status")
    for data in ("Anna Mazur", "500 975 639", "500975639", "PL10114958040940956375786126", "anna mazur"):
        assert not Outbound.safe(data), data
    assert not Outbound.safe("Anna Mazur", "final_status")          # a "safe" key does not excuse a data text
