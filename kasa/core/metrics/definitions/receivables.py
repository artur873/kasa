"""Receivables and payables open on a day, their ageing, and DSO 90 (briefing, step 8).

Open amount of an invoice = gross of its chain (the invoice and its corrections) minus the payments decided
(active ``l3_match``, booked on or before the day). The age: days after the due date; without a due date
(optional in FA(3)) - days after the issue date, marked ``due_date_missing``. Buckets: not due, 1-30, 31-60,
61-90, >90. A document seen only in a history file (JPK, office export) and issued before the bank history
starts is not counted: its payment, if any, happened before kasa sees the bank - it is listed apart.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from kasa.core.metrics.base import (
    Computed,
    Metric,
    MetricContext,
    Period,
    matches_text,
    text_filter,
)
from kasa.core.metrics.state import coverage_start

BUCKETS = ("nie_wymagalne", "1-30", "31-60", "61-90", ">90")
LIVE_CHANNELS = ("ksef", "pdf", "email", "bank")


def bucket_of(age: int) -> str:
    if age <= 0:
        return "nie_wymagalne"
    if age <= 30:
        return "1-30"
    if age <= 60:
        return "31-60"
    if age <= 90:
        return "61-90"
    return ">90"


def _pln(amount: int, currency: str, fx_rate: Decimal | None) -> int:
    if currency == "PLN" or fx_rate is None:
        return int(amount)
    return int((Decimal(amount) * Decimal(fx_rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def open_documents(ctx: MetricContext, direction: str, as_of: date) -> dict[str, Any]:
    """Open chains of one direction on ``as_of`` (the same code for receivables and payables)."""
    conn, company_id = ctx.conn, ctx.company_id
    rows = conn.execute(
        """
        SELECT d.id, d.kind::text, d.number_raw, d.ksef_number, d.issue_date, d.due_date, d.currency::text,
               d.gross_minor, d.fx_rate, d.counterparty_id,
               (SELECT l.head_id FROM document_lineage l WHERE l.document_id = d.corrects_document_id),
               EXISTS (SELECT 1 FROM l1_document_observation o
                         JOIN document_lineage l ON l.document_id = o.document_id
                         JOIN l0_raw r ON r.id = o.raw_id
                        WHERE l.head_id = d.id AND r.channel::text = ANY(%(live)s)),
               CASE WHEN c.kind = 'person' THEN coalesce(c.name_pseudonymized, 'osoba')
                    WHEN c.id IS NOT NULL THEN coalesce(c.name_pseudonymized, c.name)
                    WHEN d.kind = 'bill' THEN 'osoba'
                    WHEN d.direction = 'sales' THEN d.buyer_name ELSE d.seller_name END
          FROM l1_document d
          LEFT JOIN l1_counterparty c ON c.id = d.counterparty_id
         WHERE d.company_id = %(c)s AND d.superseded_by IS NULL AND d.direction::text = %(dir)s
           AND d.kind <> 'proforma' AND d.issue_date <= %(as_of)s
         ORDER BY d.issue_date, d.number_raw
        """,
        {"c": company_id, "dir": direction, "as_of": as_of, "live": list(LIVE_CHANNELS)},
    ).fetchall()
    docs = {r[0]: {"id": r[0], "kind": r[1], "number": r[2], "ksef_number": r[3], "issue_date": r[4],
                   "due_date": r[5], "currency": r[6], "gross_minor": int(r[7]), "fx_rate": r[8],
                   "counterparty_id": r[9], "corrects": r[10], "live": r[11], "counterparty": r[12]}
            for r in rows}
    paid: dict[UUID, int] = defaultdict(int)
    decisions: dict[UUID, set[UUID]] = defaultdict(set)
    for head, amount, decision in conn.execute(
        """
        SELECT l.head_id, coalesce(m.document_amount_minor, m.amount_minor), m.decision_id
          FROM l3_match m
          JOIN document_lineage l ON l.document_id = m.document_id
          JOIN l1_bank_transaction t ON t.id = m.transaction_id
         WHERE m.company_id = %s AND m.status = 'active' AND t.booking_date <= %s
        """,
        (company_id, as_of),
    ).fetchall():
        paid[head] += int(amount)
        if decision:
            decisions[head].add(decision)
    suggested: dict[UUID, int] = defaultdict(int)
    suggestion_ids: dict[UUID, set[UUID]] = defaultdict(set)
    for sid, proposal in conn.execute(
        """
        SELECT s.id, s.proposal FROM l2_suggestion s
          JOIN l1_bank_transaction t ON t.id = s.subject_id
         WHERE s.company_id = %s AND s.kind = 'match' AND s.status = 'open' AND s.rank = 1
           AND s.subject_type = 'transaction' AND t.booking_date <= %s
           AND NOT EXISTS (SELECT 1 FROM l3_transaction_status x WHERE x.transaction_id = s.subject_id
                            AND x.status = 'active')
        """,
        (company_id, as_of),
    ).fetchall():
        for item in (proposal or {}).get("items") or []:
            doc_id = item.get("document_id")
            if not doc_id:
                continue
            key = UUID(str(doc_id))
            suggested[key] += abs(int(item.get("document_amount_minor") or item.get("amount_minor") or 0))
            suggestion_ids[key].add(sid)
    unresolved: dict[tuple[UUID | None, int], list[str]] = defaultdict(list)
    for party, amount, ref in conn.execute(
        """
        SELECT t.counterparty_id, abs(t.amount_minor), coalesce(t.entry_reference, t.id::text)
          FROM l3_transaction_status s JOIN l1_bank_transaction t ON t.id = s.transaction_id
         WHERE s.company_id = %s AND s.status = 'active' AND s.final_status = 'to_clarify'
           AND t.booking_date <= %s AND (t.amount_minor > 0) = %s
        """,
        (company_id, as_of, direction == "sales"),
    ).fetchall():
        unresolved[(party, int(amount))].append(ref)

    def root_of(doc: dict[str, Any]) -> UUID:
        seen = set()
        while doc["kind"] == "correction" and doc["corrects"] in docs and doc["id"] not in seen:
            seen.add(doc["id"])
            doc = docs[doc["corrects"]]
        return doc["id"]

    chains: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs.values():
        chains[root_of(doc)].append(doc)
    start = coverage_start(ctx.conn, ctx.company_id)
    items, excluded = [], []
    for root_id, chain in chains.items():
        root = docs[root_id]
        gross = sum(d["gross_minor"] for d in chain)
        settled = sum(paid.get(d["id"], 0) for d in chain)
        open_minor = gross - settled
        if open_minor == 0:
            continue
        if start is not None and not any(d["live"] for d in chain) and root["issue_date"] < start:
            excluded.append({"number": root["number"], "issue_date": root["issue_date"].isoformat(),
                             "gross_minor": _pln(gross, root["currency"], root["fx_rate"]),
                             "reason": "dokument tylko z pliku historii (JPK/biuro) sprzed wyciągów - "
                                       "zapłata niewidoczna"})
            continue
        if root["due_date"] is not None:
            age, basis = (as_of - root["due_date"]).days, "po terminie płatności"
        else:
            age, basis = (as_of - root["issue_date"]).days, "od daty wystawienia (brak terminu płatności)"
        pending = sum(suggested.get(d["id"], 0) for d in chain)
        items.append({
            "document_id": str(root_id), "number": root["number"], "ksef_number": root["ksef_number"],
            "kind": root["kind"], "counterparty": root["counterparty"],
            "issue_date": root["issue_date"].isoformat(),
            "due_date": root["due_date"].isoformat() if root["due_date"] else None,
            "due_date_missing": root["due_date"] is None, "doc_currency": root["currency"],
            "gross_doc_minor": gross, "settled_doc_minor": settled, "open_doc_minor": open_minor,
            "open_minor": _pln(open_minor, root["currency"], root["fx_rate"]),
            "gross_minor": _pln(gross, root["currency"], root["fx_rate"]),
            "settled_minor": _pln(settled, root["currency"], root["fx_rate"]),
            "age_days": age, "age_basis": basis, "bucket": bucket_of(age),
            "corrections": [d["number"] for d in chain if d["id"] != root_id],
            "pending_payment_minor": _pln(pending, root["currency"], root["fx_rate"]),
            "unresolved_payments": sorted(unresolved.get((root["counterparty_id"], abs(open_minor)), [])),
            "_decisions": sorted({x for d in chain for x in decisions.get(d["id"], set())}, key=str),
            "_suggestions": sorted({x for d in chain for x in suggestion_ids.get(d["id"], set())}, key=str),
        })
    items.sort(key=lambda i: (-i["open_minor"], i["issue_date"], i["number"]))
    return {"items": items, "excluded": excluded, "coverage_start": start}


class _OpenDocuments(Metric):
    grain = "document"
    unit = "pln_minor"
    period_mode = "as_of"
    dimensions = ("bucket", "counterparty")
    filters = {"counterparty": "fragment nazwy kontrahenta (alternatywy przez |)",
               "bucket": "nie_wymagalne | 1-30 | 31-60 | 61-90 | >90",
               "overdue_only": "true: tylko po terminie",
               "issue_month": "miesiąc wystawienia RRRR-MM"}
    direction = "sales"
    uses = (open_documents, bucket_of, _pln)
    uses_modules = ("kasa.core.metrics.state",)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        data = open_documents(ctx, self.direction, as_of)
        wanted_party = text_filter(filters.get("counterparty"))
        wanted_bucket = filters.get("bucket")
        overdue = str(filters.get("overdue_only", "")).lower() in ("1", "true", "tak", "yes")
        month = filters.get("issue_month")
        items = [i for i in data["items"]
                 if matches_text(i["counterparty"], wanted_party)
                 and (not wanted_bucket or i["bucket"] == wanted_bucket)
                 and (not overdue or i["bucket"] != "nie_wymagalne")
                 and (not month or i["issue_date"].startswith(str(month)))]
        by_bucket = {b: 0 for b in BUCKETS}
        for i in items:
            by_bucket[i["bucket"]] += i["open_minor"]
        total = sum(i["open_minor"] for i in items)
        pending = sum(min(i["pending_payment_minor"], max(i["open_minor"], 0)) for i in items)
        decisions = [x for i in items for x in i.pop("_decisions")]
        suggestions = [x for i in items for x in i.pop("_suggestions")]
        values: list[dict[str, Any]] = items
        if "counterparty" in dimensions:
            per: dict[str, int] = defaultdict(int)
            for i in items:
                per[i["counterparty"] or "-"] += i["open_minor"]
            values = [{"counterparty": k, "open_minor": v} for k, v in sorted(per.items(), key=lambda kv: -kv[1])]
        elif "bucket" in dimensions:
            values = [{"bucket": b, "open_minor": by_bucket[b]} for b in BUCKETS]
        notes = []
        if any(i["due_date_missing"] for i in items):
            notes.append("część faktur nie ma terminu płatności - wiek liczony od daty wystawienia (oznaczone)")
        if data["excluded"]:
            notes.append(f"{len(data['excluded'])} dokumentów z plików historii sprzed wyciągów nie jest liczonych "
                         "(zapłata niewidoczna dla kasy) - lista w detail.excluded_history")
        return Computed(
            value=total, values=values,
            detail={"by_bucket_minor": by_bucket, "count": len(items),
                    "overdue_minor": total - by_bucket["nie_wymagalne"],
                    "due_date_missing_count": sum(1 for i in items if i["due_date_missing"]),
                    "excluded_history": data["excluded"],
                    "coverage_start": data["coverage_start"].isoformat() if data["coverage_start"] else None},
            confirmed_minor=max(total - pending, 0), unconfirmed_minor=pending,
            decisions=decisions, suggestions=suggestions, as_of=as_of, notes=notes)


class ReceivablesOpen(_OpenDocuments):
    key = "receivables_open"
    version = 1
    name_pl = "Należności otwarte i wiekowanie"
    description_pl = ("Faktury sprzedaży: brutto łańcucha (faktura + korekty) minus zatwierdzone wpłaty (aktywne "
                      "dopasowania zaksięgowane do dnia), na dzień; kubełki: niewymagalne, 1-30, 31-60, 61-90, "
                      ">90 dni po terminie. Posortowane po kwocie.")
    traps_pl = ("Termin płatności bywa niepodany (opcjonalny w FA(3)) - wtedy wiek liczony od daty wystawienia "
                "i oznaczony due_date_missing; brak terminu nie znaczy „płatne od ręki”.",
                "Korekta to osobny dokument spięty z pierwotnym: saldo z łańcucha, nigdy nie scalamy.",
                "Wpłata tylko w sugestii (nieklikniętej) nie zmniejsza należności - jest w udziale niepotwierdzonych.",
                "Dokument tylko z JPK sprzed wyciągów nie jest należnością (zapłaty nie widać) - lista osobno.",
                "Proforma nie jest należnością.")
    direction = "sales"


class PayablesOpen(_OpenDocuments):
    key = "payables_open"
    version = 1
    name_pl = "Zobowiązania otwarte i wiekowanie"
    description_pl = ("Faktury zakupu, rachunki osób, paragony i faktury zagraniczne: brutto łańcucha minus "
                      "zatwierdzone wypłaty, na dzień; kubełki jak należności.")
    traps_pl = ("Remis (dwie faktury na tę samą kwotę, jeden przelew „do wyjaśnienia”) - obie zostają otwarte, "
                "przelew w unresolved_payments, dopóki nie zdecydujesz.",
                "Termin płatności bywa niepodany - wiek od wystawienia, oznaczony.",
                "Dokument tylko z JPK sprzed wyciągów nie jest zobowiązaniem (zapłaty nie widać).")
    direction = "purchase"


def sales_gross(ctx: MetricContext, start: date, end: date) -> int:
    row = ctx.conn.execute(
        """
        SELECT coalesce(sum(coalesce(d.gross_pln_minor, d.gross_minor)), 0) FROM l1_document d
         WHERE d.company_id = %s AND d.superseded_by IS NULL AND d.direction = 'sales' AND d.kind <> 'proforma'
           AND d.issue_date BETWEEN %s AND %s
        """,
        (ctx.company_id, start, end),
    ).fetchone()
    return int(row[0])


class Dso90(Metric):
    key = "dso_90"
    version = 1
    name_pl = "DSO 90 (dni sprzedaży w należnościach)"
    description_pl = ("Należności otwarte na dzień (receivables_open@1) / sprzedaż brutto wystawiona w 90 dniach "
                      "kończących się tym dniem × 90.")
    grain = "company"
    unit = "days"
    period_mode = "as_of"
    traps_pl = ("Jedna definicja, jedna wersja: sprzedaż = brutto faktur sprzedaży (z korektami i zaliczkowymi, "
                "bez proform) wystawionych w oknie 90 dni; należności = metryka receivables_open@1.",)
    uses = (sales_gross, open_documents)
    uses_modules = ("kasa.core.metrics.state",)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        receivables = ReceivablesOpen().compute(ctx, period=None, as_of=as_of, dimensions=[], filters={})
        start = as_of - timedelta(days=89)
        sales = sales_gross(ctx, start, as_of)
        value = round(receivables.value / sales * 90, 1) if sales else None
        return Computed(
            value=value, values=[],
            detail={"receivables_minor": receivables.value, "sales_90_minor": sales,
                    "window": {"from": start.isoformat(), "to": as_of.isoformat()}},
            confirmed_minor=receivables.confirmed_minor, unconfirmed_minor=receivables.unconfirmed_minor,
            decisions=receivables.decisions, suggestions=receivables.suggestions, as_of=as_of,
            notes=([] if sales else ["brak sprzedaży w oknie 90 dni - DSO nieokreślone"]),
            metrics_used=[ReceivablesOpen.ref()])


METRICS = (ReceivablesOpen, PayablesOpen, Dso90)
