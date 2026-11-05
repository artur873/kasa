"""Company report for a month: two views that are never one KPI (briefing, steps 5 and 8).

**Memorial (accrual)** - revenue of sales documents by sale date (corrections as the difference,
advance = 0, proforma = 0, a service period spread over its days) and cost of purchase documents,
bills, receipts and internal rows (``kasa.core.allocation.amounts``), split by the allocations in
force. One cost, many pieces of evidence: a row that is evidence of a document is not counted
(precedence document > internal record > transfer; ``kasa.core.allocation.subjects``).

**Cash** - every booked transfer of every account in the month, split: transfer -> match
(``l3_match``, 4A) -> document -> its allocation (by the weight of its lines); a row of an
internal file matched to a transfer -> its allocation; a transfer without a document -> its own
allocation; a transfer with nothing = "niesparowane" (not looked at yet). Own transfers (and the
VAT rebooking) are a line of their own, they net to zero when both legs are in the month; the legs
of a split payment are two transfers matched to one invoice, each counted once.

Four rows in both views: sum of the cost objects, UNASSIGNED, not allocated / not matched, the
control (all documents of the month; the change of the balances of all accounts). An own transfer counts
as "own" only with a decision in force (``l3_transaction_status`` = ``own_transfer``, a person's or the
built-in rule's); one that only looks like an own transfer (operation type / counterparty "own") without a
decision is "niesparowane" with the detail ``unmatched_own`` (review of 4A/4B, point 9). The control
difference is 0.00 by construction - every amount is split to the grosz (largest remainder) into
exactly one row. Per object: memorial revenue, cost and margin (the whole object, not only the
month), cash contribution of the month, and the part of the amounts still in open suggestions.

Phase 5: this module is THE implementation behind the metrics ``company_result``, ``margin_accrual``,
``margin_fully_loaded``, ``cash_contribution``, ``revenue_accrual_net`` and ``allocation_coverage``
(``kasa/core/metrics/definitions/``) - the report of the company is the composition of those metrics, one
definition for the command line, the dashboard and the agent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.allocation.amounts import (
    distribute,
    load_documents,
    load_internal_records,
    split_by_month,
)
from kasa.core.allocation.objects import load_profiles, resolve
from kasa.core.allocation.subjects import evidence_of_documents, load_matches

UNALLOCATED = "__unallocated__"
UNMATCHED = "__unmatched__"
OWN = "__own__"


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    start = date(year, mon, 1)
    end = date(year + (mon == 12), mon % 12 + 1, 1) - timedelta(days=1)
    return start, end


@dataclass
class MemorialRow:
    revenue_minor: int = 0
    cost_minor: int = 0

    @property
    def result_minor(self) -> int:
        return self.revenue_minor - self.cost_minor

    def add(self, revenue: int, cost: int) -> None:
        self.revenue_minor += revenue
        self.cost_minor += cost


@dataclass
class ObjectLine:
    cost_object_id: UUID
    name: str
    kind: str
    status: str
    revenue_minor: int = 0
    cost_minor: int = 0
    month_revenue_minor: int = 0
    month_cost_minor: int = 0
    cash_minor: int = 0
    revenue_pending_minor: int = 0
    cost_pending_minor: int = 0

    @property
    def margin_minor(self) -> int:
        return self.revenue_minor - self.cost_minor

    @property
    def pending_cost_share(self) -> float:
        total = self.cost_minor + self.cost_pending_minor
        return self.cost_pending_minor / total if total else 0.0

    @property
    def pending_revenue_share(self) -> float:
        total = self.revenue_minor + self.revenue_pending_minor
        return self.revenue_pending_minor / total if total else 0.0


@dataclass
class CompanyReport:
    company_id: UUID
    month: str
    period: tuple[date, date]
    memorial: dict[str, MemorialRow] = field(default_factory=dict)
    # month amounts without an allocation that wait in open rank-1 suggestions (a part of "unallocated"):
    # "objects" / "unassigned" by the proposed target
    memorial_pending: dict[str, MemorialRow] = field(default_factory=dict)
    cash: dict[str, int] = field(default_factory=dict)
    cash_detail: dict[str, int] = field(default_factory=dict)
    objects: list[ObjectLine] = field(default_factory=list)
    balance_change_minor: int = 0
    balance_change_reported_minor: int | None = None
    control_source: str = "transactions"
    evidence_links: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def memorial_difference_minor(self) -> int:
        control = self.memorial["control"].result_minor
        return control - sum(self.memorial[k].result_minor for k in ("objects", "unassigned", "unallocated"))

    @property
    def cash_control_minor(self) -> int:
        if self.balance_change_reported_minor is not None:
            return self.balance_change_reported_minor
        return self.balance_change_minor

    @property
    def cash_difference_minor(self) -> int:
        return self.cash_control_minor - sum(self.cash[k] for k in ("objects", "unassigned", "unmatched", "own"))

    def object_line(self, name: str) -> ObjectLine | None:
        return next((o for o in self.objects if o.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "memorial": {k: {"revenue_minor": v.revenue_minor, "cost_minor": v.cost_minor,
                             "result_minor": v.result_minor} for k, v in self.memorial.items()},
            "memorial_difference_minor": self.memorial_difference_minor,
            "memorial_pending": {k: {"revenue_minor": v.revenue_minor, "cost_minor": v.cost_minor}
                                 for k, v in self.memorial_pending.items()},
            "cash": dict(self.cash), "cash_detail": dict(self.cash_detail),
            "cash_control_minor": self.cash_control_minor, "control_source": self.control_source,
            "cash_difference_minor": self.cash_difference_minor,
            "objects": [{"cost_object_id": str(o.cost_object_id), "name": o.name, "revenue_minor": o.revenue_minor,
                         "cost_minor": o.cost_minor, "margin_minor": o.margin_minor, "cash_minor": o.cash_minor,
                         "revenue_pending_minor": o.revenue_pending_minor,
                         "cost_pending_minor": o.cost_pending_minor,
                         "pending_cost_share": round(o.pending_cost_share, 4)} for o in self.objects],
            "evidence_links": self.evidence_links, "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def evidence_documents(records, documents, matches) -> dict[UUID, dict[str, Any]]:
    """Rows of internal files that are evidence of a document ("one cost, many pieces of evidence").

    Decided by matching first (0013: a row ``evidence_only`` in the group of the document, or a row matched to
    the same transfer as a document); the guess "same person, amount and date as a bill of a person" only for
    rows without any active match, marked ``presumption`` (an assumption, not a decision).
    """
    covered = evidence_of_documents(records, documents, matches, person_heuristic=False)
    guessed = evidence_of_documents(records, documents, matches, person_heuristic=True)
    for record_id, link in guessed.items():
        if record_id in covered or record_id in matches.transactions_of_record:
            continue
        covered[record_id] = {**link, "presumption": True}
    return covered


def _active_allocations(conn: psycopg.Connection, company_id: UUID) -> dict[tuple[str, UUID], list[tuple[UUID, int]]]:
    out: dict[tuple[str, UUID], list[tuple[UUID, int]]] = defaultdict(list)
    for subject_type, subject_id, cost_object_id, share in conn.execute(
        """
        SELECT subject_type::text, subject_id, cost_object_id, share FROM l3_allocation
         WHERE company_id = %s AND status = 'active' ORDER BY subject_type, subject_id, cost_object_id
        """,
        (company_id,),
    ).fetchall():
        out[(subject_type, subject_id)].append((cost_object_id, int(Decimal(share) * 1_000_000)))
    return out


def _pending(conn: psycopg.Connection, company_id: UUID) -> dict[tuple[str, UUID], list[tuple[UUID, int]]]:
    """Rank-1 open allocation suggestions (what is still waiting for the person)."""
    out: dict[tuple[str, UUID], list[tuple[UUID, int]]] = {}
    for subject_type, subject_id, proposal in conn.execute(
        """
        SELECT subject_type::text, subject_id, proposal FROM l2_suggestion
         WHERE company_id = %s AND kind = 'allocation' AND status = 'open' AND rank = 1
        """,
        (company_id,),
    ).fetchall():
        shares = []
        for item in (proposal or {}).get("shares") or []:
            shares.append((UUID(str(item["cost_object_id"])), round(float(item["share"]) * 1_000_000)))
        out[(subject_type, subject_id)] = shares
    return out


def _split(amount: int, shares: list[tuple[UUID, int]], profiles) -> list[tuple[str | UUID, int]]:
    """Amount split by micro-shares (residual of shares below 100% -> not allocated)."""
    if not shares:
        return [(UNALLOCATED, amount)]
    keys: list[str | UUID] = []
    weights: list[int] = []
    for cost_object_id, micro in shares:
        target = resolve(profiles, cost_object_id)
        keys.append(target.id if target else cost_object_id)
        weights.append(micro)
    rest = 1_000_000 - sum(weights)
    if rest > 0:
        keys.append(UNALLOCATED)
        weights.append(rest)
    parts = distribute(amount, weights)
    return list(zip(keys, parts, strict=True))


def _reported_balance_change(conn: psycopg.Connection, company_id: UUID, start: date, end: date) -> int | None:
    accounts = [r[0] for r in conn.execute("SELECT id FROM l1_bank_account WHERE company_id = %s",
                                           (company_id,)).fetchall()]
    if not accounts:
        return None
    total = 0
    for account in accounts:
        opening = conn.execute(
            """
            SELECT amount_minor FROM l1_bank_balance
             WHERE account_id = %s AND superseded_by IS NULL
               AND ((balance_type = 'OPBD' AND reference_date = %s) OR (balance_type = 'CLBD' AND reference_date < %s))
             ORDER BY (balance_type = 'OPBD') DESC, reference_date DESC LIMIT 1
            """,
            (account, start, start),
        ).fetchone()
        closing = conn.execute(
            """
            SELECT amount_minor FROM l1_bank_balance
             WHERE account_id = %s AND superseded_by IS NULL AND balance_type = 'CLBD' AND reference_date <= %s
             ORDER BY reference_date DESC LIMIT 1
            """,
            (account, end),
        ).fetchone()
        if opening is None or closing is None:
            return None
        total += closing[0] - opening[0]
    return total


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def company_report(conn: psycopg.Connection, company_id: UUID, month: str) -> CompanyReport:
    start, end = month_bounds(month)
    report = CompanyReport(company_id=company_id, month=month, period=(start, end))
    profiles = load_profiles(conn, company_id)
    documents = load_documents(conn, company_id)
    records = load_internal_records(conn, company_id)
    matches = load_matches(conn, company_id)
    covered = evidence_documents(records, documents, matches)
    allocations = _active_allocations(conn, company_id)
    pending = _pending(conn, company_id)
    unassigned_id = next(p.id for p in profiles.values() if p.system_code == "UNASSIGNED")

    lines: dict[UUID, ObjectLine] = {}

    def line_for(key) -> ObjectLine | None:
        if not isinstance(key, UUID):
            return None
        profile = profiles.get(key)
        if profile is None:
            return None
        if key not in lines:
            lines[key] = ObjectLine(cost_object_id=key, name=profile.name, kind=profile.kind, status=profile.status)
        return lines[key]

    for row in ("objects", "unassigned", "unallocated", "control"):
        report.memorial[row] = MemorialRow()
    for row in ("objects", "unassigned"):
        report.memorial_pending[row] = MemorialRow()

    def memorial_item(subject: tuple[str, UUID], amount: int, sales: bool, window: tuple[date, date]) -> None:
        month_part = split_by_month(amount, window).get(month, 0)
        revenue_sign = 1 if sales else 0
        for key, part in _split(amount, allocations.get(subject, []), profiles):
            obj = line_for(key)
            if obj is not None:
                if sales:
                    obj.revenue_minor += part
                else:
                    obj.cost_minor += part
        # month view
        report.memorial["control"].add(month_part * revenue_sign, month_part * (1 - revenue_sign))
        for key, part in _split(month_part, allocations.get(subject, []), profiles):
            bucket = ("unallocated" if key == UNALLOCATED else "unassigned" if key == unassigned_id else "objects")
            report.memorial[bucket].add(part * revenue_sign, part * (1 - revenue_sign))
            obj = line_for(key)
            if obj is not None:
                if sales:
                    obj.month_revenue_minor += part
                else:
                    obj.month_cost_minor += part
        if not allocations.get(subject) and subject in pending:
            for key, part in _split(amount, pending[subject], profiles):
                obj = line_for(key)
                if obj is not None:
                    if sales:
                        obj.revenue_pending_minor += part
                    else:
                        obj.cost_pending_minor += part
            for key, part in _split(month_part, pending[subject], profiles):
                if key == UNALLOCATED:
                    continue
                bucket = "unassigned" if key == unassigned_id else "objects"
                report.memorial_pending[bucket].add(part * revenue_sign, part * (1 - revenue_sign))

    for doc in documents.values():
        sales = doc.direction == "sales"
        window = doc.recognition_window
        if doc.lines:
            for ln in doc.lines:
                memorial_item(("line", ln.id), ln.memorial_minor, sales, window)
        else:
            memorial_item(("document", doc.id), doc.memorial_minor, sales, window)
    for record in records.values():
        if record.id in covered:
            link = covered[record.id]
            entry = {"internal_record_id": str(record.id), "document_id": str(link["document_id"]),
                     "reason": link["reason"]}
            if link.get("group_id"):
                entry["group_id"] = str(link["group_id"])
            if link.get("presumption"):
                entry["presumption"] = True
            report.evidence_links.append(entry)
            continue
        day = record.record_date or start
        memorial_item(("internal_record", record.id), record.memorial_minor, False, (day, day))

    # ---- cash ----------------------------------------------------------------
    for key in ("objects", "unassigned", "unmatched", "own"):
        report.cash[key] = 0
    for key in ("unmatched_no_match", "unmatched_document_not_allocated", "unmatched_own"):
        report.cash_detail[key] = 0
    states = dict(conn.execute(
        "SELECT transaction_id, final_status::text FROM l3_transaction_status WHERE company_id = %s "
        "AND status = 'active'", (company_id,)).fetchall())
    txs = conn.execute(
        """
        SELECT t.id, t.amount_minor, t.op_type::text, cp.kind::text
          FROM l1_bank_transaction t LEFT JOIN l1_counterparty cp ON cp.id = t.counterparty_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked'
           AND t.booking_date BETWEEN %s AND %s
        """,
        (company_id, start, end),
    ).fetchall()

    def cash_add(key, amount: int, detail: str = "unmatched_no_match") -> None:
        if key == UNALLOCATED:
            report.cash["unmatched"] += amount
            report.cash_detail[detail] += amount
            return
        if key == unassigned_id:
            report.cash["unassigned"] += amount
        else:
            report.cash["objects"] += amount
        obj = line_for(key)
        if obj is not None:
            obj.cash_minor += amount

    def document_parts(doc_id: UUID, amount: int) -> list[tuple[Any, int]]:
        doc = documents.get(doc_id)
        if doc is None:
            return [(UNALLOCATED, amount)]
        out: list[tuple[Any, int]] = []
        if doc.lines:
            weights = [ln.cash_weight_minor for ln in doc.lines]
            for ln, part in zip(doc.lines, distribute(amount, weights), strict=True):
                out += _split(part, allocations.get(("line", ln.id), []), profiles)
        else:
            out += _split(amount, allocations.get(("document", doc.id), []), profiles)
        return out

    for tx_id, amount, op_type, cp_kind in txs:
        report.balance_change_minor += amount
        status = states.get(tx_id)
        if status == "own_transfer":
            report.cash["own"] += amount
            continue
        if status is None and (op_type == "own" or cp_kind == "own"):
            # looks like an own transfer, but nobody (person or built-in rule) decided it yet
            report.cash["unmatched"] += amount
            report.cash_detail["unmatched_own"] += amount
            continue
        items = matches.by_transaction.get(tx_id, [])
        chosen = [i for i in items if i["document_id"]] or [i for i in items if i["internal_record_id"]
                                                             and not i.get("evidence_only")]
        sign = 1 if amount >= 0 else -1
        parts: list[tuple[dict, int]] = []
        if chosen:
            raw = [sign * i["amount_minor"] for i in chosen]
            if abs(sum(raw)) > abs(amount):
                raw = distribute(amount, [i["amount_minor"] for i in chosen])
                report.notes.append(f"przelew {tx_id}: dopasowania przekraczają kwotę - rozdzielono proporcjonalnie")
            parts = list(zip(chosen, raw, strict=True))
        residual = amount - sum(p for _, p in parts)
        for item, part in parts:
            if item["document_id"]:
                for key, value in document_parts(item["document_id"], part):
                    cash_add(key, value, "unmatched_document_not_allocated")
            else:
                for key, value in _split(part, allocations.get(("internal_record", item["internal_record_id"]), []),
                                         profiles):
                    cash_add(key, value, "unmatched_document_not_allocated")
        if residual:
            for key, value in _split(residual, allocations.get(("transaction", tx_id), []), profiles):
                cash_add(key, value, "unmatched_no_match")

    reported = _reported_balance_change(conn, company_id, start, end)
    report.balance_change_reported_minor = reported
    report.control_source = "bank_balances" if reported is not None else "transactions"
    if reported is not None and reported != report.balance_change_minor:
        report.notes.append(f"saldo z banku zmieniło się o {reported / 100:.2f}, a suma operacji to "
                            f"{report.balance_change_minor / 100:.2f} - sprawdź kontrolę ciągłości sald")
    report.objects = sorted(
        (o for o in lines.values() if profiles[o.cost_object_id].system_code is None),
        key=lambda o: (profiles[o.cost_object_id].window or (date.max, date.max), o.name))
    unassigned_line = lines.get(unassigned_id)
    if unassigned_line is not None:
        report.objects.append(unassigned_line)
    return report


def fmt_pln(minor: int) -> str:
    """12 345,67 (Polish grouping and decimal comma)."""
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(int(minor)), 100)
    return f"{sign}{whole:,}".replace(",", " ") + f",{cents:02d}"


def render(report: CompanyReport, cost_object_label: str = "obiekty kosztowe") -> str:
    """Plain text table for the command line."""
    m = report.memorial
    rows = [
        (f"Suma: {cost_object_label}", m["objects"].result_minor, report.cash["objects"]),
        ("UNASSIGNED (koszt ogólny)", m["unassigned"].result_minor, report.cash["unassigned"]),
        ("Nieprzypisane / niesparowane (jeszcze nie patrzyłeś)", m["unallocated"].result_minor,
         report.cash["unmatched"]),
        ("Kontrola (wszystkie dokumenty / zmiana sald rachunków)", m["control"].result_minor,
         report.cash_control_minor),
    ]
    out = [f"Raport firmy za {report.month} ({report.period[0]} - {report.period[1]})",
           "Dwa ujęcia, nigdy jeden wskaźnik: memoriałowe (wynik netto z dokumentów) i kasowe (wpływy - wypływy).",
           "", f"{'':58}{'memoriałowo':>18}{'kasowo':>18}"]
    for label, a, b in rows:
        out.append(f"{label:58}{fmt_pln(a):>18}{fmt_pln(b):>18}")
    out.append(f"{'Różnica kontrolna':58}{fmt_pln(report.memorial_difference_minor):>18}"
               f"{fmt_pln(report.cash_difference_minor):>18}")
    out.append(f"  w tym transfery własne netto (kasowo): {fmt_pln(report.cash['own'])}; kontrola kasowa z: "
               + ("sald banku" if report.control_source == "bank_balances" else "sumy operacji"))
    out.append(f"  memoriałowo przychód {fmt_pln(m['control'].revenue_minor)}, koszt {fmt_pln(m['control'].cost_minor)}"
               f"; niesparowane kasowo: bez dopasowania {fmt_pln(report.cash_detail['unmatched_no_match'])}, "
               f"dokument bez alokacji {fmt_pln(report.cash_detail['unmatched_document_not_allocated'])}"
               + (f", transfery własne bez decyzji {fmt_pln(report.cash_detail['unmatched_own'])}"
                  if report.cash_detail.get("unmatched_own") else ""))
    out.append("")
    out.append(f"{'Obiekt':34}{'przychód':>14}{'koszt':>14}{'marża':>14}{'kasowo (mies.)':>16}  w sugestiach")
    for o in report.objects:
        pending = f"koszt {o.pending_cost_share:.0%}, przychód {o.pending_revenue_share:.0%}"
        out.append(f"{o.name[:33]:34}{fmt_pln(o.revenue_minor):>14}{fmt_pln(o.cost_minor):>14}"
                   f"{fmt_pln(o.margin_minor):>14}{fmt_pln(o.cash_minor):>16}  {pending}")
    if report.evidence_links:
        out.append("")
        out.append(f"Jeden koszt, wiele dowodów: {len(report.evidence_links)} wierszy plików wewnętrznych liczonych "
                   "jako dowód dokumentu (nie drugi raz).")
    for note in report.notes:
        out.append(f"Uwaga: {note}")
    return "\n".join(out)
