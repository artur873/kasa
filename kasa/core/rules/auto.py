"""Category decisions of learned rules in stage ``auto`` (the other kinds are applied by 4A and 4B).

For every booked transaction and current document without a category in force: the hits of
``applicable_rules`` with ``action.kind = 'category'``, effective stage ``auto`` and no conflict
write ``l3_category_decision`` with ``method = 'rule'`` signed with the rule and its version (the
database refuses it when the rule is not in stage auto - KS004). A subject sent to a person because
of a sensitive word is never categorized by a rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.rules.apply import applicable_rules
from kasa.core.rules.facts import subject_facts


@dataclass
class AutoCategoryReport:
    applied: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)
    decisions: list[UUID] = field(default_factory=list)


def _has_auto_category_rules(conn: psycopg.Connection, company_id: UUID) -> bool:
    return bool(conn.execute(
        """
        SELECT 1 FROM rule_stage_effective
         WHERE company_id = %s AND effective_stage = 'auto' AND action ->> 'kind' = 'category'
           AND rule_key NOT LIKE 'builtin:%%' LIMIT 1
        """,
        (company_id,),
    ).fetchone())


def apply_category_rules(conn: psycopg.Connection, company: Any, *, dry_run: bool = False) -> AutoCategoryReport:
    from kasa.core.decisions import DecisionError, decide_category

    company_id = company.id if hasattr(company, "id") else company
    report = AutoCategoryReport()
    if not _has_auto_category_rules(conn, company_id):
        return report
    subjects = conn.execute(
        """
        SELECT 'transaction', t.id FROM l1_bank_transaction t
         WHERE t.company_id = %(c)s AND t.status = 'booked' AND t.superseded_by IS NULL
        UNION ALL
        SELECT 'document', d.id FROM l1_document d WHERE d.company_id = %(c)s AND d.superseded_by IS NULL
        """,
        {"c": company_id},
    ).fetchall()
    decided = {(r[0], r[1]) for r in conn.execute(
        "SELECT subject_type::text, subject_id FROM l3_category_decision WHERE company_id = %s AND status = 'active'",
        (company_id,)).fetchall()}
    sensitive = {(r[0], r[1]) for r in conn.execute(
        """
        SELECT subject_type::text, subject_id FROM l2_suggestion
         WHERE company_id = %s AND kind = 'category' AND proposal @> '{"needs_human": true}'::jsonb
           AND status IN ('open', 'accepted')
        """,
        (company_id,)).fetchall()}
    for subject_type, subject_id in subjects:
        if (subject_type, subject_id) in decided or (subject_type, subject_id) in sensitive:
            continue
        facts = subject_facts(conn, company_id, subject_type, subject_id)
        hits = [h for h in applicable_rules(conn, company_id, subject_type, facts, include_builtin=False)
                if (h.action or {}).get("kind") == "category"]
        if not hits:
            continue
        if any(h.conflict for h in hits) or len({h.action.get("category") for h in hits}) > 1:
            report.conflicts += 1
            continue
        hit = hits[0]
        if hit.stage != "auto" or dry_run:
            continue
        try:
            with conn.transaction():
                decision = decide_category(conn, company_id, subject_type, subject_id, hit.action["category"],
                                           decided_by=f"rule:{hit.rule_key}@{hit.version}", method="rule",
                                           rule_id=hit.rule_id, rule_version=hit.version, source="kasa.rules.auto",
                                           reason=f"reguła {hit.rule_key}: {hit.evidence_count} zgodnych decyzji")
        except (DecisionError, psycopg.Error) as exc:
            report.errors.append(f"{subject_type}:{subject_id}: {str(exc).splitlines()[0]}")
            continue
        report.applied += 1
        report.decisions.append(decision)
    return report
