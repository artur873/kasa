"""``rules simulate``: what a rule would have done on the history (nothing is written).

For every subject the rule's pattern and scope hit (booked transactions, current documents and
lines), the action is compared with the decision in force of the same kind (a person's or the
automation's): agreed / disagreed / no decision yet. The report is the evidence for "accept",
"correct" or "enable auto" - and shows the exceptions by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.rules.apply import in_scope, pattern_holds
from kasa.core.rules.facts import canonical_counterparty, subject_facts
from kasa.core.rules.lifecycle import rule_row
from kasa.core.rules.observe import compare

KIND_OF_ACTION = {"final_status": "match", "match_strategy": "match", "category": "category",
                  "allocation": "allocation"}


@dataclass
class Simulation:
    rule_key: str
    version: int
    hits: int = 0
    agreed: int = 0
    disagreed: int = 0
    undecided: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    exceptions: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Symulacja {self.rule_key} v{self.version} na historii: trafień {self.hits}, zgodnych {self.agreed}, "
                 f"niezgodnych {self.disagreed}, bez decyzji {self.undecided}"]
        if self.by_method:
            lines.append("  decyzje: " + ", ".join(f"{k} {v}" for k, v in sorted(self.by_method.items())))
        for e in self.exceptions[:20]:
            lines.append(f"  wyjątek: {e['subject_type']} {e['label'][:60]} - reguła {e['predicted']}, "
                         f"decyzja {e['actual']}")
        return "\n".join(lines)


def _decision_of(conn: psycopg.Connection, kind: str, subject_type: str, subject_id: UUID) -> tuple | None:
    if kind == "match":
        return conn.execute(
            """
            SELECT d.payload, d.method::text FROM l3_transaction_status s JOIN l3_decision_log d ON d.id = s.decision_id
             WHERE s.transaction_id = %s AND s.status = 'active' LIMIT 1
            """,
            (subject_id,),
        ).fetchone()
    table = {"category": "l3_category_decision", "allocation": "l3_allocation"}[kind]
    return conn.execute(
        f"""
        SELECT d.payload, d.method::text FROM {table} x JOIN l3_decision_log d ON d.id = x.decision_id
         WHERE x.subject_type = %s AND x.subject_id = %s AND x.status = 'active' LIMIT 1
        """,
        (subject_type, subject_id),
    ).fetchone()


def simulate(conn: psycopg.Connection, rule_id: UUID) -> Simulation:
    rule = rule_row(conn, rule_id)
    company_id = rule["company_id"]
    action = rule["action"] or {}
    kind = KIND_OF_ACTION.get(action.get("kind"))
    report = Simulation(rule["rule_key"], rule["version"])
    subjects = conn.execute(
        """
        SELECT 'transaction', id FROM l1_bank_transaction
         WHERE company_id = %(c)s AND status = 'booked' AND superseded_by IS NULL
           AND (mpp_role IS NULL OR mpp_role <> 'vat_leg')
        UNION ALL SELECT 'document', id FROM l1_document WHERE company_id = %(c)s AND superseded_by IS NULL
        UNION ALL SELECT 'line', l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id
         WHERE d.company_id = %(c)s AND d.superseded_by IS NULL
        """,
        {"c": company_id},
    ).fetchall()
    cache: dict[Any, str | None] = {}
    for subject_type, subject_id in subjects:
        if kind == "match" and subject_type != "transaction":
            continue
        facts = subject_facts(conn, company_id, subject_type, subject_id)
        party = facts.get("counterparty_id")
        if party not in cache:
            found = canonical_counterparty(conn, party) if party else None
            cache[party] = str(found) if found else None
        canonical = cache[party]
        if not in_scope(rule["scope"] or {}, subject_type, facts, canonical):
            continue
        if not pattern_holds(rule["pattern"] or {}, facts, canonical):
            continue
        report.hits += 1
        decision = _decision_of(conn, kind, subject_type, subject_id) if kind else None
        if decision is None:
            report.undecided += 1
            continue
        payload, method = decision
        report.by_method[method] = report.by_method.get(method, 0) + 1
        compared = compare(conn, action, kind, payload or {})
        if compared is None:
            report.undecided += 1
            continue
        predicted, actual, agreed = compared
        if agreed:
            report.agreed += 1
        else:
            report.disagreed += 1
            report.exceptions.append({"subject_type": subject_type, "subject_id": str(subject_id),
                                      "label": facts.get("label") or "", "predicted": predicted, "actual": actual})
    return report
