"""Writing and finding readings in ``l1_semantics`` (migration 0008).

One reading per (subject, purpose, prompt version, model): a second pass finds it
and makes no call. A new prompt version or another model writes a new row and
marks the previous current reading of the subject ``superseded_by`` (nothing is
deleted). ``model = 'code'`` marks readings made without a call (empty title,
title made only of codes).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 1


def already_read(conn: psycopg.Connection, subject_type: str, subject_id: UUID, purpose: str,
                 prompt_version: str, model: str) -> bool:
    """A reading exists for this prompt version by this model (or by code, which needs no model)."""
    return conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM l1_semantics
                        WHERE subject_type = %s AND subject_id = %s AND purpose = %s AND prompt_version = %s
                          AND model IN (%s, 'code'))
        """,
        (subject_type, subject_id, purpose, prompt_version, model),
    ).fetchone()[0]


def read_subjects(conn: psycopg.Connection, company_id: UUID, subject_type: str, purpose: str,
                  prompt_version: str, model: str) -> set[UUID]:
    """Subjects of a company read already (by this model or by code) - for a whole pass at once."""
    rows = conn.execute(
        """
        SELECT DISTINCT subject_id FROM l1_semantics
         WHERE company_id = %s AND subject_type = %s AND purpose = %s AND prompt_version = %s
           AND model IN (%s, 'code')
        """,
        (company_id, subject_type, purpose, prompt_version, model),
    ).fetchall()
    return {r[0] for r in rows}


def flagged_subjects(conn: psycopg.Connection, company_id: UUID, subject_type: str) -> set[UUID]:
    """Subjects sent to a person because of a sensitive word (they never go to a model)."""
    rows = conn.execute(
        """
        SELECT DISTINCT subject_id FROM l2_suggestion
         WHERE company_id = %s AND kind = 'category' AND subject_type = %s
           AND proposal @> '{"needs_human": true, "reason": "sensitive"}'::jsonb
        """,
        (company_id, subject_type),
    ).fetchall()
    return {r[0] for r in rows}


def store_reading(conn: psycopg.Connection, company_id: UUID, *, subject_type: str, subject_id: UUID,
                  purpose: str, prompt_version: str, model: str, method: str, intent: str | None,
                  entities: dict[str, Any], summary: str | None, input_pseudonymized: str,
                  confidence: float | None = None, llm_call_id: UUID | None = None,
                  evidence: dict[str, Any] | None = None, source: str = "semantics",
                  created_by: str = "kasa.semantics") -> UUID:
    """Insert a reading; the previous current reading of the subject (same purpose) gets superseded_by."""
    previous = conn.execute(
        """
        SELECT id FROM l1_semantics
         WHERE subject_type = %s AND subject_id = %s AND purpose = %s AND superseded_by IS NULL
        """,
        (subject_type, subject_id, purpose),
    ).fetchall()
    new_id = conn.execute(
        """
        INSERT INTO l1_semantics (company_id, subject_type, subject_id, purpose, intent, entities, summary, model,
                                  input_pseudonymized, llm_call_id, source, method, confidence, evidence,
                                  schema_version, prompt_version, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (company_id, subject_type, subject_id, purpose, intent, Jsonb(entities), summary, model,
         input_pseudonymized, llm_call_id, source, method,
         Decimal(str(confidence)).quantize(Decimal("0.0001")) if confidence is not None else None,
         Jsonb(evidence or {}), SCHEMA_VERSION, prompt_version, created_by),
    ).fetchone()[0]
    for (old_id,) in previous:
        conn.execute("UPDATE l1_semantics SET superseded_by = %s WHERE id = %s", (new_id, old_id))
    return new_id


def untrusted(value: Any) -> dict[str, str] | None:
    """Text that came from a title, a line or a row: data, never a command (briefing, step 8)."""
    if value is None:
        return None
    text = str(value).strip()
    return {"untrusted_text": text[:500]} if text else None
