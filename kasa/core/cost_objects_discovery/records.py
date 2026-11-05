"""Records that may form a cost object: lines, documents without lines, internal rows, transfers.

Every record carries what the pair model compares (briefing, step 5):

* name tokens - the proper name read from a line or a sheet row (``l1_semantics`` purpose
  ``line_entities``: ``candidate_name``) or, for a transfer, the words of its title;
* client - the buyer of a sales document, a payer of an incoming transfer, or the client hint
  of a line or a row resolved to a known buyer;
* date window - dates written in the text, the date of a row, the service period or sale date
  of the document, the booking date of a transfer (weakest);
* city, people of the internal files (pseudonyms only), memorial amount.

Texts are the pseudonymized forms (titles, rows without the person and amount columns); a person
is never named here, only by pseudonym.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.allocation.amounts import load_documents
from kasa.core.cost_objects_discovery.text import (
    city_key,
    client_tokens,
    dates_in_text,
    merge_windows,
    month_window,
    token_set,
    untrusted,
)

# member type (l2_cost_object_member.record_type) <-> polymorphic subject type
SUBJECT_TYPE = {"document_line": "line", "document": "document", "transaction": "transaction",
                "internal_record": "internal_record", "email_case": "email_case"}
MEMBER_TYPE = {v: k for k, v in SUBJECT_TYPE.items()}

_PSEUDONYM_RE = re.compile(r"^P-[0-9a-f]{4,16}$")

# transfers that never belong to a cost object of a client (own money, the state, the bank)
_SKIP_OP_TYPES = frozenset({"own", "fee", "tax", "zus", "interest"})
_SKIP_COUNTERPARTY_KINDS = frozenset({"own", "bank", "institution"})


@dataclass
class DiscoveryRecord:
    record_type: str                     # document_line | document | transaction | internal_record
    record_id: UUID
    company_id: UUID
    text: str = ""
    display_name: str | None = None      # proper name as written (lines, rows)
    name_tokens: frozenset[str] = frozenset()
    text_tokens: frozenset[str] = frozenset()
    client_tokens: frozenset[str] = frozenset()
    client_name: str | None = None
    window: tuple[date, date] | None = None
    date_source: str | None = None       # text | record | document | booking
    city: str | None = None
    city_key: str | None = None
    city_source: str | None = None       # text | document
    persons: frozenset[str] = frozenset()
    amount_minor: int | None = None      # memorial PLN (lines, rows); signed amount (transfers)
    direction: str | None = None         # sales | purchase | in | out | internal
    document_id: UUID | None = None
    document_kind: str | None = None
    line_kind: str | None = None
    month_only: bool = False
    anchor_eligible: bool = False
    entities: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, UUID]:
        return self.record_type, self.record_id

    @property
    def subject_type(self) -> str:
        return SUBJECT_TYPE[self.record_type]

    @property
    def record_date(self) -> date | None:
        return self.window[0] if self.window else None


def _entities_window(entities: dict[str, Any]) -> tuple[tuple[date, date] | None, bool]:
    period = entities.get("period") or None
    if isinstance(period, dict) and period.get("from") and period.get("to"):
        try:
            return (date.fromisoformat(period["from"]), date.fromisoformat(period["to"])), False
        except ValueError:
            pass
    dates = []
    for value in entities.get("dates") or []:
        try:
            dates.append(date.fromisoformat(str(value)))
        except ValueError:
            continue
    if dates:
        return (min(dates), max(dates)), False
    month = month_window(entities.get("month"))
    if month:
        return month, True
    return None, False


def _semantics(conn: psycopg.Connection, company_id: UUID, subject_type: str) -> dict[UUID, tuple[dict, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (subject_id) subject_id, entities, input_pseudonymized
          FROM l1_semantics
         WHERE company_id = %s AND subject_type = %s AND purpose = 'line_entities' AND superseded_by IS NULL
         ORDER BY subject_id, created_at DESC, id
        """,
        (company_id, subject_type),
    ).fetchall()
    return {r[0]: (r[1] or {}, r[2] or "") for r in rows}


def _mapped_month(fields: dict[str, Any], evidence: dict[str, Any]) -> str | None:
    """The value of the column mapped to ``month`` (a rent roll), as 'YYYY-MM'."""
    header = ((evidence.get("column_map") or {}).get("month")) or "month"
    value = fields.get(header)
    if value is None:
        extra = evidence.get("extra") or {}
        value = extra.get("month")
    if not value:
        return None
    text = str(value).strip()
    for pattern in (r"^(\d{4})-(\d{1,2})", r"^(\d{1,2})[./-](\d{4})$"):
        m = re.match(pattern, text)
        if m:
            year, month = (m[1], m[2]) if len(m[1]) == 4 else (m[2], m[1])
            if 1 <= int(month) <= 12:
                return f"{int(year):04d}-{int(month):02d}"
    return None


class ClientResolver:
    """Client hints ("ACME", "Nordis") resolved to the buyers of the company's sales documents."""

    def __init__(self, buyers: dict[UUID, str]):
        self.buyers = {cid: (name, client_tokens(name)) for cid, name in buyers.items() if name}

    def resolve(self, hint: str | None) -> tuple[str | None, frozenset[str]]:
        hint_tokens = client_tokens(hint)
        if not hint_tokens:
            return None, frozenset()
        found = [(name, toks) for name, toks in self.buyers.values() if hint_tokens & toks]
        names = {name for name, _ in found}
        if len(names) == 1:
            name, toks = found[0]
            return name, toks
        return None, hint_tokens


def load_records(conn: psycopg.Connection, company_id: UUID, *,
                 all_transactions: bool = False) -> list[DiscoveryRecord]:
    """Every record of the company the discovery may group (current versions only).

    ``all_transactions`` keeps also the transfers that never belong to a client's cost object
    (taxes, ZUS, bank fees, interest) - allocation needs them (they go to UNASSIGNED); own
    transfers are always left out.
    """
    documents = load_documents(conn, company_id)
    buyers = {d.counterparty_id: d.counterparty_name for d in documents.values()
              if d.direction == "sales" and d.counterparty_id and d.counterparty_kind != "person"}
    clients = ClientResolver(buyers)
    records: list[DiscoveryRecord] = []

    line_semantics = _semantics(conn, company_id, "line")
    for doc in documents.values():
        sales = doc.direction == "sales"
        doc_client_name = doc.counterparty_name if sales and doc.counterparty_kind != "person" else None
        persons = frozenset({doc.counterparty_pseudonym}) if (
            doc.counterparty_kind == "person" and doc.counterparty_pseudonym) else frozenset()
        doc_window = doc.recognition_window
        if not doc.lines:
            records.append(DiscoveryRecord(
                record_type="document", record_id=doc.id, company_id=company_id, text=doc.number_raw,
                client_tokens=client_tokens(doc_client_name), client_name=doc_client_name,
                window=doc_window, date_source="document", city=doc.issue_place, city_key=city_key(doc.issue_place),
                city_source="document" if doc.issue_place else None, persons=persons,
                amount_minor=doc.memorial_minor, direction=doc.direction, document_id=doc.id,
                document_kind=doc.kind,
                facts={"number": doc.number_raw, "counterparty": doc.counterparty_display},
            ))
            continue
        for line in doc.lines:
            entities, _ = line_semantics.get(line.id, ({}, ""))
            name = untrusted(entities.get("candidate_name"))
            window, month_only = _entities_window(entities)
            date_source = "text" if window else None
            if window is None:
                window, date_source = doc_window, "document"
            city = untrusted(entities.get("city"))
            city_source = "text" if city else None
            # the place a supplier issued its invoice (a hotel, a hall) - never ours on a sales document
            if not city and not name and doc.issue_place and not sales:
                city, city_source = doc.issue_place, "document"
            hint = untrusted(entities.get("client_hint"))
            if sales and doc_client_name:
                client_name, ctoks = doc_client_name, client_tokens(doc_client_name)
            else:
                client_name, ctoks = clients.resolve(hint)
            records.append(DiscoveryRecord(
                record_type="document_line", record_id=line.id, company_id=company_id, text=line.name or "",
                display_name=name, name_tokens=token_set(name), text_tokens=token_set(line.name) | token_set(name),
                client_tokens=ctoks, client_name=client_name, window=window, date_source=date_source,
                city=city, city_key=city_key(city), city_source=city_source, persons=persons,
                amount_minor=line.memorial_minor, direction=doc.direction, document_id=doc.id,
                document_kind=doc.kind, line_kind=line.line_kind, month_only=month_only, entities=entities,
                facts={"number": doc.number_raw, "line_no": line.line_no, "counterparty": doc.counterparty_display,
                       "service_kind": entities.get("service_kind"), "client_hint": hint},
            ))

    record_semantics = _semantics(conn, company_id, "internal_record")
    for r in conn.execute(
        """
        SELECT id, mapping_key, person_pseudonym, record_date, amount_minor, cost_object_ref, settlement_type,
               fields, evidence
          FROM l1_internal_record WHERE company_id = %s AND superseded_by IS NULL
        """,
        (company_id,),
    ).fetchall():
        entities, seen_text = record_semantics.get(r[0], ({}, ""))
        ref = (r[5] or "").strip() or None
        name = untrusted(entities.get("candidate_name")) or ref
        city = untrusted(entities.get("city"))
        hint = untrusted(entities.get("client_hint"))
        client_name, ctoks = clients.resolve(hint)
        month = _mapped_month(r[7] or {}, r[8] or {})
        window = (r[3], r[3]) if r[3] else (_entities_window(entities)[0] or month_window(month))
        text = seen_text or " | ".join(v for v in (ref, r[6]) if v)
        records.append(DiscoveryRecord(
            record_type="internal_record", record_id=r[0], company_id=company_id, text=text, display_name=name,
            name_tokens=token_set(ref) | token_set(name), text_tokens=token_set(text) | token_set(name),
            client_tokens=ctoks, client_name=client_name, window=window, date_source="record" if r[3] else "text",
            city=city, city_key=city_key(city), city_source="text" if city else None,
            persons=frozenset({r[2]}) if r[2] else frozenset(), amount_minor=r[4], direction="internal",
            entities=entities,
            facts={"mapping_key": r[1], "cost_object_ref": ref, "settlement_type": r[6], "month": month},
        ))

    for r in conn.execute(
        """
        SELECT t.id, t.amount_minor, t.booking_date, t.op_type::text, t.title_pseudonymized,
               t.counterparty_pseudonym, t.counterparty_id, cp.kind::text, cp.name, cp.name_pseudonymized,
               ts.intent, ts.entities
          FROM l1_bank_transaction t
          LEFT JOIN l1_counterparty cp ON cp.id = t.counterparty_id
          LEFT JOIN title_semantics_current ts ON ts.transaction_id = t.id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked'
        """,
        (company_id,),
    ).fetchall():
        tx_id, amount, booked, op_type, title, cp_pseudonym, cp_id, cp_kind, cp_name, cp_name_ps, intent, ent = r
        if op_type == "own" or cp_kind == "own":
            continue
        if not all_transactions and (op_type in _SKIP_OP_TYPES or cp_kind in _SKIP_COUNTERPARTY_KINDS):
            continue
        entities = ent or {}
        title = title or ""
        windows = dates_in_text(title, booked)
        window = merge_windows(windows) if windows else (booked, booked)
        persons = set(entities.get("person_codes") or [])
        if cp_kind == "person" and (cp_name_ps or cp_pseudonym):
            persons.add(cp_name_ps or cp_pseudonym)
        elif cp_kind is None and cp_pseudonym and _PSEUDONYM_RE.match(cp_pseudonym):
            persons.add(cp_pseudonym)       # a person the bank parser pseudonymized, not yet a counterparty
        client_name, ctoks = None, frozenset()
        if amount > 0 and cp_kind == "company" and cp_id in buyers:
            client_name, ctoks = cp_name, client_tokens(cp_name)
        hint = untrusted(entities.get("counterparty_hint"))
        if not client_name and hint:
            client_name, ctoks = clients.resolve(hint)
        place = untrusted(entities.get("place"))
        title_tokens = token_set(title)
        records.append(DiscoveryRecord(
            record_type="transaction", record_id=tx_id, company_id=company_id, text=title,
            name_tokens=title_tokens, text_tokens=title_tokens, client_tokens=ctoks, client_name=client_name,
            window=window, date_source="text" if windows else "booking", city=place, city_key=city_key(place),
            city_source="text" if place else None, persons=frozenset(p for p in persons if p),
            amount_minor=amount, direction="in" if amount > 0 else "out",
            entities=entities, facts={"op_type": op_type, "intent": intent, "counterparty_id": cp_id,
                                      "counterparty_kind": cp_kind,
                                      "invoice_reference": bool(entities.get("invoice_numbers")),
                                      "counterparty": cp_name if cp_kind != "person" else (cp_name_ps or cp_pseudonym)},
        ))
    return records


def document_frequencies(records: list[DiscoveryRecord]) -> tuple[Counter, int]:
    df: Counter = Counter()
    for record in records:
        df.update(record.text_tokens | record.name_tokens)
    return df, len(records)
