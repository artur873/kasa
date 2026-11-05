"""Allocation run: subjects -> signals -> at most three suggestions per subject -> automation.

``run_allocation(conn, company)`` is idempotent: the same data gives the same open suggestions
(a changed proposal expires the old ones); subjects with an allocation in force are left alone.
The automation (``auto.py``) runs at the end, only for signal A or a mature rule.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.allocation.amounts import load_documents, load_internal_records
from kasa.core.allocation.objects import load_profiles
from kasa.core.allocation.signals import (
    AllocationContext,
    Proposal,
    badge,
    build_context,
    precedent_count,
    proposals_for,
    share_rows,
)
from kasa.core.allocation.subjects import (
    Subject,
    evidence_of_documents,
    is_direct_transaction,
    load_matches,
    transaction_states,
)
from kasa.core.cost_objects_discovery.records import load_records
from kasa.core.extension import Extension, get_extension

SOURCE = "allocation"
AUTO_MARGIN = 0.2
TIE = 0.02


@dataclass
class AllocationRunReport:
    subjects: int = 0
    allocated: int = 0
    suggested: int = 0
    unchanged: int = 0
    evidence_links: int = 0
    by_strength: Counter = field(default_factory=Counter)
    auto: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def summary(self) -> str:
        strengths = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_strength.items())) or "-"
        auto = self.auto.get("applied", 0)
        return (f"Podmioty: {self.subjects} (z alokacją: {self.allocated}); sugestie nowe/zmienione: "
                f"{self.suggested}, bez zmian: {self.unchanged}; rank 1 wg siły sygnału: {strengths}; "
                f"wiersze spięte z dokumentem (jeden koszt): {self.evidence_links}; automat: {auto}"
                + (" (próba - nic nie zapisano)" if self.dry_run else ""))


def build_subjects(conn: psycopg.Connection, company_id: UUID, documents, internal, matches, covered,
                   states) -> list[Subject]:
    lines = {ln.id: (doc, ln) for doc in documents.values() for ln in doc.lines}
    subjects: list[Subject] = []
    for r in load_records(conn, company_id, all_transactions=True):
        common = dict(text=r.text, tokens=r.text_tokens | r.name_tokens, window=r.window, window_source=r.date_source,
                      text_windows=[r.window] if (r.date_source == "text" and r.window and not r.month_only) else [],
                      city_key=r.city_key, city_from_text=r.city_source == "text", client_tokens=r.client_tokens,
                      month_only=r.month_only, persons=r.persons)
        if r.record_type == "document_line":
            doc, line = lines[r.record_id]
            subjects.append(Subject(subject_type="line", subject_id=r.record_id, direction=doc.direction,
                                    counterparty_id=doc.counterparty_id, counterparty_kind=doc.counterparty_kind,
                                    amount_minor=line.memorial_minor, document=doc, line=line,
                                    facts={**r.facts, "document_kind": doc.kind}, **common))
        elif r.record_type == "document":
            doc = documents[r.record_id]
            subjects.append(Subject(subject_type="document", subject_id=r.record_id, direction=doc.direction,
                                    counterparty_id=doc.counterparty_id, counterparty_kind=doc.counterparty_kind,
                                    amount_minor=doc.memorial_minor, document=doc, facts=dict(r.facts), **common))
        elif r.record_type == "internal_record":
            if r.record_id in covered:
                continue
            record = internal.get(r.record_id)
            subjects.append(Subject(subject_type="internal_record", subject_id=r.record_id, direction="internal",
                                    amount_minor=int(record.amount_minor or 0) if record else 0, record=record,
                                    facts=dict(r.facts), **common))
        elif r.record_type == "transaction":
            tx = {"id": r.record_id, "op_type": r.facts.get("op_type"),
                  "counterparty_kind": r.facts.get("counterparty_kind"), "intent": r.facts.get("intent"),
                  "invoice_reference": r.facts.get("invoice_reference")}
            if not is_direct_transaction(tx, matches, states.get(r.record_id, {})):
                continue
            facts = {**r.facts, "final_status": states.get(r.record_id, {}).get("status")}
            subjects.append(Subject(subject_type="transaction", subject_id=r.record_id, direction=r.direction,
                                    counterparty_id=r.facts.get("counterparty_id"),
                                    counterparty_kind=r.facts.get("counterparty_kind"),
                                    amount_minor=int(r.amount_minor or 0), facts=facts, **common))
    return subjects


def _rows(subject: Subject, proposals: list[Proposal], ctx: AllocationContext) -> list[dict[str, Any]]:
    rows = []
    for rank, proposal in enumerate(proposals, start=1):
        nxt = proposals[rank].score if rank < len(proposals) else 0.0
        margin = round(proposal.score - nxt, 4)
        tie = rank == 1 and len(proposals) > 1 and abs(proposal.score - nxt) <= TIE
        # a transfer is allocated directly by the automation only once matching said it has no document
        awaiting = subject.subject_type == "transaction" and not subject.facts.get("final_status")
        auto = rank == 1 and proposal.auto and not proposal.contradiction and not proposal.partial and \
            margin >= AUTO_MARGIN and not tie and not awaiting
        tags = list(proposal.tags) + (["tie"] if tie else []) + (["awaiting_matching"] if awaiting else [])
        if proposal.rule:
            tags.append(f"rule:{proposal.rule[2]}@{proposal.rule[1]}")
        rows.append({
            "rank": rank,
            "score": round(proposal.score, 4),
            "proposal": {"shares": share_rows(proposal, subject.amount_minor, ctx),
                         "signal_strength": proposal.strength, "cascade_step": proposal.cascade_step},
            "evidence": {
                "facts": proposal.facts, "badge": badge(proposal),
                "precedents": precedent_count(subject, proposal, ctx),
                "hard": proposal.hard, "margin": margin, "tags": list(dict.fromkeys(tags)),
                "partial": proposal.partial, "auto_eligible": auto, "contradiction": proposal.contradiction,
                "signal_strength": proposal.strength,
                "rule": ({"rule_id": str(proposal.rule[0]), "version": proposal.rule[1], "rule_key": proposal.rule[2]}
                         if proposal.rule else None),
            },
        })
    return rows


def write_suggestions(conn: psycopg.Connection, company_id: UUID, subject: Subject, rows: list[dict[str, Any]],
                      created_by: str) -> bool:
    """Replace the open allocation suggestions of a subject when they differ. True when written."""
    current = conn.execute(
        """
        SELECT id, rank, proposal, evidence FROM l2_suggestion
         WHERE company_id = %s AND kind = 'allocation' AND subject_type = %s AND subject_id = %s AND status = 'open'
         ORDER BY rank
        """,
        (company_id, subject.subject_type, subject.subject_id),
    ).fetchall()
    same = len(current) == len(rows) and all(
        c[1] == r["rank"] and c[2] == r["proposal"] and c[3] == r["evidence"]
        for c, r in zip(current, rows, strict=False))
    if same:
        return False
    if current:
        conn.execute("UPDATE l2_suggestion SET status = 'expired', resolved_at = now() WHERE id = ANY(%s)",
                     ([c[0] for c in current],))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, source,
                                       method, confidence, evidence, created_by)
            VALUES (%s, 'allocation', %s, %s, %s, %s, %s, %s, 'code', %s, %s, %s)
            """,
            [(company_id, subject.subject_type, subject.subject_id, Jsonb(r["proposal"]), r["score"], r["rank"],
              SOURCE, r["score"], Jsonb(r["evidence"]), created_by) for r in rows],
        )
    return True


def prepare(conn: psycopg.Connection, company: Any, extension: Extension | None = None):
    extension = extension or get_extension(company.extension_slug)
    documents = load_documents(conn, company.id)
    internal = load_internal_records(conn, company.id)
    profiles = load_profiles(conn, company.id)
    matches = load_matches(conn, company.id)
    covered = evidence_of_documents(internal, documents, matches)
    states = transaction_states(conn, company.id)
    ctx = build_context(conn, company.id, extension, profiles, documents, internal)
    subjects = build_subjects(conn, company.id, documents, internal, matches, covered, states)
    return ctx, subjects, covered


def suggest(conn: psycopg.Connection, company: Any, *, created_by: str = "kasa",
            report: AllocationRunReport | None = None) -> AllocationRunReport:
    report = report or AllocationRunReport()
    ctx, subjects, covered = prepare(conn, company)
    report.evidence_links = len(covered)
    for subject in subjects:
        report.subjects += 1
        if ctx.allocations.get(subject.key):
            report.allocated += 1
            continue
        proposals = proposals_for(subject, ctx)
        if not proposals:
            continue
        report.by_strength[proposals[0].strength or "UNASSIGNED"] += 1
        if write_suggestions(conn, company.id, subject, _rows(subject, proposals, ctx), created_by):
            report.suggested += 1
        else:
            report.unchanged += 1
    return report


def run_allocation(conn: psycopg.Connection, company: Any, *, dry_run: bool = False, auto: bool = True,
                   created_by: str = "kasa") -> AllocationRunReport:
    """Suggestions for every subject without an allocation, then the automation (signal A / mature rule)."""
    from kasa.core.allocation.auto import run_auto

    report = AllocationRunReport(dry_run=dry_run)
    if dry_run:
        with conn.transaction(force_rollback=True):
            suggest(conn, company, created_by=created_by, report=report)
            if auto:
                report.auto = run_auto(conn, company).as_dict()
        return report
    suggest(conn, company, created_by=created_by, report=report)
    if auto:
        report.auto = run_auto(conn, company).as_dict()
    return report


def allocation_status(conn: psycopg.Connection, company_id: UUID) -> dict[str, Any]:
    """How far allocation is: decided (human / automation), open suggestions by badge and signal."""
    decided = dict(conn.execute(
        """
        SELECT method::text, count(DISTINCT (subject_type, subject_id)) FROM l3_allocation
         WHERE company_id = %s AND status = 'active' GROUP BY 1
        """,
        (company_id,),
    ).fetchall())
    open_rows = conn.execute(
        """
        SELECT subject_type::text, evidence ->> 'badge', proposal ->> 'signal_strength', count(*),
               count(*) FILTER (WHERE (evidence ->> 'partial')::boolean)
          FROM l2_suggestion
         WHERE company_id = %s AND kind = 'allocation' AND status = 'open' AND rank = 1
         GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """,
        (company_id,),
    ).fetchall()
    return {
        "decided": {"human": decided.get("human", 0), "rule": decided.get("rule", 0)},
        "open": [{"subject_type": r[0], "badge": r[1], "signal_strength": r[2], "count": r[3], "partial": r[4]}
                 for r in open_rows],
    }
