"""Cash metrics (briefing, step 8): revenue by cash, balances of the accounts (the VAT account included),
inflows and outflows of a period, and costs without a document ("brakuje papieru")."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from kasa.core.metrics.base import Computed, Metric, MetricContext, MetricError, Period, matches_text, text_filter

SUBTYPE_PL = {"zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata", "interest": "odsetki",
              "advance": "zaliczka (czeka 180 dni na fakturę)", "deposit": "kaucja",
              "card_awaiting": "karta - czeka na fakturę (30 dni)", "salary": "wynagrodzenie", "refund": "zwrot",
              "duplicate": "duplikat", "other": "inne", None: "bez podtypu"}
THRESHOLD_MINOR = 10_000          # 100 zł


def _need_period(period: Period | None) -> Period:
    if period is None:
        raise MetricError("ta metryka potrzebuje okresu (np. 2026-10)")
    return period


def cash_revenue_rows(ctx: MetricContext, period: Period) -> dict[str, Any]:
    """Inflows booked in the period matched to sales documents: decided (l3_match) and only suggested."""
    decided = ctx.conn.execute(
        """
        SELECT m.transaction_id, m.amount_minor, m.decision_id, t.booking_date, d.kind::text, d.number_raw,
               CASE WHEN c.kind = 'person' THEN coalesce(c.name_pseudonymized, 'osoba')
                    ELSE coalesce(c.name_pseudonymized, c.name, d.buyer_name) END
          FROM l3_match m
          JOIN l1_bank_transaction t ON t.id = m.transaction_id
          JOIN l1_document d ON d.id = m.document_id
          LEFT JOIN l1_counterparty c ON c.id = d.counterparty_id
         WHERE m.company_id = %s AND m.status = 'active' AND d.direction = 'sales' AND t.amount_minor > 0
           AND t.status = 'booked' AND t.superseded_by IS NULL AND t.booking_date BETWEEN %s AND %s
        """,
        (ctx.company_id, period.start, period.end),
    ).fetchall()
    suggested = []
    for sid, tx_id, amount, proposal in ctx.conn.execute(
        """
        SELECT s.id, s.subject_id, t.amount_minor, s.proposal FROM l2_suggestion s
          JOIN l1_bank_transaction t ON t.id = s.subject_id
         WHERE s.company_id = %s AND s.kind = 'match' AND s.status = 'open' AND s.rank = 1
           AND s.subject_type = 'transaction' AND t.amount_minor > 0 AND t.status = 'booked'
           AND t.booking_date BETWEEN %s AND %s
           AND NOT EXISTS (SELECT 1 FROM l3_transaction_status x WHERE x.transaction_id = s.subject_id
                            AND x.status = 'active')
        """,
        (ctx.company_id, period.start, period.end),
    ).fetchall():
        items = [i for i in (proposal or {}).get("items") or [] if i.get("document_id")]
        if not items or (proposal or {}).get("final_status") != "matched":
            continue
        directions = {r[0] for r in ctx.conn.execute(
            "SELECT direction::text FROM l1_document WHERE id = ANY(%s::uuid[])",
            ([i["document_id"] for i in items],)).fetchall()}
        if directions == {"sales"}:
            suggested.append((sid, tx_id, sum(int(i.get("amount_minor") or 0) for i in items) or int(amount)))
    return {"decided": decided, "suggested": suggested}


class RevenueCashGross(Metric):
    key = "revenue_cash_gross"
    version = 1
    name_pl = "Przychód kasowy brutto"
    description_pl = ("Wpływy dopasowane (decyzją) do faktur sprzedaży, po dacie księgowania, brutto - w tym "
                      "zaliczki i obie nogi split payment; wpływy dopasowane tylko w sugestii osobno.")
    grain = "company"
    unit = "pln_minor"
    period_mode = "month"
    dimensions = ("counterparty", "day")
    traps_pl = ("W tym zaliczki (kasowo zaliczka jest wpływem, memoriałowo nie jest przychodem).",
                "Duplikat wpłaty i jego zwrot nie są przychodem (dopasowane do przelewu, nie do faktury).",
                "Wpływ dopasowany tylko w sugestii nie wchodzi - jest w udziale niepotwierdzonych.")
    uses = (cash_revenue_rows,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        period = _need_period(period)
        data = cash_revenue_rows(ctx, period)
        total = sum(int(r[1]) for r in data["decided"])
        pending = sum(r[2] for r in data["suggested"])
        values: list[dict[str, Any]] = []
        if "counterparty" in dimensions:
            per: dict[str, int] = defaultdict(int)
            for r in data["decided"]:
                per[r[6] or "-"] += int(r[1])
            values = [{"counterparty": k, "amount_minor": v} for k, v in sorted(per.items(), key=lambda kv: -kv[1])]
        elif "day" in dimensions:
            per_day: dict[str, int] = defaultdict(int)
            for r in data["decided"]:
                per_day[r[3].isoformat()] += int(r[1])
            values = [{"day": k, "amount_minor": v} for k, v in sorted(per_day.items())]
        advances = sum(int(r[1]) for r in data["decided"] if r[4] == "advance")
        return Computed(value=total, values=values,
                        detail={"advances_minor": advances, "matches": len(data["decided"]),
                                "suggested_minor": pending},
                        confirmed_minor=total, unconfirmed_minor=pending,
                        decisions=sorted({r[2] for r in data["decided"] if r[2]}, key=str),
                        suggestions=[r[0] for r in data["suggested"]], as_of=as_of)


def balances_on(ctx: MetricContext, as_of: date) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        """
        SELECT a.id, a.iban, a.kind::text, a.currency::text, coalesce(a.name, a.bank),
               (SELECT b.amount_minor FROM l1_bank_balance b
                 WHERE b.account_id = a.id AND b.superseded_by IS NULL AND b.balance_type = 'CLBD'
                   AND b.reference_date <= %(d)s ORDER BY b.reference_date DESC LIMIT 1),
               (SELECT b.reference_date FROM l1_bank_balance b
                 WHERE b.account_id = a.id AND b.superseded_by IS NULL AND b.balance_type = 'CLBD'
                   AND b.reference_date <= %(d)s ORDER BY b.reference_date DESC LIMIT 1),
               (SELECT t.balance_after_minor FROM l1_bank_transaction t
                 WHERE t.account_id = a.id AND t.status = 'booked' AND t.superseded_by IS NULL
                   AND t.booking_date <= %(d)s AND t.balance_after_minor IS NOT NULL
                 ORDER BY t.booking_date DESC, t.created_at DESC LIMIT 1),
               (SELECT coalesce(sum(t.amount_minor), 0) FROM l1_bank_transaction t
                 WHERE t.account_id = a.id AND t.status = 'pending' AND t.superseded_by IS NULL
                   AND t.resolved_by_transaction_id IS NULL AND coalesce(t.transaction_date, t.booking_date) <= %(d)s)
          FROM l1_bank_account a
         WHERE a.company_id = %(c)s
         ORDER BY a.kind, a.iban
        """,
        {"c": ctx.company_id, "d": as_of},
    ).fetchall()
    out = []
    for account_id, iban, kind, currency, name, clbd, clbd_date, after, pending in rows:
        balance = clbd if clbd is not None and (clbd_date == as_of or after is None) else (after if after is not None
                                                                                            else clbd)
        source = "saldo z banku (CLBD)" if balance is not None and balance == clbd else "saldo po ostatniej operacji"
        out.append({"account_id": str(account_id), "iban": iban, "account": name, "kind": kind, "currency": currency,
                    "balance_minor": int(balance) if balance is not None else None, "source": source,
                    "pending_holds_minor": int(pending), "available_minor":
                        int(balance) + int(pending) if balance is not None else None})
    return out


class AccountBalances(Metric):
    key = "account_balances"
    version = 1
    name_pl = "Saldo rachunków (w tym VAT)"
    description_pl = ("Saldo każdego rachunku na dzień: zamknięcie zgłoszone przez bank (CLBD), inaczej saldo po "
                      "ostatniej zaksięgowanej operacji; razem i osobno rachunek VAT; dostępne = saldo + blokady "
                      "kartowe (oczekujące).")
    grain = "company"
    unit = "pln_minor"
    period_mode = "as_of"
    dimensions = ("account",)
    traps_pl = ("Rachunek VAT to pieniądze firmy, ale tylko na podatki i dostawców (split payment) - osobna pozycja.",
                "Blokady kartowe nie wchodzą do salda (bilansu), pokazują tylko „ile naprawdę mam dostępne”.")
    uses = (balances_on,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        rows = balances_on(ctx, as_of)
        pln = [r for r in rows if r["currency"] == "PLN" and r["balance_minor"] is not None]
        total = sum(r["balance_minor"] for r in pln)
        vat = sum(r["balance_minor"] for r in pln if r["kind"] == "vat")
        return Computed(value=total, values=rows,
                        detail={"vat_minor": vat, "without_vat_minor": total - vat,
                                "available_minor": sum(r["available_minor"] for r in pln),
                                "pending_holds_minor": sum(r["pending_holds_minor"] for r in pln),
                                "accounts": len(rows)},
                        confirmed_minor=abs(total), as_of=as_of,
                        notes=[f"rachunek {r['iban']} w {r['currency']} poza sumą PLN" for r in rows
                               if r["currency"] != "PLN"])


class CashFlow(Metric):
    key = "cash_flow"
    version = 1
    name_pl = "Wpływy i wypływy okresu"
    description_pl = ("Zaksięgowane wpływy i wypływy wszystkich rachunków w okresie: z transferami własnymi i bez; "
                      "wpływy + wypływy = zmiana sald (transfery własne netto 0, gdy obie nogi w okresie).")
    grain = "company"
    unit = "pln_minor"
    period_mode = "month"
    dimensions = ("account",)
    traps_pl = ("Transfer własny (i przeksięgowanie z rachunku VAT) to nie wpływ ani wypływ firmy - pokazany osobno.",
                "Zmiana sald = wpływy + wypływy (ze znakiem) - różnicę wyjaśniają tylko transfery własne między "
                "okresami.")

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        period = _need_period(period)
        rows = ctx.conn.execute(
            """
            SELECT a.iban, a.kind::text,
                   coalesce(sum(t.amount_minor) FILTER (WHERE t.amount_minor > 0), 0),
                   coalesce(sum(t.amount_minor) FILTER (WHERE t.amount_minor < 0), 0),
                   coalesce(sum(t.amount_minor) FILTER (WHERE t.amount_minor > 0 AND own.own), 0),
                   coalesce(sum(t.amount_minor) FILTER (WHERE t.amount_minor < 0 AND own.own), 0),
                   count(t.id)
              FROM l1_bank_account a
              LEFT JOIN l1_bank_transaction t ON t.account_id = a.id AND t.status = 'booked'
                   AND t.superseded_by IS NULL AND t.booking_date BETWEEN %s AND %s
              LEFT JOIN LATERAL (SELECT (t.op_type = 'own' OR EXISTS (
                         SELECT 1 FROM l3_transaction_status s WHERE s.transaction_id = t.id AND s.status = 'active'
                            AND s.final_status = 'own_transfer')) AS own) own ON true
             WHERE a.company_id = %s
             GROUP BY a.iban, a.kind ORDER BY a.kind, a.iban
            """,
            (period.start, period.end, ctx.company_id),
        ).fetchall()
        values = [{"iban": r[0], "kind": r[1], "inflows_minor": int(r[2]), "outflows_minor": int(r[3]),
                   "own_in_minor": int(r[4]), "own_out_minor": int(r[5]), "operations": int(r[6]),
                   "change_minor": int(r[2]) + int(r[3])} for r in rows]
        inflows = sum(v["inflows_minor"] for v in values)
        outflows = sum(v["outflows_minor"] for v in values)
        own_in = sum(v["own_in_minor"] for v in values)
        own_out = sum(v["own_out_minor"] for v in values)
        return Computed(
            value=inflows + outflows, values=values,
            detail={"inflows_minor": inflows, "outflows_minor": outflows,
                    "inflows_without_own_minor": inflows - own_in, "outflows_without_own_minor": outflows - own_out,
                    "own_net_minor": own_in + own_out, "balance_change_minor": inflows + outflows},
            confirmed_minor=abs(inflows) + abs(outflows), as_of=as_of)


def no_document_outflows(ctx: MetricContext, period: Period) -> list[dict[str, Any]]:
    """Booked outflows of the period with a decided or proposed final status "no document" or "to clarify"."""
    rows = ctx.conn.execute(
        """
        SELECT t.id, t.entry_reference, t.booking_date, t.amount_minor, t.sensitive,
               CASE WHEN c.kind = 'person' THEN coalesce(c.name_pseudonymized, 'osoba')
                    WHEN c.id IS NOT NULL THEN coalesce(c.name_pseudonymized, c.name)
                    ELSE t.counterparty_pseudonym END,
               s.final_status::text, s.subtype, s.decision_id, s.method::text,
               g.id, g.proposal ->> 'final_status', g.proposal ->> 'subtype',
               (SELECT cd.category FROM l3_category_decision cd WHERE cd.subject_type = 'transaction'
                   AND cd.subject_id = t.id AND cd.status = 'active' LIMIT 1),
               kasa_tx_is_payroll(t.id),
               EXISTS (SELECT 1 FROM l3_match m WHERE m.transaction_id = t.id AND m.status = 'active'
                          AND (m.document_id IS NOT NULL OR m.internal_record_id IS NOT NULL))
          FROM l1_bank_transaction t
          LEFT JOIN l1_counterparty c ON c.id = t.counterparty_id
          LEFT JOIN l3_transaction_status s ON s.transaction_id = t.id AND s.status = 'active'
          LEFT JOIN l2_suggestion g ON g.subject_type = 'transaction' AND g.subject_id = t.id AND g.kind = 'match'
               AND g.status = 'open' AND g.rank = 1 AND s.id IS NULL
         WHERE t.company_id = %s AND t.status = 'booked' AND t.superseded_by IS NULL AND t.amount_minor < 0
           AND t.booking_date BETWEEN %s AND %s
         ORDER BY t.amount_minor, t.booking_date
        """,
        (ctx.company_id, period.start, period.end),
    ).fetchall()
    out = []
    for (tx_id, ref, day, amount, sensitive, party, status, subtype, decision, method, sid, proposed, proposed_sub,
         category, payroll, has_document) in rows:
        if has_document:
            continue
        final = status or proposed
        if final not in ("no_document", "to_clarify"):
            continue
        out.append({"transaction_id": tx_id, "entry_reference": ref, "booking_date": day, "amount_minor": int(amount),
                    "sensitive": bool(sensitive), "payroll": bool(payroll), "counterparty": party,
                    "final_status": final, "subtype": subtype if status else proposed_sub, "category": category,
                    "decided": status is not None, "decision_id": decision, "method": method, "suggestion_id": sid})
    return out


class CostsWithoutDocument(Metric):
    key = "costs_without_document"
    version = 1
    name_pl = "Koszty bez dokumentu (powyżej 100 zł)"
    description_pl = ("Zaksięgowane wypływy okresu bez faktury ani dokumentu wewnętrznego (status końcowy „bez "
                      "dokumentu” z decyzji albo z otwartej sugestii), o kwocie powyżej 100 zł; wynagrodzenia osób "
                      "i pozycje wrażliwe zbiorczo (bez osoby). Wartość = suma wypływów (dodatnia).")
    grain = "company"
    unit = "pln_minor"
    period_mode = "month"
    filters = {"counterparty": "fragment nazwy kontrahenta", "subtype": "zus | tax | fee | advance | salary | ...",
               "decided_only": "true: tylko z decyzją"}
    traps_pl = ("To sygnał „brakuje papieru”, nie koszt do P&L.",
                "Brak dokumentu to osobna hipoteza z podtypem (ZUS, podatek, zaliczka, prowizja...), nie „nie "
                "znaleźliśmy”; zaliczka czeka 180 dni na fakturę, karta 30 dni.",
                "Przelew „do wyjaśnienia” nie jest „bez dokumentu” - liczony osobno (detail.to_clarify).",
                "Wynagrodzenia osób tylko zbiorczo - płace per osoba nie są dostępne.",
                "Tytuł ze słowem wrażliwym (komornik, zdrowie...) - tylko zbiorczo, szczegóły w kolejce.")
    uses = (no_document_outflows,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        period = _need_period(period)
        wanted_party = text_filter(filters.get("counterparty"))
        wanted_subtype = filters.get("subtype")
        decided_only = str(filters.get("decided_only", "")).lower() in ("1", "true", "tak", "yes")
        rows = no_document_outflows(ctx, period)
        clarify = [r for r in rows if r["final_status"] == "to_clarify"]
        items = [r for r in rows if r["final_status"] == "no_document" and abs(r["amount_minor"]) > THRESHOLD_MINOR
                 and (not decided_only or r["decided"])
                 and (not wanted_subtype or r["subtype"] == wanted_subtype)
                 and matches_text(r["counterparty"] if not (r["payroll"] or r["sensitive"]) else None, wanted_party)]
        values: list[dict[str, Any]] = []
        groups: dict[str, dict[str, Any]] = {}
        for r in items:
            hidden = "payroll" if r["payroll"] else ("sensitive" if r["sensitive"] else None)
            if hidden:
                label = ("wynagrodzenia osób (zbiorczo, osoby niewidoczne)" if hidden == "payroll"
                         else "pozycje ze słowem wrażliwym (zbiorczo, szczegóły tylko w kolejce)")
                group = groups.setdefault(hidden, {"group": hidden, "label": label, "count": 0, "amount_minor": 0,
                                                   "decided": 0, "in_suggestions": 0})
                group["count"] += 1
                group["amount_minor"] += r["amount_minor"]
                group["decided" if r["decided"] else "in_suggestions"] += 1
                continue
            values.append({"entry_reference": r["entry_reference"], "booking_date": r["booking_date"].isoformat(),
                           "amount_minor": r["amount_minor"], "counterparty": r["counterparty"],
                           "subtype": r["subtype"], "subtype_pl": SUBTYPE_PL.get(r["subtype"], r["subtype"]),
                           "category": r["category"], "status": "decyzja" if r["decided"] else "sugestia",
                           "method": r["method"]})
        values += list(groups.values())
        total = -sum(r["amount_minor"] for r in items)
        pending = -sum(r["amount_minor"] for r in items if not r["decided"])
        return Computed(
            value=total, values=values,
            detail={"count": len(items), "threshold_minor": THRESHOLD_MINOR,
                    "to_clarify": {"count": len(clarify), "amount_minor": -sum(r["amount_minor"] for r in clarify)},
                    "by_subtype_minor": _by_subtype(items)},
            confirmed_minor=total - pending, unconfirmed_minor=pending,
            decisions=[r["decision_id"] for r in items if r["decision_id"]],
            suggestions=[r["suggestion_id"] for r in items if r["suggestion_id"]], as_of=as_of)


def _by_subtype(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in items:
        out[r["subtype"] or "bez podtypu"] += -r["amount_minor"]
    return dict(out)


METRICS = (RevenueCashGross, AccountBalances, CashFlow, CostsWithoutDocument)
