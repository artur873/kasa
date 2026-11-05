"""The test client has no way to a number other than the MCP tools ("0 liczb z głowy")."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import kasa.core.agent.client as client_module

AMOUNT = re.compile(r"-?\d{1,3}(?: \d{3})*,\d{2} (?:zł|EUR|USD)")
PERCENT = re.compile(r"\d+,\d%")


def test_the_client_imports_no_database_and_no_metric_code():
    tree = ast.parse(Path(client_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not [m for m in imported if m.startswith(("psycopg", "kasa"))], imported
    assert "mcp" in imported


def numbers_from_tools(answer) -> list[str]:
    """Amounts and percentages of an answer that do NOT appear in the results of its tool calls."""
    source = answer.results_json
    return [n for n in AMOUNT.findall(answer.text) + PERCENT.findall(answer.text) if n not in source]


def test_every_number_of_an_answer_comes_from_a_tool(client):
    for question in ("receivables_open as_of=2026-10-31", "margin_accrual filter.cost_object=Gala",
                     "costs_without_document period=2026-10", "account_balances",
                     "query_view view=documents filter.month=2026-10 filter.direction=purchase", "list_queue limit=5"):
        answer = client.ask(question)
        assert answer.calls and not answer.refused, question
        assert AMOUNT.search(answer.text), question
        assert numbers_from_tools(answer) == [], question


def test_requests_to_decide_have_no_tool(client):
    for verb in ("zatwierdz_wszystko", "approve_all", "zaplac FV 14/2026", "wyslij przypomnienie", "ustaw kategorie"):
        answer = client.ask(verb)
        assert answer.refused and "nie mam narzędzia" in answer.text and answer.calls[0].result.get("no_tool")
