"""Cost objects (l3_cost_object). The kind and attributes come from the extension."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.decisions import log_decision
from kasa.core.extension import Extension, ExtensionError

UNASSIGNED = "UNASSIGNED"


def create_candidate(
    conn: psycopg.Connection,
    extension: Extension,
    *,
    company_id: UUID,
    attrs: Mapping[str, Any],
    source: str,
    created_by: str,
    method: Literal["code", "model"] = "code",
    name: str | None = None,
    confidence: float | None = None,
    evidence: dict[str, Any] | None = None,
    prompt_version: str | None = None,
) -> UUID:
    """Insert a cost object candidate validated against the extension schema."""
    _check_company_extension(conn, company_id, extension)
    clean = extension.validate_cost_object_attrs(attrs)
    row = conn.execute(
        """
        INSERT INTO l3_cost_object (
            company_id, kind, name, attrs, status,
            source, method, confidence, evidence, prompt_version, created_by
        ) VALUES (%s, %s, %s, %s, 'candidate', %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            company_id,
            extension.cost_object_kind,
            name or extension.cost_object_name(clean),
            Jsonb(clean),
            source,
            method,
            confidence,
            Jsonb(evidence or {}),
            prompt_version,
            created_by,
        ),
    ).fetchone()
    return row[0]


def approve(
    conn: psycopg.Connection,
    cost_object_id: UUID,
    *,
    decided_by: str,
    source: str = "queue",
    reason: str | None = None,
    suggestion_id: UUID | None = None,
) -> UUID:
    """A human approves a candidate. Returns the decision id."""
    row = conn.execute(
        "SELECT company_id, status FROM l3_cost_object WHERE id = %s FOR UPDATE", (cost_object_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma obiektu kosztowego {cost_object_id}")
    company_id, status = row
    if status != "candidate":
        raise ValueError(f"Obiekt kosztowy {cost_object_id} ma status {status}, zatwierdzić można tylko kandydata")
    decision_id = log_decision(
        conn,
        company_id=company_id,
        kind="cost_object",
        subject_type="cost_object",
        subject_id=cost_object_id,
        decided_by=decided_by,
        source=source,
        payload={"action": "approve"},
        suggestion_id=suggestion_id,
        reason=reason,
    )
    conn.execute(
        """
        UPDATE l3_cost_object
           SET status = 'approved', method = 'human', decision_id = %s, updated_by = %s
         WHERE id = %s
        """,
        (decision_id, decided_by, cost_object_id),
    )
    return decision_id


def unassigned_id(conn: psycopg.Connection, company_id: UUID) -> UUID:
    row = conn.execute(
        "SELECT id FROM l3_cost_object WHERE company_id = %s AND system_code = %s", (company_id, UNASSIGNED)
    ).fetchone()
    if row is None:
        raise LookupError(f"Firma {company_id} nie ma obiektu {UNASSIGNED}")
    return row[0]


def _check_company_extension(conn: psycopg.Connection, company_id: UUID, extension: Extension) -> None:
    row = conn.execute("SELECT extension_slug FROM company WHERE id = %s", (company_id,)).fetchone()
    if row is None:
        raise LookupError(f"Nie ma firmy {company_id}")
    if row[0] != extension.slug:
        raise ExtensionError(f"Firma {company_id} używa rozszerzenia {row[0]!r}, nie {extension.slug!r}")
