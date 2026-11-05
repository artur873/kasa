"""Suggestions of matching in ``l2_suggestion`` (kind ``match``), in the format of the phase 4 contract.

At most 3 open suggestions per transaction (rank 1-3). A run that proposes exactly what is
already open (same proposals, ranks and scores) writes nothing (idempotency); a run that
proposes something else expires the old open suggestions of that transaction and writes the
new ones. A proposal rejected by the owner is never proposed again (``data.load_context``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.matching import weights as W
from kasa.core.matching.engine import ENGINE_VERSION, SOURCE, Candidate, TxResult, proposal_key

CREATED_BY = "kasa.matching"


@dataclass
class SaveOutcome:
    transaction_id: UUID
    suggestion_ids: list[UUID] = field(default_factory=list)   # by rank
    created: int = 0
    expired: int = 0
    unchanged: bool = False


def evidence_for(cand: Candidate, result: TxResult, params: dict[str, float]) -> dict[str, Any]:
    """Contract keys (facts, badge, precedents, hard, margin, tags) + ``detail`` for explain and decide."""
    return {
        "facts": cand.facts,
        "badge": W.badge(cand.score, cand.hard),
        "precedents": cand.precedents,
        "hard": cand.hard,
        "margin": round(cand.margin, 4),
        "tags": cand.tags,
        "detail": {
            "engine_version": ENGINE_VERSION,
            "kind": cand.kind,
            "raw_score": round(cand.raw, 4),
            "rule_key": cand.rule_key,
            "rule": {k: (str(v) if isinstance(v, UUID) else v) for k, v in cand.rule.items()} if cand.rule else None,
            "tie": cand.tie,
            "blockers": cand.blockers,
            "signals": [{"tag": s.tag, "weight": s.weight, "fact": s.fact_pl} for s in cand.signals],
            "items": [i.detail() for i in cand.items],
            "linked_evidence": cand.linked_evidence,
            "params": params,
            "occurrence": {
                "amount_minor": result.amount_minor,
                "legs": [{"transaction_id": str(t.id), "entry_reference": t.entry_reference,
                          "role": t.mpp_role or "single", "amount_minor": t.amount_minor} for t in result.group],
            },
        },
    }


def _signature(rows: list[tuple[int, dict[str, Any], float]]) -> list[tuple[int, str, float]]:
    return [(rank, proposal_key(proposal), round(float(score), 4)) for rank, proposal, score in rows]


def open_suggestions(conn: psycopg.Connection, company_id: UUID, transaction_id: UUID) -> list[tuple]:
    return conn.execute(
        """
        SELECT id, rank, proposal, score FROM l2_suggestion
         WHERE company_id = %s AND kind = 'match' AND subject_type = 'transaction' AND subject_id = %s
           AND status = 'open' AND source = %s
         ORDER BY rank, created_at
        """,
        (company_id, transaction_id, SOURCE),
    ).fetchall()


def save_result(conn: psycopg.Connection, company_id: UUID, result: TxResult,
                params: dict[str, float]) -> SaveOutcome:
    outcome = SaveOutcome(result.tx.id)
    existing = open_suggestions(conn, company_id, result.tx.id)
    new = [(rank, cand.proposal(), round(cand.score, 6)) for rank, cand in enumerate(result.candidates, start=1)]
    if existing and _signature([(r[1], r[2], r[3]) for r in existing]) == _signature(new):
        outcome.unchanged = True
        outcome.suggestion_ids = [r[0] for r in existing]
        return outcome
    if existing:
        conn.execute(
            "UPDATE l2_suggestion SET status = 'expired', resolved_at = now() WHERE id = ANY(%s) AND status = 'open'",
            ([r[0] for r in existing],),
        )
        outcome.expired = len(existing)
    for rank, cand in enumerate(result.candidates, start=1):
        method = "rule" if cand.rule else "code"
        suggestion_id = conn.execute(
            """
            INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, status,
                                       source, method, confidence, evidence, schema_version, created_by)
            VALUES (%s, 'match', 'transaction', %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (company_id, result.tx.id, Jsonb(cand.proposal()), round(cand.score, 6), rank, SOURCE, method,
             round(min(cand.score, 1.0), 4), Jsonb(evidence_for(cand, result, params)), ENGINE_VERSION, CREATED_BY),
        ).fetchone()[0]
        outcome.suggestion_ids.append(suggestion_id)
        outcome.created += 1
    return outcome
