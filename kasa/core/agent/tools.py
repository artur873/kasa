"""The 17 tools of the agent (briefing, step 8) - read metrics, views and evidence, append suggestions.

There is no "approve", "pay", "send" or "set": the agent cannot decide anything, a person decides in the
review queue. Every read runs on the role ``kasa_agent_ro`` (``AgentDatabase.ro``), every write on
``kasa_agent_suggest`` (``AgentDatabase.suggest``) into ``l2_suggestion`` with ``source = 'agent'``,
``method = 'model'`` and the client's ``prompt_version``. Texts of the data come back as
``{"untrusted_text": ...}`` and pass the pseudonymization gate on the way out (``outbound.py``).

A tool that cannot answer returns ``{"refused": true, "reason": ...}`` - a question outside the metric layer
also logs a metric proposal (``proposal_logged: true``) instead of inventing a plausible number.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.metrics import registry
from kasa.core.metrics.base import MetricError, UnknownMetric, fmt_pln, parse_day

AGENT_SOURCE = "agent"
MAX_ROWS = 200

# Questions the agent must refuse without proposing a metric: pay per person is never available (the tool
# does not evaluate employees - briefing, step 9, AI Act Annex III); "approve"-like requests have no tool.
FORBIDDEN_KEY_RE = re.compile(r"(salar|wynagrodz|pensj|płac|plac|zarob|payroll|employee|pracownik|osob|person)", re.I)
FORBIDDEN_REASON = ("płace per osoba nie są dostępne: kasa liczy marżę obiektu kosztowego, nie osoby i nie ocenia "
                    "pracowników (Krok 9); żaden widok nie wystawia wynagrodzeń osób")


class Refusal(Exception):
    def __init__(self, reason: str, **extra: Any):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"refused": True, "reason": self.reason, **self.extra}


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value) if value != value.to_integral_value() else int(value)
    return value


def _uuid(value: Any, what: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise Refusal(f"{what}: oczekiwano identyfikatora (uuid), podano {str(value)[:40]!r}") from None


def _like(value: Any) -> str:
    options = [o.strip() for o in str(value).split("|") if o.strip()]
    return options[0] if len(options) == 1 else "|".join(options)


# ---------------------------------------------------------------------------
# query_view: predefined question views with whitelisted filters (never free SQL)
# ---------------------------------------------------------------------------


def _text_any(column: str):
    def build(value: Any) -> tuple[sql.Composable, list[Any]]:
        options = [o.strip() for o in str(value).split("|") if o.strip()]
        parts = [sql.SQL("{} ILIKE %s").format(sql.SQL(column)) for _ in options]
        return sql.SQL("(") + sql.SQL(" OR ").join(parts) + sql.SQL(")"), [f"%{o}%" for o in options]
    return build


def _cmp(expression: str, convert=lambda v: v):
    def build(value: Any) -> tuple[sql.Composable, list[Any]]:
        return sql.SQL(expression), [convert(value)]
    return build


def _flag(expression: str):
    def build(value: Any) -> tuple[sql.Composable, list[Any]]:
        on = str(value).lower() in ("1", "true", "tak", "yes")
        return (sql.SQL(expression) if on else sql.SQL("true")), []
    return build


def _minor(value: Any) -> int:
    return int(round(float(str(value).replace(",", ".").replace(" ", "")) * 100))


def _direction(value: Any) -> str:
    folded = str(value).strip().lower()
    mapping = {"in": "wpływ", "wpływ": "wpływ", "wplyw": "wpływ", "inflow": "wpływ",
               "out": "wypływ", "wypływ": "wypływ", "wyplyw": "wypływ", "outflow": "wypływ"}
    if folded not in mapping:
        raise Refusal("direction: in | out")
    return mapping[folded]


VIEWS: dict[str, dict[str, Any]] = {
    "transactions": {
        "description": "zaksięgowane przelewy (bez wrażliwych i bez wynagrodzeń osób); tytuł jako untrusted_text",
        "columns": ["id", "entry_reference", "booking_date", "amount_minor", "currency", "direction", "account_kind",
                    "op_type", "counterparty", "counterparty_kind", "title", "final_status", "subtype",
                    "status_method", "matched_documents", "related_transactions", "category", "intent"],
        "order": "booking_date, entry_reference",
        "amount": "amount_minor",
        "filters": {
            "date_from": _cmp("booking_date >= %s", parse_day), "date_to": _cmp("booking_date <= %s", parse_day),
            "month": _cmp("to_char(booking_date, 'YYYY-MM') = %s", str), "direction": _cmp("direction = %s", _direction),
            "counterparty": _text_any("counterparty"), "text": _text_any("title ->> 'untrusted_text'"),
            "final_status": _cmp("final_status = %s", str), "undecided": _flag("final_status IS NULL"),
            "subtype": _cmp("subtype = %s", str), "intent": _cmp("intent = %s", str),
            "op_type": _cmp("op_type = %s", str), "account_kind": _cmp("account_kind = %s", str),
            "min_amount": _cmp("abs(amount_minor) >= %s", _minor), "max_amount": _cmp("abs(amount_minor) <= %s", _minor),
            "related": _flag("related_transactions IS NOT NULL"), "category": _cmp("category = %s", str),
        },
    },
    "documents": {
        "description": "bieżące dokumenty z zapłatą (aktywne dopasowania); osoby kodem",
        "columns": ["id", "direction", "kind", "number", "ksef_number", "issue_date", "sale_date", "due_date",
                    "counterparty", "currency", "net_minor", "vat_minor", "gross_minor", "gross_pln_minor",
                    "paid_minor", "transfers", "source"],
        "order": "issue_date, number",
        "amount": "coalesce(gross_pln_minor, CASE WHEN currency = 'PLN' THEN gross_minor END)",
        "filters": {
            "direction": _cmp("direction = %s", lambda v: {"sprzedaż": "sales", "zakup": "purchase"}.get(v, v)),
            "kind": _cmp("kind = %s", str), "month": _cmp("to_char(issue_date, 'YYYY-MM') = %s", str),
            "date_from": _cmp("issue_date >= %s", parse_day), "date_to": _cmp("issue_date <= %s", parse_day),
            "counterparty": _text_any("counterparty"), "number": _text_any("number"),
            "open_only": _flag("gross_minor - paid_minor <> 0"),
        },
    },
    "receivables": {
        "description": "faktury sprzedaży z otwartym saldem (kwoty i wiek: metryka receivables_open)",
        "columns": ["id", "number", "ksef_number", "kind", "issue_date", "due_date", "due_date_missing",
                    "counterparty", "currency", "gross_minor", "paid_minor", "open_minor"],
        "order": "open_minor DESC, issue_date",
        "amount": "open_minor",
        "filters": {"counterparty": _text_any("counterparty"),
                    "issue_month": _cmp("to_char(issue_date, 'YYYY-MM') = %s", str),
                    "due_date_missing": _flag("due_date_missing")},
    },
    "cost_objects": {
        "description": "obiekty kosztowe (zatwierdzone i kandydaci) z atrybutami rozszerzenia",
        "columns": ["id", "name", "kind", "status", "system_code", "client", "city", "date_start", "date_end",
                    "allocations", "method"],
        "order": "date_start NULLS LAST, name",
        "amount": None,
        "filters": {"status": _cmp("status = %s", str), "name": _text_any("name"), "client": _text_any("client"),
                    "city": _text_any("city")},
    },
    "allocations": {
        "description": "alokacje w mocy z dowodem (decyzja, kto, kiedy); bez osób",
        "columns": ["id", "subject_type", "subject_id", "subject_label", "counterparty", "cost_object", "share",
                    "amount_minor", "method", "signal_strength", "cascade_step", "decision_id", "decided_at",
                    "decided_by"],
        "order": "cost_object, subject_label",
        "amount": "amount_minor",
        "filters": {"cost_object": _text_any("cost_object"), "counterparty": _text_any("counterparty"),
                    "label": _text_any("subject_label"), "subject_type": _cmp("subject_type = %s", str),
                    "method": _cmp("method = %s", str)},
    },
    "queue": {
        "description": "otwarte karty kolejki przeglądu (bez wartowników)",
        "columns": ["id", "position", "card_kind", "subject_type", "subject_id", "rank_reason", "badge",
                    "amount_minor", "deferred_until"],
        "order": "position",
        "amount": "amount_minor",
        "filters": {"card_kind": _cmp("card_kind = %s", str), "badge": _cmp("badge = %s", str),
                    "min_amount": _cmp("abs(amount_minor) >= %s", _minor)},
    },
}


def run_view(conn: psycopg.Connection, view: str, filters: dict[str, Any] | None, limit: int) -> dict[str, Any]:
    spec = VIEWS.get(view)
    if spec is None:
        raise Refusal(f"nie ma widoku {view!r} - dostępne: {sorted(VIEWS)} (bez wolnego SQL)")
    filters = dict(filters or {})
    unknown = [f for f in filters if f not in spec["filters"]]
    if unknown:
        reason = (FORBIDDEN_REASON if any(FORBIDDEN_KEY_RE.search(f) for f in unknown)
                  else f"widok {view}: nieznany filtr {unknown} (dostępne: {sorted(spec['filters'])})")
        raise Refusal(reason)
    where: list[sql.Composable] = [sql.SQL("true")]
    params: list[Any] = []
    for name, value in filters.items():
        if value is None or value == "":
            continue
        clause, values = spec["filters"][name](value)
        where.append(clause)
        params += values
    condition = sql.SQL(" AND ").join(where)
    limit = max(1, min(int(limit or 50), MAX_ROWS))
    query = sql.SQL("SELECT {cols} FROM views.{view} WHERE {cond} ORDER BY {order} LIMIT {limit}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in spec["columns"]), view=sql.Identifier(view),
        cond=condition, order=sql.SQL(spec["order"]), limit=sql.Literal(limit))
    with conn.cursor(row_factory=dict_row) as cur:
        rows = [jsonable(dict(r)) for r in cur.execute(query, params).fetchall()]
        totals: dict[str, Any] = {}
        agg = sql.SQL("SELECT count(*) AS count{amount} FROM views.{view} WHERE {cond}").format(
            amount=(sql.SQL(", coalesce(sum({}), 0) AS amount_sum_minor").format(sql.SQL(spec["amount"]))
                    if spec["amount"] else sql.SQL("")), view=sql.Identifier(view), cond=condition)
        row = cur.execute(agg, params).fetchone()
        totals = {k: int(v) for k, v in row.items()}
    return {"view": view, "filters": filters, "rows": rows, "returned": len(rows), "totals": totals,
            "truncated": totals["count"] > len(rows),
            "note": "sumy liczy widok (totals) - nie dodawaj kwot samodzielnie"}


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


class KasaTools:
    """Implementations of the 17 tools over an ``AgentDatabase`` (reads: ro, writes: suggest)."""

    TOOL_NAMES = ("list_metrics", "describe_metric", "get_metric", "query_view", "data_state", "search_documents",
                  "get_document", "search_transactions", "get_transaction", "explain_fact", "list_queue",
                  "simulate_rule", "propose_match", "propose_category", "propose_allocation", "propose_rule",
                  "draft_payment_reminder")

    def __init__(self, db, *, config):
        self.db = db
        self.config = config
        self.company_id: UUID = db.company_id
        self.call_id: UUID | None = None          # set by the server for every call

    # -- metrics ---------------------------------------------------------

    def list_metrics(self) -> dict[str, Any]:
        with self.db.ro() as conn:
            recorded = {(r["key"], r["version"]): r for r in registry.recorded(conn)}
        metrics = []
        for key in registry.keys():
            cls = registry.latest(key)
            info = registry.describe(cls)
            row = recorded.get((cls.key, cls.version))
            metrics.append({"key": info["key"], "version": info["version"], "ref": info["ref"],
                            "name_pl": info["name_pl"], "unit": info["unit"], "grain": info["grain"],
                            "period": info["period"], "dimensions": info["dimensions"],
                            "filters": sorted(info["filters"]), "status": row["status"] if row else "niezapisana"})
        return {"metrics": metrics, "count": len(metrics),
                "note": "liczby tylko przez get_metric (ta sama definicja co dashboard); pytanie spoza listy = "
                        "odmowa i propozycja metryki"}

    def describe_metric(self, key: str, version: int | None = None) -> dict[str, Any]:
        try:
            cls = registry.get(key, version)
        except UnknownMetric:
            raise Refusal(f"nie ma metryki {key!r}" + (f" w wersji {version}" if version else ""),
                          known=registry.keys()) from None
        info = registry.describe(cls)
        with self.db.ro() as conn:
            versions = [r for r in registry.recorded(conn) if r["key"] == cls.key]
        return {**info, "versions": versions}

    def get_metric(self, key: str, version: int | None = None, period: str | None = None, as_of: str | None = None,
                   dimensions: list[str] | None = None, filters: dict[str, Any] | None = None,
                   question: str | None = None) -> dict[str, Any]:
        if FORBIDDEN_KEY_RE.search(key or "") and key not in registry.keys():
            raise Refusal(FORBIDDEN_REASON, proposal_logged=False,
                          available_instead=["margin_accrual (marża obiektu kosztowego)"])
        try:
            cls = registry.get(key, version)
        except UnknownMetric:
            self._propose_metric(question or key, key, "brak metryki w warstwie metryk")
            raise Refusal(f"nie ma metryki {key!r} w warstwie metryk (kasa to narzędzie zarządcze, nie podatkowe ani "
                          "księgowe) - zamiast liczby zapisano propozycję metryki do przeglądu",
                          proposal_logged=True, known=registry.keys()) from None
        with self.db.ro() as conn:
            result = registry.compute(conn, self.company_id, cls.key, version=cls.version, period=period,
                                      as_of=as_of, dimensions=dimensions, filters=filters)
        return jsonable(result.as_dict())

    def _propose_metric(self, question: str, key: str | None, reason: str) -> None:
        with self.db.suggest() as conn:
            conn.execute(
                "INSERT INTO metric_proposal (id, company_id, question, requested_key, reason, proposed_by, "
                "agent_call_id) VALUES (%s, %s, %s, %s, %s, 'agent', %s)",
                (uuid.uuid4(), self.company_id, self.config.pseudonymize(question)[:2000], key, reason,
                 self.call_id),
            )

    def query_view(self, view: str, filters: dict[str, Any] | None = None, limit: int = 50) -> dict[str, Any]:
        with self.db.ro() as conn:
            return run_view(conn, view, filters, limit)

    def data_state(self, period: str | None = None) -> dict[str, Any]:
        with self.db.ro() as conn:
            state = registry.compute(conn, self.company_id, "data_state", period=period)
            queue = registry.compute(conn, self.company_id, "queue_state")
        return {"data_state": jsonable(state.as_dict()), "queue": jsonable(queue.as_dict())}

    # -- documents and transactions ---------------------------------------

    def _sensitive_query(self, query: str | None) -> None:
        if not query:
            return
        from kasa.core.semantics.gate import terms_for
        from kasa.core.sensitive import find_sensitive

        with self.db.gate() as conn:
            terms = terms_for(conn, self.db.company)
        if find_sensitive(query, terms):
            raise Refusal("zapytanie dotyczy tematu wrażliwego (komornik, zdrowie, związki, alimenty...): takie "
                          "przelewy omijają model, dane są tylko w kolejce przeglądu dla człowieka",
                          sensitive=True)

    def search_documents(self, query: str | None = None, direction: str | None = None, date_from: str | None = None,
                         date_to: str | None = None, limit: int = 20) -> dict[str, Any]:
        self._sensitive_query(query)
        filters: dict[str, Any] = {"direction": direction, "date_from": date_from, "date_to": date_to}
        with self.db.ro() as conn:
            if query:
                found = run_view(conn, "documents", {**filters, "counterparty": query}, limit)
                by_number = run_view(conn, "documents", {**filters, "number": query}, limit)
                seen = {r["id"] for r in found["rows"]}
                rows = found["rows"] + [r for r in by_number["rows"] if r["id"] not in seen]
                return {"query": query, "rows": rows[:limit], "returned": len(rows[:limit])}
            return run_view(conn, "documents", filters, limit)

    def get_document(self, document_id: str) -> dict[str, Any]:
        doc_id = _uuid(document_id, "document_id")
        with self.db.ro() as conn, conn.cursor(row_factory=dict_row) as cur:
            doc = cur.execute("SELECT * FROM views.documents WHERE id = %s", (doc_id,)).fetchone()
            if doc is None:
                raise Refusal("nie ma takiego dokumentu w tej firmie")
            lines = cur.execute(
                "SELECT line_no, jsonb_build_object('untrusted_text', name) AS name, quantity, net_minor, vat_minor, "
                "gross_minor, line_kind FROM l1_document_line WHERE document_id = %s ORDER BY line_no", (doc_id,)
            ).fetchall()
            payments = cur.execute(
                """
                SELECT t.entry_reference, t.booking_date, m.amount_minor, coalesce(m.document_amount_minor,
                       m.amount_minor) AS document_amount_minor, m.difference_kind::text AS difference_kind,
                       m.method::text AS method, m.decision_id, d.decided_by, d.decided_at
                  FROM l3_match m JOIN document_lineage l ON l.document_id = m.document_id
                  JOIN l1_bank_transaction t ON t.id = m.transaction_id
                  LEFT JOIN l3_decision_log d ON d.id = m.decision_id
                 WHERE l.head_id = %s AND m.status = 'active' ORDER BY t.booking_date
                """,
                (doc_id,),
            ).fetchall()
            allocations = cur.execute(
                "SELECT subject_type, subject_label, cost_object, share, amount_minor, method, decision_id, decided_at, "
                "decided_by FROM views.allocations WHERE subject_id = %s OR subject_id IN (SELECT id FROM "
                "l1_document_line WHERE document_id = %s) ORDER BY subject_label", (doc_id, doc_id)
            ).fetchall()
            corrections = cur.execute("SELECT number, gross_minor, issue_date FROM views.documents "
                                      "WHERE corrects_document_id = %s", (doc_id,)).fetchall()
        return jsonable({"document": dict(doc), "lines": [dict(r) for r in lines], "payments": [dict(r) for r in payments],
                         "paid_minor": sum(int(p["document_amount_minor"]) for p in payments),
                         "corrections": [dict(r) for r in corrections],
                         "allocations": [dict(r) for r in allocations]})

    def search_transactions(self, query: str | None = None, direction: str | None = None,
                            date_from: str | None = None, date_to: str | None = None,
                            min_amount: float | None = None, max_amount: float | None = None,
                            limit: int = 20) -> dict[str, Any]:
        self._sensitive_query(query)
        filters: dict[str, Any] = {"direction": direction, "date_from": date_from, "date_to": date_to,
                                   "min_amount": min_amount, "max_amount": max_amount}
        with self.db.ro() as conn:
            if query:
                by_party = run_view(conn, "transactions", {**filters, "counterparty": query}, limit)
                by_text = run_view(conn, "transactions", {**filters, "text": query}, limit)
                seen = {r["id"] for r in by_party["rows"]}
                rows = by_party["rows"] + [r for r in by_text["rows"] if r["id"] not in seen]
                return {"query": query, "rows": rows[:limit], "returned": len(rows[:limit])}
            return run_view(conn, "transactions", filters, limit)

    def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        tx_id = self._visible_transaction(transaction_id)
        with self.db.ro() as conn, conn.cursor(row_factory=dict_row) as cur:
            tx = cur.execute("SELECT * FROM views.transactions WHERE id = %s", (tx_id,)).fetchone()
            matches = cur.execute(
                """
                SELECT d.number_raw AS document, r.entry_reference AS related_transaction, m.amount_minor,
                       m.difference_kind::text AS difference_kind, m.method::text AS method, m.decision_id,
                       m.internal_record_id IS NOT NULL AS internal_record,
                       CASE WHEN m.evidence_only THEN 'dowód dokumentu grupy (ten sam koszt, nie osobny)' END
                           AS evidence_only
                  FROM l3_match m LEFT JOIN l1_document d ON d.id = m.document_id
                  LEFT JOIN l1_bank_transaction r ON r.id = m.related_transaction_id
                 WHERE m.transaction_id = %s AND m.status = 'active'
                """,
                (tx_id,),
            ).fetchall()
            suggestions = cur.execute(
                "SELECT id, kind::text AS kind, rank, source, method::text AS method, proposal ->> 'final_status' "
                "AS final_status, proposal ->> 'subtype' AS subtype, evidence ->> 'badge' AS badge "
                "FROM l2_suggestion WHERE subject_type = 'transaction' AND "
                "subject_id = %s AND status = 'open' ORDER BY rank NULLS LAST", (tx_id,)
            ).fetchall()
        return jsonable({"transaction": dict(tx), "matches": [dict(m) for m in matches],
                         "open_suggestions": [dict(s) for s in suggestions]})

    def _visible_transaction(self, transaction_id: str) -> UUID:
        with self.db.ro() as conn:
            row = conn.execute("SELECT id FROM views.transactions WHERE id::text = %s OR entry_reference = %s",
                               (str(transaction_id), str(transaction_id))).fetchone()
            if row is None:
                exists = conn.execute("SELECT 1 FROM l1_bank_transaction WHERE id::text = %s OR entry_reference = %s",
                                      (str(transaction_id), str(transaction_id))).fetchone()
        if row is None:
            if exists:
                raise Refusal("ten przelew nie jest dostępny dla agenta: słowo wrażliwe albo wynagrodzenie osoby - "
                              "dane tylko w kolejce przeglądu", sensitive=True)
            raise Refusal("nie ma takiego przelewu w tej firmie")
        return row[0]

    # -- explain ----------------------------------------------------------

    def explain_fact(self, subject_type: str, subject_id: str) -> dict[str, Any]:
        if subject_type not in ("transaction", "document", "line", "internal_record", "cost_object"):
            raise Refusal("subject_type: transaction | document | line | internal_record | cost_object")
        sid = self._visible_transaction(subject_id) if subject_type == "transaction" else _uuid(subject_id, "subject_id")
        with self.db.ro() as conn, conn.cursor(row_factory=dict_row) as cur:
            chain: dict[str, Any] = {"subject": {"type": subject_type, "id": str(sid)}}
            if subject_type == "transaction":
                fact = cur.execute(
                    "SELECT entry_reference, booking_date, amount_minor, op_type::text AS op_type, source, "
                    "method::text AS method, schema_version, created_at, raw_id, extracted - 'phones' AS extracted "
                    "FROM l1_bank_transaction WHERE id = %s", (sid,)).fetchone()
                raw_ids = [fact["raw_id"]] if fact and fact["raw_id"] else []
            elif subject_type in ("document", "line"):
                doc_id = sid if subject_type == "document" else (cur.execute(
                    "SELECT document_id FROM l1_document_line WHERE id = %s", (sid,)).fetchone() or {}).get(
                    "document_id")
                fact = cur.execute(
                    "SELECT number_raw, kind::text AS kind, direction::text AS direction, issue_date, gross_minor, "
                    "currency::text AS currency, source, method::text AS method, schema_version, created_at "
                    "FROM l1_document WHERE id = %s", (doc_id,)).fetchone()
                observations = cur.execute(
                    "SELECT o.raw_id, o.linked_by::text AS linked_by, o.parser_version, o.created_at FROM "
                    "l1_document_observation o JOIN document_lineage l ON l.document_id = o.document_id "
                    "WHERE l.head_id = %s ORDER BY o.created_at", (doc_id,)).fetchall()
                chain["observations"] = [dict(o) for o in observations]
                raw_ids = [o["raw_id"] for o in observations]
            elif subject_type == "internal_record":
                fact = cur.execute(
                    "SELECT mapping_key, row_no, record_date, amount_minor, cost_object_ref, settlement_type, "
                    "person_pseudonym, source, method::text AS method, created_at, file_raw_id FROM "
                    "l1_internal_record WHERE id = %s", (sid,)).fetchone()
                raw_ids = [fact["file_raw_id"]] if fact else []
            else:
                fact = cur.execute(
                    "SELECT name, kind, status::text AS status, attrs, method::text AS method, decision_id, created_at "
                    "FROM l3_cost_object WHERE id = %s", (sid,)).fetchone()
                members = cur.execute(
                    "SELECT record_type::text AS record_type, count(*) AS n, min(link_strength) AS weakest FROM "
                    "l2_cost_object_member WHERE candidate_id = %s AND status IN ('open', 'accepted') GROUP BY 1",
                    (sid,)).fetchall()
                chain["members"] = [dict(m) for m in members]
                raw_ids = []
            if fact is None:
                raise Refusal("nie ma takiego podmiotu w tej firmie")
            chain["1_source"] = [dict(r) for r in cur.execute(
                "SELECT id, channel::text AS channel, external_ref, sha256, fetched_at, purged_at FROM l0_raw "
                "WHERE id = ANY(%s::uuid[])", ([str(r) for r in raw_ids],)).fetchall()]
            chain["2_fact"] = dict(fact)
            chain["2_semantics"] = [dict(r) for r in cur.execute(
                "SELECT purpose, intent, model, prompt_version, method::text AS method, input_sha256, input_chars, "
                "created_at FROM l1_semantics WHERE subject_type::text = %s AND subject_id = %s AND superseded_by IS NULL",
                (subject_type, sid)).fetchall()]
            subjects = [sid]
            if subject_type == "transaction":
                main = cur.execute(
                    "SELECT m.id FROM l1_bank_transaction t JOIN l1_bank_transaction m ON m.mpp_group_id = t.mpp_group_id "
                    "AND m.mpp_role = 'main' AND m.superseded_by IS NULL WHERE t.id = %s AND t.mpp_role = 'vat_leg'",
                    (sid,)).fetchone()
                if main:
                    subjects.append(main["id"])
                    chain["split_payment"] = ("noga VAT split payment - sugestie i decyzje są na nodze głównej "
                                              f"{main['id']}")
            if subject_type == "document":
                subjects += [r["id"] for r in cur.execute("SELECT id FROM l1_document_line WHERE document_id = %s",
                                                          (sid,)).fetchall()]
            chain["3_suggestions"] = [dict(r) for r in cur.execute(
                """
                SELECT id, kind::text AS kind, subject_type::text AS subject_type, rank, status::text AS status,
                       source, method::text AS method, prompt_version, evidence ->> 'badge' AS badge,
                       evidence -> 'tags' AS tags, proposal ->> 'final_status' AS final_status,
                       proposal ->> 'subtype' AS subtype, proposal -> 'shares' AS shares, created_at, resolved_by_decision
                  FROM l2_suggestion WHERE subject_id = ANY(%s::uuid[]) ORDER BY created_at, rank NULLS LAST LIMIT 60
                """,
                ([str(s) for s in subjects],)).fetchall()]
            chain["4_decisions"] = [dict(r) for r in cur.execute(
                """
                SELECT d.id, d.kind::text AS kind, d.method::text AS method, d.decided_by, d.decided_at,
                       r.rule_key, d.rule_version, d.reverts_decision_id, d.suggestion_id
                  FROM l3_decision_log d LEFT JOIN l3_rule r ON r.id = d.rule_id
                 WHERE d.subject_id = ANY(%s::uuid[]) ORDER BY d.decided_at
                """,
                ([str(s) for s in subjects],)).fetchall()]
            if subject_type == "transaction":
                chain["4_status"] = [dict(r) for r in cur.execute(
                    "SELECT final_status::text AS final_status, subtype, status::text AS status, method::text AS method, "
                    "decision_id, awaiting_until FROM l3_transaction_status WHERE transaction_id = %s", (sid,)).fetchall()]
                chain["4_matches"] = [dict(r) for r in cur.execute(
                    "SELECT d.number_raw AS document, m.amount_minor, m.status::text AS status, m.method::text AS "
                    "method, m.decision_id FROM l3_match m LEFT JOIN l1_document d ON d.id = m.document_id "
                    "WHERE m.transaction_id = %s", (sid,)).fetchall()]
            chain["4_allocations"] = [dict(r) for r in cur.execute(
                "SELECT a.subject_type::text AS subject_type, o.name AS cost_object, a.share, a.amount_minor, "
                "a.status::text AS status, a.method::text AS method, a.decision_id FROM l3_allocation a "
                "JOIN l3_cost_object o ON o.id = a.cost_object_id WHERE a.subject_id = ANY(%s::uuid[])",
                ([str(s) for s in subjects],)).fetchall()]
        chain["note"] = ("łańcuch: źródło (L0, SHA-256) → fakt i odczyt modelu (L1) → sugestie (L2) → decyzje (L3); "
                         "teksty modelu i fakty sugestii to tekst niezaufany")
        return jsonable(chain)

    # -- queue and rules --------------------------------------------------

    def list_queue(self, limit: int = 20, card_kind: str | None = None) -> dict[str, Any]:
        with self.db.ro() as conn:
            cards = run_view(conn, "queue", {"card_kind": card_kind}, limit)
            summary = registry.compute(conn, self.company_id, "queue_state")
        return {"cards": cards, "summary": jsonable(summary.as_dict()),
                "note": "kolejka to sugestie do decyzji człowieka - agent nie ma narzędzia „zatwierdź”"}

    def simulate_rule(self, rule_key: str | None = None, pattern: dict[str, Any] | None = None,
                      action: dict[str, Any] | None = None, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        from kasa.core.rules import simulate as sim
        from kasa.core.rules.apply import in_scope, pattern_holds, validate_pattern
        from kasa.core.rules.facts import canonical_counterparty, subject_facts
        from kasa.core.rules.observe import compare

        with self.db.ro() as conn:
            if rule_key:
                row = conn.execute("SELECT id FROM l3_rule WHERE rule_key = %s ORDER BY version DESC LIMIT 1",
                                   (rule_key,)).fetchone()
                if row is None:
                    raise Refusal(f"nie ma reguły {rule_key!r}")
                report = sim.simulate(conn, row[0])
                out = report.__dict__
            else:
                if not pattern or not action:
                    raise Refusal("podaj rule_key albo pattern + action")
                try:
                    validate_pattern(pattern)
                except (ValueError, TypeError) as exc:
                    raise Refusal(f"zły wzorzec: {exc}") from None
                kind = sim.KIND_OF_ACTION.get(action.get("kind"))
                report = sim.Simulation("(propozycja)", 0)
                subjects = conn.execute(
                    """
                    SELECT 'transaction', id FROM l1_bank_transaction
                     WHERE status = 'booked' AND superseded_by IS NULL AND (mpp_role IS NULL OR mpp_role <> 'vat_leg')
                    UNION ALL SELECT 'document', id FROM l1_document WHERE superseded_by IS NULL
                    """
                ).fetchall()
                for subject_type, subject_id in subjects:
                    if kind == "match" and subject_type != "transaction":
                        continue
                    facts = subject_facts(conn, self.company_id, subject_type, subject_id)
                    party = facts.get("counterparty_id")
                    canonical = str(found) if party and (found := canonical_counterparty(conn, party)) else None
                    if not in_scope(scope or {}, subject_type, facts, canonical) or not pattern_holds(
                            pattern, facts, canonical):
                        continue
                    report.hits += 1
                    decision = sim._decision_of(conn, kind, subject_type, subject_id) if kind else None
                    compared = compare(conn, action, kind, (decision[0] or {}) if decision else {}) if decision else None
                    if compared is None:
                        report.undecided += 1
                        continue
                    predicted, actual, agreed = compared
                    report.by_method[decision[1]] = report.by_method.get(decision[1], 0) + 1
                    if agreed:
                        report.agreed += 1
                    else:
                        report.disagreed += 1
                        report.exceptions.append({"subject_type": subject_type, "subject_id": str(subject_id),
                                                  "label": facts.get("label") or "", "predicted": predicted,
                                                  "actual": actual})
                out = report.__dict__
        out = jsonable(out)
        out["exceptions"] = out.get("exceptions", [])[:20]
        out["note"] = "symulacja na historii, nic nie zapisano; regułę włącza tylko człowiek (karta reguły)"
        return out

    # -- proposals (L2, source = agent) -----------------------------------

    def _insert_suggestion(self, kind: str, subject_type: str | None, subject_id: UUID | None, proposal: dict[str, Any],
                           prompt_version: str, rationale: str | None, facts: list[str]) -> str:
        if not prompt_version or not str(prompt_version).strip():
            raise Refusal("prompt_version jest wymagane (pochodzenie sugestii modelu)")
        suggestion_id = uuid.uuid4()
        evidence = {"facts": facts, "badge": "low", "precedents": 0, "hard": False, "agent": True,
                    "agent_call_id": str(self.call_id) if self.call_id else None,
                    "client": self.config.client, "model": self.config.model}
        if rationale:
            evidence["rationale"] = {"untrusted_text": self.config.pseudonymize(rationale)[:2000]}
        with self.db.suggest() as conn:
            conn.execute(
                """
                INSERT INTO l2_suggestion (id, company_id, kind, subject_type, subject_id, proposal, source, method,
                                           evidence, prompt_version, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'model', %s, %s, %s)
                """,
                (suggestion_id, self.company_id, kind, subject_type, subject_id, Jsonb(jsonable(proposal)),
                 AGENT_SOURCE, Jsonb(evidence), str(prompt_version)[:200], f"agent:{self.config.client}"),
            )
        return str(suggestion_id)

    def propose_match(self, transaction_id: str, document_ids: list[str], prompt_version: str,
                      rationale: str | None = None) -> dict[str, Any]:
        tx_id = self._visible_transaction(transaction_id)
        if not document_ids:
            raise Refusal("podaj co najmniej jeden dokument")
        with self.db.ro() as conn, conn.cursor(row_factory=dict_row) as cur:
            tx = cur.execute("SELECT amount_minor, final_status FROM views.transactions WHERE id = %s", (tx_id,)).fetchone()
            if tx["final_status"] is not None:
                raise Refusal(f"przelew ma już decyzję ({tx['final_status']}) - zmienić ją może tylko człowiek")
            docs = []
            for raw in document_ids:
                doc = cur.execute("SELECT id, number, direction, gross_minor, paid_minor, currency FROM views.documents "
                                  "WHERE id::text = %s OR number = %s", (str(raw), str(raw))).fetchall()
                if len(doc) != 1:
                    raise Refusal(f"dokument {raw!r}: " + ("nie ma takiego" if not doc else "niejednoznaczny"))
                docs.append(doc[0])
        wanted = "sales" if tx["amount_minor"] > 0 else "purchase"
        if any(d["direction"] != wanted for d in docs):
            raise Refusal(f"wpływ łączymy z fakturą sprzedaży, wypływ z fakturą zakupu ({wanted})")
        items, rest = [], abs(int(tx["amount_minor"]))
        for d in docs:
            open_minor = int(d["gross_minor"]) - int(d["paid_minor"])
            part = min(rest, open_minor) if d is not docs[-1] else rest
            items.append({"document_id": str(d["id"]), "amount_minor": part, "document_amount_minor": part,
                          "number": d["number"]})
            rest -= part
        proposal = {"final_status": "matched", "subtype": None, "items": items, "proposed_by": "agent"}
        facts = [f"propozycja agenta: {', '.join(d['number'] for d in docs)}"]
        sid = self._insert_suggestion("match", "transaction", tx_id, proposal, prompt_version, rationale, facts)
        return {"proposed": True, "suggestion_id": sid, "kind": "match", "proposal": proposal,
                "note": "sugestia w L2 (źródło agent) - decyduje człowiek w kolejce przeglądu"}

    def propose_category(self, subject_type: str, subject_id: str, category: str, prompt_version: str,
                         rationale: str | None = None) -> dict[str, Any]:
        from kasa.core.extension import get_extension

        categories = list(get_extension(self.db.company.extension_slug).categories)
        if category not in categories:
            raise Refusal(f"nieznana kategoria {category!r} (dostępne: {categories})")
        sid_value = self._subject(subject_type, subject_id)
        sid = self._insert_suggestion("category", subject_type, sid_value, {"category": category, "proposed_by": "agent"},
                                      prompt_version, rationale, [f"propozycja agenta: kategoria {category}"])
        return {"proposed": True, "suggestion_id": sid, "kind": "category", "category": category,
                "note": "sugestia w L2 (źródło agent) - decyduje człowiek"}

    def _subject(self, subject_type: str, subject_id: str) -> UUID:
        if subject_type == "transaction":
            return self._visible_transaction(subject_id)
        table = {"document": "l1_document", "line": "l1_document_line", "internal_record": "l1_internal_record"}.get(
            subject_type)
        if table is None:
            raise Refusal("subject_type: transaction | document | line | internal_record")
        value = _uuid(subject_id, "subject_id")
        with self.db.ro() as conn:
            if conn.execute(sql.SQL("SELECT 1 FROM {} WHERE id = %s").format(sql.Identifier(table)),
                            (value,)).fetchone() is None:
                raise Refusal("nie ma takiego podmiotu w tej firmie")
        return value

    def propose_allocation(self, subject_type: str, subject_id: str, shares: list[dict[str, Any]], prompt_version: str,
                           rationale: str | None = None) -> dict[str, Any]:
        subject = self._subject(subject_type, subject_id)
        if not shares:
            raise Refusal("podaj udziały: [{cost_object, share}]")
        out, total = [], Fraction(0)
        with self.db.ro() as conn:
            for item in shares:
                ref = str(item.get("cost_object") or item.get("cost_object_id") or "")
                rows = conn.execute("SELECT id, name FROM views.cost_objects WHERE status = 'approved' AND "
                                    "(id::text = %s OR name = %s)", (ref, ref)).fetchall()
                if len(rows) != 1:
                    raise Refusal(f"obiekt kosztowy {ref!r}: " + ("nie ma zatwierdzonego" if not rows
                                                                 else "niejednoznaczny"))
                try:
                    value = Fraction(str(item.get("share"))).limit_denominator(1_000_000)
                except (ValueError, ZeroDivisionError):
                    raise Refusal(f"zły udział {item.get('share')!r}") from None
                if not 0 < value <= 1:
                    raise Refusal("udział musi być w (0, 1]")
                total += value
                out.append({"cost_object_id": str(rows[0][0]), "cost_object_name": rows[0][1],
                            "share": round(float(value), 6), "share_exact": str(value)})
        if abs(total - 1) > Fraction(1, 1_000_000):
            raise Refusal(f"udziały sumują się do {float(total):.6f}, a muszą do 1 (alokacja = 100%)")
        proposal = {"shares": out, "signal_strength": None, "cascade_step": None, "proposed_by": "agent"}
        sid = self._insert_suggestion("allocation", subject_type, subject, proposal, prompt_version, rationale,
                                      ["propozycja agenta: " + ", ".join(f"{s['cost_object_name']} {s['share']:g}"
                                                                         for s in out)])
        return {"proposed": True, "suggestion_id": sid, "kind": "allocation", "proposal": proposal,
                "note": "sugestia w L2 (źródło agent) - decyduje człowiek"}

    def propose_rule(self, question: str, pattern: dict[str, Any], action: dict[str, Any], prompt_version: str,
                     scope: dict[str, Any] | None = None, rationale: str | None = None) -> dict[str, Any]:
        from kasa.core.rules.apply import validate_pattern

        try:
            validate_pattern(pattern)
        except (ValueError, TypeError) as exc:
            raise Refusal(f"zły wzorzec: {exc}") from None
        if (action or {}).get("kind") not in ("category", "final_status", "allocation", "match_strategy"):
            raise Refusal("action.kind: category | final_status | allocation | match_strategy")
        proposal = {"question": self.config.pseudonymize(question), "pattern": pattern, "action": action,
                    "scope": scope or {}, "proposed_by": "agent", "stage": "candidate"}
        sid = self._insert_suggestion("rule_candidate", None, None, proposal, prompt_version, rationale,
                                      ["propozycja reguły od agenta - etap kandydat; dalej decyduje człowiek"])
        return {"proposed": True, "suggestion_id": sid, "kind": "rule_candidate",
                "note": "kandydat na regułę w L2 - podpowiedź, cień i auto ustawia wyłącznie człowiek"}

    def draft_payment_reminder(self, document_id: str, prompt_version: str, text: str | None = None,
                               tone: str | None = None) -> dict[str, Any]:
        doc_id = _uuid(document_id, "document_id")
        with self.db.ro() as conn:
            receivables = registry.compute(conn, self.company_id, "receivables_open")
        item = next((i for i in receivables.values if i["document_id"] == str(doc_id)), None)
        if item is None:
            raise Refusal("ten dokument nie jest otwartą należnością - przypomnienie niepotrzebne")
        open_pl = fmt_pln(item["open_minor"])
        due = item["due_date"] or "bez terminu płatności"
        draft = text or (
            f"Dzień dobry,\nuprzejmie przypominamy o płatności faktury {item['number']} z dnia {item['issue_date']} "
            f"(termin: {due}); do zapłaty pozostaje {open_pl}. Jeśli płatność została już zlecona, prosimy "
            f"zignorować tę wiadomość.\nPozdrawiamy")
        proposal = {"document_number": item["number"], "open_minor": item["open_minor"], "due_date": item["due_date"],
                    "age_days": item["age_days"], "tone": tone or "uprzejmy",
                    "draft": {"untrusted_text": self.config.pseudonymize(draft)}, "proposed_by": "agent",
                    "send": "wysyła człowiek - kasa niczego nie wysyła"}
        sid = self._insert_suggestion("payment_reminder", "document", doc_id, proposal, prompt_version, None,
                                      [f"szkic przypomnienia: {item['number']}, {open_pl}, wiek {item['age_days']} dni"])
        return {"proposed": True, "suggestion_id": sid, "kind": "payment_reminder", "draft": draft,
                "receivable": {"number": item["number"], "open_minor": item["open_minor"], "open_pl": open_pl,
                               "due_date": item["due_date"], "age_days": item["age_days"], "bucket": item["bucket"]},
                "metrics_used": [receivables.key + "@" + str(receivables.version)],
                "note": "to szkic (sugestia) - wysyłasz Ty; agent nie ma narzędzia „wyślij”"}


def refusal_for(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, Refusal):
        return exc.as_dict()
    if isinstance(exc, MetricError):
        return {"refused": True, "reason": str(exc)}
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return {"refused": True, "reason": "zapytanie przekroczyło limit 15 s (statement_timeout roli agenta)"}
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return {"refused": True, "reason": "rola agenta nie ma do tego uprawnień (zabezpieczenie w bazie)"}
    return {"refused": True, "reason": f"błąd narzędzia ({type(exc).__name__})", "error": True}
