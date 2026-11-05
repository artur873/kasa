"""Metrics of the company report (briefing, steps 5 and 8): revenue by accrual, margin per cost object,
fully loaded margin, cash contribution, the four rows of the company and the coverage of allocation.

All of them are views of ONE implementation - ``kasa.core.reports.company.company_report`` (memorial and
cash, control difference 0,00 by construction). The report of the company is the composition of these
metrics; nothing here recomputes an amount in a second way.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any
from uuid import UUID

from kasa.core.allocation.amounts import distribute
from kasa.core.metrics.base import Computed, Metric, MetricContext, MetricError, Period, matches_text, share, text_filter
from kasa.core.reports.company import CompanyReport, company_report


def report_for(ctx: MetricContext, month: str) -> CompanyReport:
    return ctx.cached(("company_report", month), lambda: company_report(ctx.conn, ctx.company_id, month))


def object_windows(ctx: MetricContext) -> dict[UUID, tuple[date | None, date | None]]:
    def load():
        out = {}
        for oid, attrs in ctx.conn.execute(
            "SELECT id, attrs FROM l3_cost_object WHERE company_id = %s", (ctx.company_id,)
        ).fetchall():
            attrs = attrs or {}
            try:
                start = date.fromisoformat(str(attrs.get("date_start"))) if attrs.get("date_start") else None
                end = date.fromisoformat(str(attrs.get("date_end"))) if attrs.get("date_end") else start
            except ValueError:
                start = end = None
            out[oid] = (start, end)
        return out

    return ctx.cached("object_windows", load)


def allocation_provenance(ctx: MetricContext, object_ids: set[UUID] | None) -> tuple[list[UUID], list[UUID]]:
    """Decisions (allocations in force) and open rank-1 suggestions behind amounts of cost objects."""
    decisions = [r[0] for r in ctx.conn.execute(
        """
        SELECT DISTINCT decision_id FROM l3_allocation
         WHERE company_id = %s AND status = 'active' AND decision_id IS NOT NULL
           AND (%s::uuid[] IS NULL OR cost_object_id = ANY(%s::uuid[]))
        """,
        (ctx.company_id, list(object_ids) if object_ids is not None else None,
         list(object_ids) if object_ids is not None else None),
    ).fetchall()]
    suggestions = []
    wanted = {str(o) for o in object_ids} if object_ids is not None else None
    for sid, proposal in ctx.conn.execute(
        "SELECT id, proposal FROM l2_suggestion WHERE company_id = %s AND kind = 'allocation' AND status = 'open' "
        "AND rank = 1", (ctx.company_id,)
    ).fetchall():
        targets = {str(s.get("cost_object_id")) for s in (proposal or {}).get("shares") or []}
        if wanted is None or targets & wanted:
            suggestions.append(sid)
    return decisions, suggestions


def _month_of(period: Period | None, ctx: MetricContext, as_of: date | None) -> list[str]:
    if period is None:
        raise MetricError("ta metryka potrzebuje okresu (np. 2026-10)")
    return period.months


def _select_objects(report: CompanyReport, wanted: str | None, include_unassigned: bool = True):
    out = []
    for line in report.objects:
        unassigned = line.kind == "unassigned" or line.name == "UNASSIGNED"
        if unassigned and not include_unassigned:
            continue
        if wanted and not (matches_text(line.name, wanted) or str(line.cost_object_id) == wanted):
            continue
        out.append((line, unassigned))
    return out


class MarginAccrual(Metric):
    key = "margin_accrual"
    version = 1
    name_pl = "Marża per obiekt kosztowy (memoriałowa)"
    description_pl = ("Przychód przypisany minus koszt przypisany, za cały obiekt (także koszty sprzed miesiąca), "
                      "wg alokacji w mocy; UNASSIGNED osobnym wierszem; kwoty jeszcze w otwartych sugestiach osobno "
                      "(udział niepotwierdzonych). Okres zawęża listę do obiektów, których okno dat go przecina.")
    grain = "cost_object"
    unit = "pln_minor"
    period_mode = "optional"
    dimensions = ("cost_object",)
    filters = {"cost_object": "nazwa (fragment, alternatywy przez |) albo id obiektu kosztowego"}
    traps_pl = ("UNASSIGNED (koszt ogólny) to decyzja, nie brak decyzji - osobny wiersz, nie część marży obiektów.",
                "„W pełni obciążona” to osobna metryka (margin_fully_loaded), nie ta.",
                "Faktura zbiorcza liczy się wg ZATWIERDZONEGO podziału (kaskada + decyzja), nigdy w całości na "
                "jeden obiekt i nigdy wg własnego podziału pytającego.",
                "Kwota w otwartej sugestii nie wchodzi do marży - jest w udziale niepotwierdzonych.",
                "Marża obiektu kosztowego, nie osoby (kasa nie ocenia pracowników).")
    uses = (report_for, object_windows, allocation_provenance, _select_objects)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        month = period.months[-1] if period else f"{as_of:%Y-%m}"
        report = report_for(ctx, month)
        wanted = text_filter(filters.get("cost_object"))
        windows = object_windows(ctx)
        rows, selected = [], []
        for line, unassigned in _select_objects(report, wanted):
            if period is not None and not unassigned:
                start, end = windows.get(line.cost_object_id, (None, None))
                if start is not None and (end < period.start or start > period.end):
                    continue
            rows.append({"cost_object_id": str(line.cost_object_id), "cost_object": line.name,
                         "status": line.status, "is_unassigned": unassigned,
                         "revenue_minor": line.revenue_minor, "cost_minor": line.cost_minor,
                         "margin_minor": line.margin_minor, "revenue_pending_minor": line.revenue_pending_minor,
                         "cost_pending_minor": line.cost_pending_minor,
                         "pending_cost_share": round(line.pending_cost_share, 4),
                         "pending_revenue_share": round(line.pending_revenue_share, 4)})
            if not unassigned:
                selected.append(line)
        if not selected and len(rows) == 1:          # the filter asked for UNASSIGNED itself
            selected = [line for line, _ in _select_objects(report, wanted)]
        value = sum(o.margin_minor for o in selected)
        confirmed = sum(o.revenue_minor + o.cost_minor for o in selected)
        pending = sum(o.revenue_pending_minor + o.cost_pending_minor for o in selected)
        decisions, suggestions = allocation_provenance(ctx, {o.cost_object_id for o in selected})
        notes = [f"{o.name}: obiekt czeka na zatwierdzenie w karcie - jego kwoty są tylko w sugestiach"
                 for o in selected if o.status != "approved"]
        if wanted and not rows:
            notes.append("żaden obiekt kosztowy nie pasuje do filtra")
        best = max(selected, key=lambda o: o.margin_minor, default=None)
        worst = min(selected, key=lambda o: o.margin_minor, default=None)
        return Computed(
            value=value, values=rows,
            detail={"objects": len(selected),
                    "highest": {"cost_object": best.name, "margin_minor": best.margin_minor} if best else None,
                    "lowest": {"cost_object": worst.name, "margin_minor": worst.margin_minor} if worst else None,
                    "cost_pending_share": share(sum(o.cost_pending_minor for o in selected),
                                                sum(o.cost_minor + o.cost_pending_minor for o in selected)),
                    "revenue_pending_share": share(sum(o.revenue_pending_minor for o in selected),
                                                   sum(o.revenue_minor + o.revenue_pending_minor for o in selected))},
            confirmed_minor=confirmed, unconfirmed_minor=pending, decisions=decisions, suggestions=suggestions,
            as_of=as_of, notes=notes)


class MarginFullyLoaded(Metric):
    key = "margin_fully_loaded"
    version = 1
    name_pl = "Marża w pełni obciążona (per obiekt, miesiąc)"
    description_pl = ("Wynik memoriałowy obiektu w okresie (przychód minus koszt okresu) minus udział kosztu "
                      "ogólnego (UNASSIGNED) okresu, rozdzielonego proporcjonalnie do przychodu obiektów w tym "
                      "okresie (co do grosza, największa reszta).")
    grain = "cost_object"
    unit = "pln_minor"
    period_mode = "month"
    filters = {"cost_object": "nazwa (fragment) albo id obiektu kosztowego"}
    traps_pl = ("To NIE jest marża obiektu (margin_accrual) - podział kosztu ogólnego wg przychodu to konwencja "
                "zarządcza, nie fakt; nigdy jeden KPI z marżą memoriałową.",
                "Liczona tylko za okres (miesiąc): koszty obiektu sprzed okresu nie wchodzą.",
                "Przychód UNASSIGNED nie jest kosztem ogólnym - dzielony jest tylko koszt UNASSIGNED.")
    uses = (report_for, distribute)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        wanted = text_filter(filters.get("cost_object"))
        totals: dict[UUID, dict[str, Any]] = {}
        overhead_total = 0
        for month in _month_of(period, ctx, as_of):
            report = report_for(ctx, month)
            objects = [o for o in report.objects if o.kind != "unassigned" and o.name != "UNASSIGNED"]
            overhead = report.memorial["unassigned"].cost_minor
            overhead_total += overhead
            weights = [max(o.month_revenue_minor, 0) for o in objects]
            parts = distribute(overhead, weights) if objects and sum(weights) else [0] * len(objects)
            for obj, part in zip(objects, parts, strict=True):
                row = totals.setdefault(obj.cost_object_id, {"cost_object_id": str(obj.cost_object_id),
                                                             "cost_object": obj.name, "revenue_minor": 0,
                                                             "cost_minor": 0, "overhead_minor": 0})
                row["revenue_minor"] += obj.month_revenue_minor
                row["cost_minor"] += obj.month_cost_minor
                row["overhead_minor"] += part
        rows = []
        for row in totals.values():
            if wanted and not (matches_text(row["cost_object"], wanted) or row["cost_object_id"] == wanted):
                continue
            row["result_minor"] = row["revenue_minor"] - row["cost_minor"]
            row["fully_loaded_minor"] = row["result_minor"] - row["overhead_minor"]
            rows.append(row)
        rows.sort(key=lambda r: -r["fully_loaded_minor"])
        value = sum(r["fully_loaded_minor"] for r in rows)
        return Computed(value=value, values=rows,
                        detail={"overhead_unassigned_cost_minor": overhead_total,
                                "split_basis": "przychód obiektów w okresie"},
                        confirmed_minor=sum(abs(r["revenue_minor"]) + abs(r["cost_minor"]) for r in rows),
                        as_of=as_of, metrics_used=[MarginAccrual.ref()])


class CashContribution(Metric):
    key = "cash_contribution"
    version = 1
    name_pl = "Cash contribution per obiekt kosztowy"
    description_pl = ("Wpływy minus wypływy okresu przypisane obiektom (przelew → dopasowanie → dokument → jego "
                      "alokacja; przelew bez dokumentu → jego alokacja), osobno UNASSIGNED, niesparowane i "
                      "transfery własne; suma = zmiana sald rachunków z konstrukcji (różnica kontrolna 0,00).")
    grain = "cost_object"
    unit = "pln_minor"
    period_mode = "month"
    filters = {"cost_object": "nazwa (fragment) albo id obiektu kosztowego"}
    traps_pl = ("Suma obiektów + UNASSIGNED + niesparowane + transfery własne = zmiana salda, z konstrukcji; "
                "różnica kontrolna inna niż 0,00 to błąd danych (kontrola prawdy 1).",
                "Transfer własny bez decyzji (osoby albo reguły wbudowanej) jest niesparowany (detal "
                "unmatched_own), nie „własny”.",
                "Noga VAT split payment to osobny przelew dopasowany do tej samej faktury - każdy liczony raz.")
    uses = (report_for,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        wanted = text_filter(filters.get("cost_object"))
        per: dict[UUID, dict[str, Any]] = {}
        rows_total: dict[str, int] = defaultdict(int)
        detail: dict[str, int] = defaultdict(int)
        control = difference = 0
        for month in _month_of(period, ctx, as_of):
            report = report_for(ctx, month)
            for line in report.objects:
                row = per.setdefault(line.cost_object_id, {"cost_object_id": str(line.cost_object_id),
                                                           "cost_object": line.name,
                                                           "is_unassigned": line.kind == "unassigned"
                                                           or line.name == "UNASSIGNED", "cash_minor": 0})
                row["cash_minor"] += line.cash_minor
            for key in ("objects", "unassigned", "unmatched", "own"):
                rows_total[key] += report.cash[key]
            for key, value in report.cash_detail.items():
                detail[key] += value
            control += report.cash_control_minor
            difference += report.cash_difference_minor
        rows = [r for r in per.values() if not wanted or matches_text(r["cost_object"], wanted)
                or r["cost_object_id"] == wanted]
        rows.sort(key=lambda r: (r["is_unassigned"], -r["cash_minor"]))
        if wanted:
            value = sum(r["cash_minor"] for r in rows)
            unconfirmed = 0
        else:
            value = rows_total["objects"]
            unconfirmed = abs(rows_total["unmatched"])
        volume = sum(abs(v) for v in rows_total.values())
        return Computed(
            value=value, values=rows,
            detail={"rows_minor": {"objects": rows_total["objects"], "unassigned": rows_total["unassigned"],
                                   "unmatched": rows_total["unmatched"], "own": rows_total["own"]},
                    "unmatched_detail_minor": dict(detail), "balance_change_minor": control,
                    "control_difference_minor": difference},
            confirmed_minor=max(volume - unconfirmed, 0), unconfirmed_minor=unconfirmed, as_of=as_of,
            notes=([] if difference == 0 else ["różnica kontrolna kasowa inna niż 0,00 - sprawdź ciągłość sald"]))


class CompanyResult(Metric):
    key = "company_result"
    version = 1
    name_pl = "Raport firmy: wynik memoriałowy i kasowy okresu"
    description_pl = ("Dwa ujęcia, nigdy jeden KPI: memoriałowo (przychód po dacie sprzedaży minus koszt, jak "
                      "widzi biuro) i kasowo (wpływy minus wypływy, jak widzi bank); po cztery wiersze: suma "
                      "obiektów, UNASSIGNED, nieprzypisane / niesparowane, kontrola; różnica kontrolna 0,00. "
                      "Wartość = wynik memoriałowy okresu (wiersz kontrolny).")
    grain = "company"
    unit = "pln_minor"
    period_mode = "month"
    traps_pl = ("Memoriałowo ≠ kasowo: łączy je „w drodze” = należności + zobowiązania; to dwie liczby, nie jedna.",
                "Zaliczka i proforma nie są przychodem; okres usługi rozkłada się po dniach.",
                "Różnica kontrolna inna niż 0,00 to błąd systemu, nie szum.")
    uses = (report_for,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        memorial: dict[str, dict[str, int]] = {k: {"revenue_minor": 0, "cost_minor": 0, "result_minor": 0}
                                               for k in ("objects", "unassigned", "unallocated", "control")}
        cash: dict[str, int] = defaultdict(int)
        cash_detail: dict[str, int] = defaultdict(int)
        diffs = {"memorial_difference_minor": 0, "cash_difference_minor": 0}
        pending = 0
        notes: list[str] = []
        for month in _month_of(period, ctx, as_of):
            report = report_for(ctx, month)
            for key, row in report.memorial.items():
                memorial[key]["revenue_minor"] += row.revenue_minor
                memorial[key]["cost_minor"] += row.cost_minor
                memorial[key]["result_minor"] += row.result_minor
            for key in ("objects", "unassigned", "unmatched", "own"):
                cash[key] += report.cash[key]
            cash["control"] += report.cash_control_minor
            for key, value in report.cash_detail.items():
                cash_detail[key] += value
            diffs["memorial_difference_minor"] += report.memorial_difference_minor
            diffs["cash_difference_minor"] += report.cash_difference_minor
            pending += report.memorial["unallocated"].revenue_minor + report.memorial["unallocated"].cost_minor
            notes += report.notes
        volume = memorial["control"]["revenue_minor"] + memorial["control"]["cost_minor"]
        values = [{"row": k, "memorial_minor": memorial[k]["result_minor"],
                   "cash_minor": cash["unmatched" if k == "unallocated" else k]}
                  for k in ("objects", "unassigned", "unallocated", "control")]
        return Computed(
            value=memorial["control"]["result_minor"], values=values,
            detail={"memorial": memorial, "cash_minor": dict(cash), "cash_unmatched_detail_minor": dict(cash_detail),
                    **diffs, "revenue_minor": memorial["control"]["revenue_minor"],
                    "cost_minor": memorial["control"]["cost_minor"], "balance_change_minor": cash["control"]},
            confirmed_minor=max(volume - pending, 0), unconfirmed_minor=pending, as_of=as_of, notes=notes)


class RevenueAccrualNet(Metric):
    key = "revenue_accrual_net"
    version = 1
    name_pl = "Przychód memoriałowy netto"
    description_pl = ("Faktury sprzedaży minus korekty, po dacie sprzedaży (okres usługi rozłożony po dniach), "
                      "netto w PLN (waluta po kursie dokumentu); wymiar obiekt kosztowy wg alokacji w mocy.")
    grain = "company"
    unit = "pln_minor"
    period_mode = "month"
    dimensions = ("cost_object", "month")
    traps_pl = ("Zaliczka nigdy nie jest przychodem; proforma też nie.",
                "Okres usługi 1-31.05 rozkłada się po dniach (część w jednym miesiącu, część w drugim).",
                "Faktura rozliczeniowa (ROZ) = całe zamówienie; korekta = różnica.")
    uses = (report_for,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        total = 0
        by_month: list[dict[str, Any]] = []
        per_object: dict[str, int] = defaultdict(int)
        buckets: dict[str, int] = defaultdict(int)
        for month in _month_of(period, ctx, as_of):
            report = report_for(ctx, month)
            revenue = report.memorial["control"].revenue_minor
            total += revenue
            by_month.append({"month": month, "revenue_minor": revenue})
            for line in report.objects:
                if line.month_revenue_minor:
                    per_object[line.name] += line.month_revenue_minor
            for key in ("objects", "unassigned", "unallocated"):
                buckets[key] += report.memorial[key].revenue_minor
        values: list[dict[str, Any]] = by_month
        unconfirmed = 0
        if "cost_object" in dimensions:
            values = [{"cost_object": k, "revenue_minor": v} for k, v in sorted(per_object.items(), key=lambda kv: -kv[1])]
            values.append({"cost_object": "nieprzypisane (bez decyzji alokacji)", "revenue_minor": buckets["unallocated"]})
            unconfirmed = abs(buckets["unallocated"])
        return Computed(value=total, values=values, detail={"by_bucket_minor": dict(buckets)},
                        confirmed_minor=max(abs(total) - unconfirmed, 0), unconfirmed_minor=unconfirmed, as_of=as_of)


class AllocationCoverage(Metric):
    key = "allocation_coverage"
    version = 1
    name_pl = "Pokrycie alokacji"
    description_pl = ("Udział kwot memoriałowych okresu (przychód + koszt) przypisanych obiektom kosztowym; osobno "
                      "UNASSIGNED (decyzja: koszt ogólny) i „jeszcze nie patrzyłem” (bez decyzji), w tym część, "
                      "która czeka w otwartych sugestiach.")
    grain = "company"
    unit = "ratio"
    period_mode = "month"
    traps_pl = ("UNASSIGNED to decyzja („koszt ogólny”), „jeszcze nie patrzyłem” to brak decyzji - dwa różne wiersze.",
                "Sugestia nie jest przypisaniem: kwota w sugestii jest w „jeszcze nie patrzyłem”.")
    uses = (report_for,)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        volume: dict[str, int] = defaultdict(int)
        costs: dict[str, int] = defaultdict(int)
        pending = 0
        pending_costs = 0
        for month in _month_of(period, ctx, as_of):
            report = report_for(ctx, month)
            for key in ("objects", "unassigned", "unallocated"):
                row = report.memorial[key]
                volume[key] += abs(row.revenue_minor) + abs(row.cost_minor)
                costs[key] += abs(row.cost_minor)
            for row in report.memorial_pending.values():
                pending += abs(row.revenue_minor) + abs(row.cost_minor)
                pending_costs += abs(row.cost_minor)
        total = sum(volume.values())
        total_costs = sum(costs.values())
        rows = [{"row": "obiekty kosztowe", "amount_minor": volume["objects"], "amount_share": share(volume["objects"], total)},
                {"row": "UNASSIGNED (decyzja: koszt ogólny)", "amount_minor": volume["unassigned"],
                 "amount_share": share(volume["unassigned"], total)},
                {"row": "jeszcze nie patrzyłem (bez decyzji)", "amount_minor": volume["unallocated"],
                 "amount_share": share(volume["unallocated"], total)},
                {"row": "  w tym w otwartych sugestiach", "amount_minor": pending, "amount_share": share(pending, total)}]
        return Computed(
            value=share(volume["objects"], total), values=rows,
            detail={"total_minor": total, "objects_minor": volume["objects"], "unassigned_minor": volume["unassigned"],
                    "not_looked_minor": volume["unallocated"], "in_suggestions_minor": pending,
                    "costs": {"total_minor": total_costs, "objects_minor": costs["objects"],
                              "unassigned_minor": costs["unassigned"], "not_looked_minor": costs["unallocated"],
                              "in_suggestions_minor": pending_costs,
                              "not_looked_share": share(costs["unallocated"], total_costs)}},
            confirmed_minor=total - volume["unallocated"], unconfirmed_minor=pending, as_of=as_of)


METRICS = (MarginAccrual, MarginFullyLoaded, CashContribution, CompanyResult, RevenueAccrualNet, AllocationCoverage)


def known_objects(ctx: MetricContext) -> list[tuple[UUID, str, str]]:
    """Cost objects approved or waiting for their card (system objects apart)."""
    return [(r[0], r[1], r[2]) for r in ctx.conn.execute(
        "SELECT id, name, status::text FROM l3_cost_object WHERE company_id = %s AND system_code IS NULL "
        "AND status IN ('approved', 'candidate') ORDER BY name", (ctx.company_id,)).fetchall()]


class MarginAccrualV2(MarginAccrual):
    """Version 2: an object waiting for its card is listed (zero, with a note) and a filter that matches no
    object is a refusal - never a plausible "0,00 zł" for an object that does not exist (v1 answered 0)."""

    version = 2
    traps_pl = (*MarginAccrual.traps_pl,
                "Obiekt-kandydat (bez karty) ma marżę 0 z adnotacją - jego kwoty są tylko w sugestiach.",
                "Filtr, który nie pasuje do żadnego obiektu, to odmowa, nie „0,00 zł”.")
    uses = (*MarginAccrual.uses, known_objects)

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        result = super().compute(ctx, period=period, as_of=as_of, dimensions=dimensions, filters=filters)
        wanted = text_filter(filters.get("cost_object"))
        listed = {row["cost_object_id"] for row in result.values}
        for object_id, name, status in known_objects(ctx):
            if str(object_id) in listed or (wanted and not (matches_text(name, wanted) or str(object_id) == wanted)):
                continue
            result.values.append({"cost_object_id": str(object_id), "cost_object": name, "status": status,
                                  "is_unassigned": False, "revenue_minor": 0, "cost_minor": 0, "margin_minor": 0,
                                  "revenue_pending_minor": 0, "cost_pending_minor": 0, "pending_cost_share": 0.0,
                                  "pending_revenue_share": 0.0})
            if status != "approved":
                result.notes.append(f"{name}: kandydat na obiekt kosztowy czeka na zatwierdzenie w karcie - bez "
                                    "alokacji, marża jeszcze nieznana")
        result.notes = [n for n in result.notes if n != "żaden obiekt kosztowy nie pasuje do filtra"]
        if wanted and not result.values:
            raise MetricError(f"żaden obiekt kosztowy nie pasuje do filtra {filters.get('cost_object')!r} - "
                              "sprawdź nazwę (query_view cost_objects)")
        return result


METRICS = (*METRICS, MarginAccrualV2)


class MarginAccrualV3(MarginAccrualV2):
    """Version 3: when every object of the answer still waits for its card, there is no number at all
    (``value = None``) - v2 answered "0,00 zł" next to the note, which reads like a margin of zero."""

    version = 3
    traps_pl = (*MarginAccrualV2.traps_pl[:-2],
                "Obiekt-kandydat (bez karty) nie ma marży: odpowiedź bez liczby (value = null) z adnotacją.",
                MarginAccrualV2.traps_pl[-1])

    def compute(self, ctx: MetricContext, *, period: Period | None, as_of: date | None, dimensions: list[str],
                filters: dict[str, Any]) -> Computed:
        result = super().compute(ctx, period=period, as_of=as_of, dimensions=dimensions, filters=filters)
        objects = [r for r in result.values if not r["is_unassigned"]]
        if objects and all(r["status"] != "approved" for r in objects):
            result.value = None
        return result


METRICS = (*METRICS, MarginAccrualV3)
