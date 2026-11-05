"""A test client of the MCP server, in process and without a model (``python -m kasa agent ask``).

It talks to the server ONLY through MCP (``mcp.Client``): it lists the tools, calls them and writes the
answer from their structured results - every number it prints is a ``*_pl`` / ``value_pl`` field or an
integer of a result (the gate test checks it: "0 liczb z głowy"). It has no database connection and no
import of the metric layer: there is no other way to a number.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import Client

FORBIDDEN_VERBS = re.compile(r"^(zatwierd|approve|accept|zapła|zapla|pay|wyśl|wysl|send|ustaw|set_|usu|delete|"
                             r"update|commit|execute|sql)", re.I)
NUMERIC_ARGS = ("limit", "version", "min_amount", "max_amount")
LABEL_KEYS = ("number", "cost_object", "counterparty", "row", "account", "iban", "class", "label", "group",
              "entry_reference", "month", "bucket", "check", "day", "subject_label", "name")


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass
class Answer:
    text: str
    calls: list[Call] = field(default_factory=list)
    refused: bool = False

    @property
    def results_json(self) -> str:
        return json.dumps([c.result for c in self.calls], ensure_ascii=False, default=str)


class AgentClient:
    """``call(tool, **args)`` and ``ask(text)`` over an in-process MCP server."""

    def __init__(self, server: Any):
        self.server = server
        self.calls: list[Call] = []
        self._tools: list[str] | None = None

    async def _list(self) -> list[str]:
        async with Client(self.server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    def tools(self) -> list[str]:
        if self._tools is None:
            self._tools = anyio.run(self._list)
        return list(self._tools)

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with Client(self.server) as client:
            result = await client.call_tool(tool, args)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return {"refused": True, "reason": f"brak narzędzia albo błąd wywołania: {text}", "tool_error": True}
        if result.structured_content is not None:
            return dict(result.structured_content)
        text = " ".join(getattr(c, "text", "") for c in result.content)
        return json.loads(text) if text.strip().startswith("{") else {"text": text}

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        args = {k: v for k, v in args.items() if v is not None}
        if tool not in self.tools():
            result = {"refused": True, "reason": f"nie ma narzędzia {tool!r} - agent może tylko czytać i proponować",
                      "no_tool": True}
        else:
            result = anyio.run(self._call, tool, args)
        self.calls.append(Call(tool, args, result))
        return result

    # ------------------------------------------------------------------
    # the answer
    # ------------------------------------------------------------------

    def ask(self, text: str) -> Answer:
        """``<metric key | tool> key=value ...`` (values: JSON or text; dimensions=a,b; filters as k=v after --)."""
        start = len(self.calls)
        tokens = shlex.split(text)
        if not tokens:
            return Answer("Podaj klucz metryki albo narzędzie.", refused=True)
        head, rest = tokens[0], tokens[1:]
        args: dict[str, Any] = {}
        filters: dict[str, Any] = {}
        for token in rest:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            parsed: Any = value
            numeric = key in NUMERIC_ARGS and re.fullmatch(r"-?\d+(\.\d+)?", value or "x")
            if (value[:1] in "[{" and value) or value in ("true", "false") or numeric:
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = value
            if key == "dimensions" and isinstance(parsed, str):
                parsed = [p for p in parsed.split(",") if p]
            if key.startswith("filter."):
                filters[key[len("filter."):]] = parsed
            else:
                args[key] = parsed
        if filters:
            args["filters"] = filters
        if FORBIDDEN_VERBS.match(head) and head not in self.tools():
            answer = Answer(f"Nie mogę: nie mam narzędzia „{head}” (agent tylko czyta metryki i proponuje; "
                            f"decyzje podejmujesz w kolejce przeglądu). Dostępne narzędzia: {', '.join(self.tools())}.",
                            refused=True)
            self.calls.append(Call(head, args, {"refused": True, "reason": "brak narzędzia", "no_tool": True}))
            answer.calls = self.calls[start:]
            return answer
        if head in self.tools():
            result = self.call(head, **args)
        else:
            args.setdefault("question", text)
            result = self.call("get_metric", key=head, **args)
        answer = Answer(self.render(result), refused=bool(result.get("refused")))
        answer.calls = self.calls[start:]
        return answer

    @staticmethod
    def _row(row: dict[str, Any]) -> str:
        label = " · ".join(str(row[k]) for k in LABEL_KEYS if row.get(k) not in (None, "", []))
        title = row.get("title")
        if isinstance(title, dict) and title.get("untrusted_text"):
            label += f" · tytuł (tekst niezaufany): {title['untrusted_text']}"
        amounts = ", ".join(f"{k[:-3]} {v}" for k, v in row.items() if k.endswith("_pl") and v is not None)
        extra = []
        for key in ("age_days", "due_date", "due_date_missing", "status", "subtype_pl", "count", "cards"):
            if row.get(key) not in (None, "", False):
                extra.append(f"{key} {row[key]}")
        return "  - " + label + (f": {amounts}" if amounts else "") + (f" ({', '.join(extra)})" if extra else "")

    def render(self, result: dict[str, Any], *, rows: int = 12) -> str:
        if result.get("refused"):
            text = f"Odmowa: {result.get('reason')}"
            if result.get("proposal_logged"):
                text += " Zapisano propozycję metryki do przeglądu (metric_proposal)."
            return text
        metric = result.get("metric")
        if metric:
            state = result.get("data_state") or {}
            checks = ("zielone" if state.get("truth_checks_green") else
                      f"CZERWONE: {state.get('red_checks')}" if state.get("red_checks") else "nie liczone")
            period = (result.get("period") or {}).get("label") or f"na dzień {result.get('as_of')}"
            lines = [f"{metric['name_pl']} ({metric['ref']}): {result.get('value_pl')}",
                     f"Okres: {period} | dane na dzień: {state.get('as_of')} | niepotwierdzone (w otwartych "
                     f"sugestiach): {result.get('unconfirmed_share_pl')} ({result.get('unconfirmed_pl')}) | "
                     f"kontrole prawdy: {checks}"]
            for row in (result.get("values") or [])[:rows]:
                if isinstance(row, dict):
                    lines.append(self._row(row))
            if len(result.get("values") or []) > rows:
                lines.append(f"  … i dalej (razem pozycji: {len(result['values'])})")
            for warning in result.get("warnings") or []:
                lines.append(f"Uwaga: {warning}")
            return "\n".join(lines)
        if "cards" in result and "summary" in result:
            summary = result["summary"]
            lines = [f"Kolejka przeglądu ({summary['metric']['ref']}): {summary.get('value_pl')} kart, blokuje liczby: "
                     f"{summary['detail'].get('blocking_cards')} ({summary['detail'].get('blocking_amount_pl')})"]
            lines += [self._row(r) for r in (summary.get("values") or [])]
            lines += [f"  #{c.get('position')} {c.get('card_kind')}: {(c.get('rank_reason') or {}).get('untrusted_text')}"
                      for c in (result["cards"].get("rows") or [])[:rows]]
            return "\n".join(lines)
        if "hits" in result and "agreed" in result:
            return (f"Symulacja {result.get('rule_key')} v{result.get('version')}: trafień {result['hits']}, zgodnych "
                    f"{result['agreed']}, niezgodnych {result['disagreed']}, bez decyzji {result['undecided']}")
        if "rows" in result:
            totals = result.get("totals") or {}
            lines = [f"Widok {result.get('view') or ''}: {totals.get('count', result.get('returned'))} pozycji"
                     + (f", suma {totals.get('amount_sum_pl')}" if totals.get("amount_sum_pl") else "")]
            lines += [self._row(r) for r in result["rows"][:rows]]
            return "\n".join(lines)
        if "draft" in result:
            return f"Szkic przypomnienia (wysyłasz Ty):\n{result['draft']}"
        if "metrics" in result:
            return "Metryki: " + ", ".join(m["ref"] for m in result["metrics"])
        return json.dumps(result, ensure_ascii=False, indent=1, default=str)[:4000]
