"""One matching run of a company: evaluate -> suggestions (L2) -> automation (L3) -> report."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from kasa.core.matching import auto
from kasa.core.matching.engine import Evaluation, evaluate
from kasa.core.matching.suggestions import save_result


@dataclass
class RunReport:
    company: str
    period: tuple[date, date] | None
    dry_run: bool
    params: dict[str, float]
    subjects: int = 0
    auto: int = 0
    queue: int = 0
    no_document: int = 0
    ties: int = 0
    suggestions_created: int = 0
    suggestions_unchanged: int = 0
    suggestions_expired: int = 0
    auto_errors: list[str] = field(default_factory=list)
    by_rule: Counter = field(default_factory=Counter)
    by_rank1: Counter = field(default_factory=Counter)
    decisions: list[auto.AutoDecision] = field(default_factory=list)
    evaluation: Evaluation | None = None

    def summary(self) -> str:
        scope = f"{self.period[0]:%Y-%m}" if self.period else "wszystkie miesiące"
        head = "SYMULACJA (nic nie zapisano) - " if self.dry_run else ""
        lines = [
            f"{head}Dopasowanie {self.company}, {scope}: przelewów do rozstrzygnięcia {self.subjects}",
            f"  automat: {self.auto}" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(self.by_rule.items()))})"
                                         if self.by_rule else ""),
            (f"  do kolejki: {self.queue} (w tym bez dokumentu jako pierwsza propozycja: {self.no_document}, "
             f"remisy: {self.ties})"),
            "  pierwsza propozycja: " + ", ".join(f"{k} {v}" for k, v in sorted(self.by_rank1.items())),
        ]
        if not self.dry_run:
            lines.append(f"  sugestie: nowe {self.suggestions_created}, bez zmian {self.suggestions_unchanged} "
                         f"przelewów, wygaszone {self.suggestions_expired}")
        if self.auto_errors:
            lines.append("  automat odmówił: " + "; ".join(self.auto_errors))
        return "\n".join(lines)


def run_matching(conn: psycopg.Connection, company: Any, *, period: tuple[date, date] | None = None,
                 dry_run: bool = False, auto_decide: bool = True,
                 params: dict[str, float] | None = None) -> RunReport:
    """Suggest matches for every undecided booked transaction; close what the automation may close.

    ``dry_run``: nothing is written (the report says what would happen).
    """
    evaluation = evaluate(conn, company.id, period=period, params=params)
    report = RunReport(company.slug, period, dry_run, evaluation.params, evaluation=evaluation)
    rules = evaluation.ctx.builtin_rules
    for result in evaluation.results:
        report.subjects += 1
        best = result.best
        if best is None:
            continue
        report.by_rank1[f"{best.final_status}" + (f"/{best.subtype}" if best.subtype else "")] += 1
        ok, _why = auto.eligibility(best, evaluation.params, rules) if auto_decide else (False, "")
        if dry_run:
            if ok:
                report.auto += 1
                report.by_rule[auto.licence(best, rules)["rule_key"]] += 1
            else:
                _count_queue(report, best)
            continue
        saved = save_result(conn, company.id, result, evaluation.params)
        report.suggestions_created += saved.created
        report.suggestions_expired += saved.expired
        report.suggestions_unchanged += saved.unchanged
        if ok and saved.suggestion_ids:
            decision = auto.close(conn, result.tx.id, saved.suggestion_ids[0], best, rules)
            report.decisions.append(decision)
            if decision.decision_id:
                report.auto += 1
                report.by_rule[decision.rule_key] += 1
                continue
            report.auto_errors.append(f"{result.tx.label}: {decision.error}")
        _count_queue(report, best)
    return report


def _count_queue(report: RunReport, best) -> None:
    report.queue += 1
    if best.final_status == "no_document":
        report.no_document += 1
    if best.tie:
        report.ties += 1
