"""Helpers of the phase 4B tests: the reference truth of the fixtures as a person's clicks.

* ``approve_all`` - discovery + approval of every candidate (decided_by='test');
* ``event_map`` - GT event id (EV-01...) -> approved cost object (by name, else by dates and city);
* ``simulate_matches`` - the matches of ``ground_truth.json`` as decisions: through 4A's
  ``kasa.core.matching.decide.apply_match`` when the module exists (a match suggestion per
  transfer, accepted by 'test'), otherwise ``l3_match`` / ``l3_transaction_status`` inserted here;
* ``click_ground_truth`` - for every subject with open allocation suggestions: accept the one that
  equals ``final_allocation`` of the file (rank 1 when it is right), else allocate by hand ("Inne...");
  automation decisions are compared with the file (a wrong automatic allocation is a failure).
"""

from __future__ import annotations

import importlib.util
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.allocation.decide import allocate_manually, apply_allocation
from kasa.core.cost_objects_discovery.decide import approve_candidate
from kasa.core.cost_objects_discovery.discover import discover
from kasa.core.decisions import log_decision
from kasa.core.normalize import normalize_invoice_number
from kasa.reference.ground_truth import SUBTYPE_BY_CATEGORY

REPO = Path(__file__).resolve().parents[2]
GROUND_TRUTH = REPO / "fixtures" / "odm" / "ground_truth.json"


def ground_truth() -> dict[str, Any]:
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


def minor(text: str | None) -> int:
    return int((Decimal(text or "0") * 100).to_integral_value())


def approve_all(conn: psycopg.Connection, company) -> list[UUID]:
    discover(conn, company, created_by="test")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM l3_cost_object WHERE company_id = %s AND status = 'candidate' ORDER BY name", (company.id,)
    ).fetchall()]
    for cost_object_id in ids:
        approve_candidate(conn, cost_object_id, decided_by="test")
    return ids


def event_map(conn: psycopg.Connection, company, truth: dict[str, Any] | None = None) -> dict[str, UUID]:
    truth = truth or ground_truth()
    rows = conn.execute(
        "SELECT id, name, attrs FROM l3_cost_object WHERE company_id = %s AND status = 'approved' "
        "AND system_code IS NULL", (company.id,),
    ).fetchall()
    out: dict[str, UUID] = {}
    for event in truth["events"]:
        exact = [r[0] for r in rows if r[1] == event["name"]]
        if exact:
            out[event["id"]] = exact[0]
            continue
        by_window = [r[0] for r in rows if r[2].get("date_start") == event["date_from"]
                     and r[2].get("date_end") == event["date_to"] and r[2].get("city") == event["city"]]
        if len(by_window) == 1:
            out[event["id"]] = by_window[0]
    unassigned = conn.execute(
        "SELECT id FROM l3_cost_object WHERE company_id = %s AND system_code = 'UNASSIGNED'", (company.id,)
    ).fetchone()[0]
    out["UNASSIGNED"] = unassigned
    return out


# ---------------------------------------------------------------------------
# matches from the reference file
# ---------------------------------------------------------------------------


def _lookups(conn: psycopg.Connection, company_id: UUID) -> dict[str, dict]:
    tx = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT entry_reference, id, mpp_role, mpp_group_id FROM l1_bank_transaction WHERE company_id = %s "
        "AND superseded_by IS NULL AND status = 'booked'", (company_id,)).fetchall()}
    ksef = dict(conn.execute(
        "SELECT ksef_number, id FROM l1_document WHERE company_id = %s AND superseded_by IS NULL "
        "AND ksef_number IS NOT NULL", (company_id,)).fetchall())
    numbers = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT number_normalized, direction::text, id FROM l1_document WHERE company_id = %s "
        "AND superseded_by IS NULL ORDER BY (ksef_number IS NOT NULL)", (company_id,)).fetchall()}
    rows = dict(conn.execute(
        "SELECT row_no, id FROM l1_internal_record WHERE company_id = %s AND superseded_by IS NULL",
        (company_id,)).fetchall())
    return {"tx": tx, "ksef": ksef, "numbers": numbers, "rows": rows}


_DIFFERENCE = {"kurs": "fx", "niedopłata": "underpayment", "nadpłata (możliwy duplikat)": "overpayment"}


def _items(tx: dict[str, Any], look: dict[str, dict]) -> list[dict[str, Any]]:
    items = []
    for doc in tx.get("matched_documents") or []:
        amount = minor(doc.get("amount_pln"))
        item = {"document_id": None, "internal_record_id": None, "related_transaction_id": None,
                "amount_minor": amount, "difference_kind": None, "difference_minor": None}
        if doc["doc_type"] == "ksef":
            item["document_id"] = str(look["ksef"][doc["doc_id"]])
        elif doc["doc_type"] == "email_pdf":
            key = (normalize_invoice_number(doc["invoice_number"]), "purchase")
            item["document_id"] = str(look["numbers"][key])
        elif doc["doc_type"] == "pm_sheet":
            item["internal_record_id"] = str(look["rows"][int(doc["sheet_row"])])
        elif doc["doc_type"] == "bank_tx":
            item["related_transaction_id"] = str(look["tx"][doc["doc_id"]][0])
        difference = tx.get("difference") or {}
        kind = _DIFFERENCE.get(difference.get("name"))
        if kind and len(tx["matched_documents"]) == 1:
            item["difference_kind"], item["difference_minor"] = kind, minor(difference.get("amount_pln"))
        items.append(item)
    return items


def simulate_matches(conn: psycopg.Connection, company, truth: dict[str, Any] | None = None,
                     *, use_matching_module: bool | None = None) -> str:
    """Every transfer of the file decided as the file says. Returns the path used ('4A' or 'direct')."""
    truth = truth or ground_truth()
    look = _lookups(conn, company.id)
    if use_matching_module is None:
        use_matching_module = importlib.util.find_spec("kasa.core.matching.decide") is not None
    by_ref = {t["entry_reference"]: t for t in truth["transactions"]}
    vat_legs: dict[str, list[dict]] = defaultdict(list)
    for t in truth["transactions"]:
        tx_id, role, group = look["tx"][t["entry_reference"]]
        if role == "vat_leg" and group:
            vat_legs[str(group)].append(t)
    for ref, t in by_ref.items():
        tx_id, role, group = look["tx"][ref]
        has_main = any(look["tx"][r][2] == group and look["tx"][r][1] == "main" for r in by_ref)
        if role == "vat_leg" and group and has_main:
            continue      # the decision on the main leg closes the VAT leg (one occurrence)
        items = _items(t, look) if t["expected_status"] == "matched" else []
        if role == "main" and group:
            for leg in vat_legs.get(str(group), []):
                for extra in _items(leg, look):
                    same = next((i for i in items if i["document_id"] == extra["document_id"]), None)
                    if same:
                        same["amount_minor"] += extra["amount_minor"]
        subtype = (SUBTYPE_BY_CATEGORY.get(t.get("no_document_category") or "")
                   if t["expected_status"] == "no_document" else None)
        proposal = {"items": items, "final_status": t["expected_status"], "subtype": subtype, "awaiting_until": None}
        if use_matching_module:
            from kasa.core.matching.decide import apply_match

            suggestion = conn.execute(
                """
                INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, source,
                                           method, evidence, created_by)
                VALUES (%s, 'match', 'transaction', %s, %s, 1, 1, 'test', 'code', %s, 'test') RETURNING id
                """,
                (company.id, tx_id, Jsonb(proposal), Jsonb({"facts": ["ground truth"], "badge": "high"})),
            ).fetchone()[0]
            apply_match(conn, suggestion, decided_by="test", method="human")
        else:
            _insert_match(conn, company.id, tx_id, proposal, look, group)
    return "4A" if use_matching_module else "direct"


def _insert_match(conn, company_id, tx_id, proposal, look, group) -> None:
    legs = [tx_id]
    if group:
        legs = [r[0] for r in look["tx"].values() if r[2] == group]
    decision = log_decision(conn, company_id=company_id, kind="match", subject_type="transaction", subject_id=tx_id,
                            decided_by="test", source="test", payload=proposal)
    group_id = uuid4()
    amounts = dict(conn.execute("SELECT id, abs(amount_minor) FROM l1_bank_transaction WHERE id = ANY(%s)",
                                (legs,)).fetchall())
    total = sum(amounts.values())
    for item in proposal["items"]:
        for leg in legs:
            part = item["amount_minor"] if len(legs) == 1 else item["amount_minor"] * amounts[leg] // total
            conn.execute(
                """
                INSERT INTO l3_match (company_id, transaction_id, document_id, internal_record_id,
                                      related_transaction_id, amount_minor, group_id, decision_id, source, method,
                                      created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test', 'human', 'test')
                """,
                (company_id, leg, item["document_id"], item["internal_record_id"], item["related_transaction_id"],
                 part, group_id, decision),
            )
    for leg in legs:
        conn.execute(
            """
            INSERT INTO l3_transaction_status (company_id, transaction_id, final_status, subtype, decision_id,
                                               source, method, created_by)
            VALUES (%s, %s, %s, %s, %s, 'test', 'human', 'test')
            """,
            (company_id, leg, proposal["final_status"], proposal["subtype"], decision),
        )


# ---------------------------------------------------------------------------
# allocations from the reference file
# ---------------------------------------------------------------------------


def expected_allocations(conn: psycopg.Connection, company, events: dict[str, UUID],
                         truth: dict[str, Any] | None = None) -> dict[tuple[str, UUID], dict[UUID, Fraction]]:
    """{(subject_type, subject_id): {cost_object_id: exact share}} as the file decides."""
    truth = truth or ground_truth()
    look = _lookups(conn, company.id)

    def shares(items: list[dict[str, Any]]) -> dict[UUID, Fraction]:
        return {events[i["event"]]: Fraction(i.get("share_exact") or i["share"]) for i in items}

    out: dict[tuple[str, UUID], dict[UUID, Fraction]] = {}

    def put_document(doc_id: UUID, value: dict[UUID, Fraction]) -> None:
        lines = [r[0] for r in conn.execute("SELECT id FROM l1_document_line WHERE document_id = %s",
                                            (doc_id,)).fetchall()]
        for line in lines or []:
            out[("line", line)] = value
        if not lines:
            out[("document", doc_id)] = value

    for invoice in truth["invoices"]:
        put_document(look["ksef"][invoice["ksef_number"]], shares(invoice["final_allocation"]))
    for document in truth["email_documents"]:
        if document.get("same_as_ksef"):
            continue
        put_document(look["numbers"][(normalize_invoice_number(document["number"]), "purchase")],
                     shares(document["final_allocation"]))
    for row in truth["pm_sheet_rows"]:
        out[("internal_record", look["rows"][row["sheet_row"]])] = {events[row["final_event"]]: Fraction(1)}
    for t in truth["transactions"]:
        tx_id = look["tx"][t["entry_reference"]][0]
        bank_only = t["expected_status"] == "matched" and all(
            d["doc_type"] == "bank_tx" for d in t.get("matched_documents") or [])
        if t["expected_status"] == "no_document" or bank_only:
            out[("transaction", tx_id)] = {events["UNASSIGNED"]: Fraction(1)}
    return out


@dataclass
class ClickStats:
    rank1: int = 0
    other_rank: int = 0
    manual: int = 0
    auto_ok: int = 0
    auto_wrong: list[str] = field(default_factory=list)
    no_expectation: list[str] = field(default_factory=list)
    manual_subjects: list[str] = field(default_factory=list)


def _proposal_shares(proposal: dict[str, Any]) -> dict[UUID, Fraction]:
    return {UUID(s["cost_object_id"]): Fraction(s.get("share_exact") or str(s["share"])) for s in proposal["shares"]}


def click_ground_truth(conn: psycopg.Connection, company, events: dict[str, UUID],
                       truth: dict[str, Any] | None = None) -> ClickStats:
    expected = expected_allocations(conn, company, events, truth)
    stats = ClickStats()
    for (subject_type, subject_id), (method,) in [
        ((r[0], r[1]), (r[2],)) for r in conn.execute(
            "SELECT DISTINCT subject_type::text, subject_id, method::text FROM l3_allocation "
            "WHERE company_id = %s AND status = 'active'", (company.id,)).fetchall()
    ]:
        want = expected.get((subject_type, subject_id))
        got = {r[0]: Fraction(Decimal(r[1])) for r in conn.execute(
            "SELECT cost_object_id, share FROM l3_allocation WHERE subject_type = %s AND subject_id = %s "
            "AND status = 'active'", (subject_type, subject_id)).fetchall()}
        if want is not None and set(got) == set(want) and all(abs(got[k] - want[k]) < Fraction(1, 100000) for k in got):
            stats.auto_ok += 1
        elif want is not None:
            stats.auto_wrong.append(f"{subject_type}:{subject_id} {got} != {want}")
    subjects = conn.execute(
        """
        SELECT subject_type::text, subject_id, array_agg(id ORDER BY rank), array_agg(proposal ORDER BY rank)
          FROM l2_suggestion WHERE company_id = %s AND kind = 'allocation' AND status = 'open'
         GROUP BY 1, 2
        """,
        (company.id,),
    ).fetchall()
    for subject_type, subject_id, ids, proposals in subjects:
        want = expected.get((subject_type, subject_id))
        if want is None:
            stats.no_expectation.append(f"{subject_type}:{subject_id}")
            continue
        chosen = None
        for rank, (sid, proposal) in enumerate(zip(ids, proposals, strict=True), start=1):
            got = _proposal_shares(proposal)
            if set(got) == set(want) and all(abs(got[k] - want[k]) < Fraction(1, 100000) for k in got):
                chosen = (rank, sid)
                break
        if chosen:
            apply_allocation(conn, chosen[1], decided_by="test", method="human")
            if chosen[0] == 1:
                stats.rank1 += 1
            else:
                stats.other_rank += 1
        else:
            allocate_manually(conn, company.id, subject_type, subject_id, [(k, v) for k, v in want.items()],
                              decided_by="test", reason="ground truth: Inne...")
            stats.manual += 1
            stats.manual_subjects.append(f"{subject_type}:{subject_id}")
    return stats
