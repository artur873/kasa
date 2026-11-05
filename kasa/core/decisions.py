"""Decision log (l3_decision_log): every decision, human or auto rule, append-only."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

DecisionKind = Literal["match", "allocation", "category", "transaction_status", "cost_object", "rule", "revoke"]
SubjectType = Literal["transaction", "document", "line", "internal_record", "cost_object", "rule"]


def log_decision(
    conn: psycopg.Connection,
    *,
    company_id: UUID,
    kind: DecisionKind,
    subject_type: SubjectType,
    subject_id: UUID,
    decided_by: str,
    source: str,
    method: Literal["human", "rule"] = "human",
    payload: dict[str, Any] | None = None,
    rule_id: UUID | None = None,
    rule_version: int | None = None,
    reverts_decision_id: UUID | None = None,
    suggestion_id: UUID | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    evidence: dict[str, Any] | list[Any] | None = None,
    created_by: str | None = None,
) -> UUID:
    """Append one decision and return its id. Guards in the database do the policing."""
    row = conn.execute(
        """
        INSERT INTO l3_decision_log (
            company_id, kind, subject_type, subject_id, payload, rule_id, rule_version,
            decided_by, reverts_decision_id, suggestion_id, reason,
            source, method, confidence, evidence, created_by
        ) VALUES (
            %(company_id)s, %(kind)s, %(subject_type)s, %(subject_id)s, %(payload)s, %(rule_id)s, %(rule_version)s,
            %(decided_by)s, %(reverts)s, %(suggestion_id)s, %(reason)s,
            %(source)s, %(method)s, %(confidence)s, %(evidence)s, %(created_by)s
        )
        RETURNING id
        """,
        {
            "company_id": company_id,
            "kind": kind,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": Jsonb(payload or {}),
            "rule_id": rule_id,
            "rule_version": rule_version,
            "decided_by": decided_by,
            "reverts": reverts_decision_id,
            "suggestion_id": suggestion_id,
            "reason": reason,
            "source": source,
            "method": method,
            "confidence": confidence,
            "evidence": Jsonb(evidence if evidence is not None else {}),
            "created_by": created_by or decided_by,
        },
    ).fetchone()
    return row[0]
