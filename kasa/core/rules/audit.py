"""Audit of the automation (briefing, step 6, "Bezpieczniki"): 2% of its decisions a month, at least 20.

* ``sample(conn, company, month)`` draws (seeded, reproducible) from the automatic decisions in force
  (``method = 'rule'``, not revoked) whose subject belongs to the month (booking date of a transfer,
  sale/issue date of a document, date of an internal row); fewer than 20 = all of them. Drawn once per
  month (idempotent).
* The sample shows up in the queue as an ordinary card marked "audyt": the automatic decision with
  "Potwierdź" / "Błąd".
* ``verdict(..., 'error')`` revokes the automatic decision (the subject is back in the queue), which
  suspends the rule with a post-mortem draft (``kasa.core.decisions.revoke_decision``).
"""

from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SOURCE = "kasa.rules.audit"
RATE = 0.02
MINIMUM = 20

_SUBJECT_DAY = """
    CASE d.subject_type
        WHEN 'transaction' THEN (SELECT t.booking_date FROM l1_bank_transaction t WHERE t.id = d.subject_id)
        WHEN 'document' THEN (SELECT coalesce(x.sale_date, x.issue_date) FROM l1_document x WHERE x.id = d.subject_id)
        WHEN 'line' THEN (SELECT coalesce(x.sale_date, x.issue_date) FROM l1_document_line l
                            JOIN l1_document x ON x.id = l.document_id WHERE l.id = d.subject_id)
        WHEN 'internal_record' THEN (SELECT r.record_date FROM l1_internal_record r WHERE r.id = d.subject_id)
    END
"""
_SUBJECT_AMOUNT = """
    CASE d.subject_type
        WHEN 'transaction' THEN (SELECT abs(t.amount_minor) FROM l1_bank_transaction t WHERE t.id = d.subject_id)
        WHEN 'document' THEN (SELECT abs(coalesce(x.gross_pln_minor, x.gross_minor)) FROM l1_document x
                               WHERE x.id = d.subject_id)
        WHEN 'line' THEN (SELECT abs(coalesce(l.gross_minor, l.net_minor)) FROM l1_document_line l WHERE l.id = d.subject_id)
        WHEN 'internal_record' THEN (SELECT abs(r.amount_minor) FROM l1_internal_record r WHERE r.id = d.subject_id)
    END
"""


@dataclass
class AuditReport:
    month: str
    population: int
    sample_size: int
    drawn: list[UUID] = field(default_factory=list)
    existing: bool = False
    seed: int | None = None

    def summary(self) -> str:
        if self.existing:
            return f"Audyt {self.month}: próbka już wylosowana ({self.sample_size} z {self.population})."
        return (f"Audyt {self.month}: decyzji automatu {self.population}, wylosowano {self.sample_size} "
                f"(2%, min. {MINIMUM}; ziarno {self.seed}).")


def sample_size(population: int, rate: float = RATE, minimum: int = MINIMUM) -> int:
    if population <= minimum:
        return population
    return max(minimum, math.ceil(rate * population))


def population(conn: psycopg.Connection, company_id: UUID, month: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT * FROM (
                SELECT d.id AS decision_id, d.kind::text AS kind, d.subject_type::text AS subject_type, d.subject_id,
                       d.rule_id, d.rule_version, ({_SUBJECT_DAY}) AS day, ({_SUBJECT_AMOUNT}) AS amount_minor
                  FROM l3_decision_log d
                 WHERE d.company_id = %s AND d.method = 'rule' AND d.kind <> 'revoke'
                   AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
            ) p
             WHERE to_char(p.day, 'YYYY-MM') = %s
             ORDER BY p.decision_id
            """,
            (company_id, month),
        ).fetchall()


def sample(conn: psycopg.Connection, company: Any, month: str, *, rate: float = RATE, minimum: int = MINIMUM,
           seed: int | None = None, created_by: str = SOURCE) -> AuditReport:
    company_id = company.id if hasattr(company, "id") else company
    existing = conn.execute(
        "SELECT count(*), max(population) FROM l2_audit_sample WHERE company_id = %s AND month = %s",
        (company_id, month),
    ).fetchone()
    if existing[0]:
        return AuditReport(month, int(existing[1]), int(existing[0]), existing=True)
    rows = population(conn, company_id, month)
    if not rows:
        return AuditReport(month, 0, 0)
    seed = seed if seed is not None else secrets.randbelow(2**31)
    k = sample_size(len(rows), rate, minimum)
    drawn = random.Random(seed).sample(rows, k)
    report = AuditReport(month, len(rows), k, seed=seed)
    for row in drawn:
        sample_id = conn.execute(
            """
            INSERT INTO l2_audit_sample (company_id, decision_id, month, rule_id, rule_version, subject_type,
                                         subject_id, amount_minor, population, sample_size, seed, source, method,
                                         evidence, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'code', %s, %s) RETURNING id
            """,
            (company_id, row["decision_id"], month, row["rule_id"], row["rule_version"], row["subject_type"],
             row["subject_id"], row["amount_minor"], len(rows), k, seed, SOURCE,
             Jsonb({"kind": row["kind"], "rate": rate, "minimum": minimum}), created_by),
        ).fetchone()[0]
        report.drawn.append(sample_id)
    return report


def list_samples(conn: psycopg.Connection, company_id: UUID, *, month: str | None = None,
                 open_only: bool = False) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT s.id, s.month, s.decision_id, s.subject_type::text AS subject_type, s.subject_id, s.amount_minor,
                   s.verdict, s.reviewed_by, s.reviewed_at, s.review_decision_id, r.rule_key, d.kind::text AS kind,
                   d.decided_by, d.payload
              FROM l2_audit_sample s
              JOIN l3_decision_log d ON d.id = s.decision_id
              LEFT JOIN l3_rule r ON r.id = s.rule_id
             WHERE s.company_id = %s AND (%s::text IS NULL OR s.month = %s) AND (NOT %s OR s.verdict IS NULL)
             ORDER BY s.month, s.sampled_at, s.id
            """,
            (company_id, month, month, open_only),
        ).fetchall()


def verdict(conn: psycopg.Connection, sample_id: UUID, value: str, *, decided_by: str,
            note: str | None = None) -> dict[str, Any]:
    """'confirmed' or 'error' (the automatic decision is revoked and the rule suspended)."""
    from kasa.core.decisions import revoke_decision           # decisions -> rules -> decisions: late import

    if value not in ("confirmed", "error"):
        raise ValueError("werdykt audytu: confirmed albo error")
    row = conn.execute("SELECT decision_id, verdict FROM l2_audit_sample WHERE id = %s FOR UPDATE",
                       (sample_id,)).fetchone()
    if row is None:
        raise LookupError(f"Nie ma próbki audytu {sample_id}")
    if row[1] is not None:
        raise ValueError(f"próbka {sample_id} ma już werdykt {row[1]}")
    review = None
    if value == "error":
        review = revoke_decision(conn, row[0], decided_by, reason=f"audyt: błąd automatu{f' - {note}' if note else ''}")
    conn.execute(
        """
        UPDATE l2_audit_sample SET verdict = %s, reviewed_by = %s, reviewed_at = now(), review_decision_id = %s,
               note = %s
         WHERE id = %s
        """,
        (value, decided_by, review, note, sample_id),
    )
    return {"sample_id": sample_id, "verdict": value, "review_decision_id": review}
