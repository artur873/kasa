"""Automation of allocation - only signal A or a mature rule (briefing, step 5; decision D3).

An open rank-1 allocation suggestion is applied by the automation when its evidence says
``auto_eligible``: an explicit identifier (a unique token of the name in the line, the title or
the mail) with nothing contradicting it and a clear margin over the second proposal, or a rule
of the counterparty in stage ``auto`` (4C, ``kasa.core.rules``). A row of an internal file is
hard evidence only when the extension says so (``allocation.internal_record_is_hard``, off by
default - decision D2). Every write is ``method = 'rule'`` signed with the rule and its version:
``builtin:allocation_explicit_identifier`` (seeded by migration 0010, stage auto, the user may
suspend it - then nothing is automatic) or the learned rule itself. A suggestion of the agent
(``source = 'agent'``) is never applied, whatever its evidence says (review of 4C/5, point 7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.allocation.decide import AllocationError, apply_allocation

BUILTIN_RULE = "builtin:allocation_explicit_identifier"


@dataclass
class AutoReport:
    applied: int = 0
    skipped_no_rule: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "skipped_no_rule": self.skipped_no_rule, "failed": self.failed}


def builtin_rule(conn: psycopg.Connection, company_id: UUID) -> tuple[UUID, int] | None:
    row = conn.execute(
        """
        SELECT id, version FROM l3_rule
         WHERE company_id = %s AND rule_key = %s AND stage = 'auto'
         ORDER BY version DESC LIMIT 1
        """,
        (company_id, BUILTIN_RULE),
    ).fetchone()
    return (row[0], row[1]) if row else None


def run_auto(conn: psycopg.Connection, company: Any) -> AutoReport:
    report = AutoReport()
    builtin = builtin_rule(conn, company.id)
    rows = conn.execute(
        """
        SELECT s.id, s.evidence FROM l2_suggestion s
         WHERE s.company_id = %s AND s.kind = 'allocation' AND s.status = 'open' AND s.rank = 1
           AND s.source <> 'agent'                -- a proposal of the agent is never applied by the automation
           AND (s.evidence ->> 'auto_eligible')::boolean
           AND NOT EXISTS (SELECT 1 FROM l3_allocation a WHERE a.subject_type = s.subject_type
                             AND a.subject_id = s.subject_id AND a.status = 'active')
         ORDER BY s.created_at, s.id
        """,
        (company.id,),
    ).fetchall()
    for suggestion_id, evidence in rows:
        rule = (evidence or {}).get("rule")
        if rule:
            rule_id, version, key = UUID(rule["rule_id"]), int(rule["version"]), rule["rule_key"]
        elif builtin:
            (rule_id, version), key = builtin, BUILTIN_RULE
        else:
            report.skipped_no_rule += 1
            continue
        try:
            with conn.transaction():
                decision = apply_allocation(conn, suggestion_id, decided_by=f"rule:{key}@{version}", method="rule",
                                            rule_id=rule_id, rule_version=version, source="allocation_auto")
        except (AllocationError, psycopg.Error) as exc:
            report.failed.append({"suggestion_id": str(suggestion_id), "error": str(exc).splitlines()[0]})
            continue
        report.applied += 1
        report.decisions.append(decision)
    return report
