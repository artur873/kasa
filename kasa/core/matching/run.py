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
    passes: int = 0                     # extra passes after a reservation the automation refused

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
        if self.passes:
            lines.append(f"  przeliczono ponownie bez rezerwacji, które nie stały się decyzją: {self.passes}×")
        return "\n".join(lines)


MAX_PASSES = 3


@dataclass
class _Outcome:
    rank1: str
    automatic: bool
    no_document: bool
    tie: bool


def run_matching(conn: psycopg.Connection, company: Any, *, period: tuple[date, date] | None = None,
                 dry_run: bool = False, auto_decide: bool = True,
                 params: dict[str, float] | None = None, today: date | None = None) -> RunReport:
    """Suggest matches for every undecided booked transaction; close what the automation may close.

    ``dry_run``: nothing is written (the report says what would happen). Only a rank 1 the automation
    may close reserves its invoice for the later transfers of the run; when the automation then refuses
    it (a stale suggestion, a rule suspended meanwhile), its reservation did not become a decision: the
    transfers still undecided are evaluated again without it (at most ``MAX_PASSES`` passes; review of
    4A, point 4) - their suggestions no longer see an overpayment that does not exist.
    """
    report: RunReport | None = None
    refused: set = set()
    outcomes: dict = {}
    seen: set = set()
    unchanged: set = set()
    for _ in range(MAX_PASSES):
        evaluation = evaluate(conn, company.id, period=period, params=params, reserve=auto_decide,
                              no_reserve=refused, today=today)
        if report is None:
            report = RunReport(company.slug, period, dry_run, evaluation.params, evaluation=evaluation)
        else:
            report.evaluation = evaluation
            report.passes += 1
        seen |= {r.tx.id for r in evaluation.results}
        newly_refused = _one_pass(conn, company, evaluation, report, outcomes, unchanged, dry_run=dry_run,
                                  auto_decide=auto_decide)
        if dry_run or not newly_refused - refused:
            break
        refused |= newly_refused
    report.subjects = len(seen)
    report.suggestions_unchanged = len(unchanged)
    for outcome in outcomes.values():
        report.by_rank1[outcome.rank1] += 1
        if outcome.automatic:
            report.auto += 1
            continue
        report.queue += 1
        report.no_document += outcome.no_document
        report.ties += outcome.tie
    return report


def _one_pass(conn: psycopg.Connection, company: Any, evaluation: Evaluation, report: RunReport,
              outcomes: dict, unchanged: set, *, dry_run: bool, auto_decide: bool) -> set:
    """Save the suggestions of one evaluation and close what the automation may; returns the refused reservations."""
    rules = evaluation.ctx.builtin_rules
    refused = set()
    for result in evaluation.results:
        best = result.best
        if best is None:
            continue
        rank1 = f"{best.final_status}" + (f"/{best.subtype}" if best.subtype else "")
        ok, _why = auto.eligibility(best, evaluation.params, rules) if auto_decide else (False, "")
        outcome = _Outcome(rank1, False, best.final_status == "no_document", bool(best.tie))
        outcomes[result.tx.id] = outcome
        if dry_run:
            if ok:
                outcome.automatic = True
                report.by_rule[auto.licence(best, rules)["rule_key"]] += 1
            continue
        saved = save_result(conn, company.id, result, evaluation.params)
        report.suggestions_created += saved.created
        report.suggestions_expired += saved.expired
        if saved.unchanged:
            unchanged.add(result.tx.id)
        else:
            unchanged.discard(result.tx.id)
        if ok and saved.suggestion_ids:
            decision = auto.close(conn, result.tx.id, saved.suggestion_ids[0], best, rules)
            report.decisions.append(decision)
            if decision.decision_id:
                outcome.automatic = True
                report.by_rule[decision.rule_key] += 1
                continue
            report.auto_errors.append(f"{result.tx.label}: {decision.error}")
            if result.tx.id in evaluation.reserved:
                refused.add(result.tx.id)
    return refused
