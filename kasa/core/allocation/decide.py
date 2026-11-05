"""Allocation decisions (L3): a person's click or an automation acting on a rule in stage ``auto``.

``apply_allocation(conn, suggestion_id, *, decided_by, method, rule_id, rule_version, reason)``
turns one open ``kind = 'allocation'`` suggestion into ``l3_allocation`` rows (one per cost object,
with ``signal_strength`` and ``cascade_step``), marks the suggestion accepted and the other open
allocation suggestions of the subject rejected (``resolved_by_decision``), and appends a
``kind = 'allocation'`` row to ``l3_decision_log``. Shares are stored with 6 decimals summing to
exactly 1.000000 (truth check 3); amounts are split to the grosz.

* A person may replace a previous allocation (the old rows are revoked by the new decision);
  automation never replaces anything - the database also refuses a rule write over a human row
  (KS003) and a rule outside stage ``auto`` (KS004).
* A partial proposal (offers known for some objects only) cannot be accepted as it is: the person
  completes it (``shares``) - nothing is split "by eye".
* A target merged into another cost object is followed to the object in force; it must be approved.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.allocation.amounts import distribute, load_documents
from kasa.core.decisions import log_decision

SUBJECT_TYPES = ("line", "document", "internal_record", "transaction")


class AllocationError(ValueError):
    """The allocation cannot be written as asked."""


def subject_amount(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID) -> int:
    """Memorial amount (lines, documents, internal rows; PLN minor) or the signed amount of a transfer."""
    if subject_type == "transaction":
        row = conn.execute("SELECT amount_minor FROM l1_bank_transaction WHERE id = %s", (subject_id,)).fetchone()
        return int(row[0]) if row else 0
    if subject_type == "internal_record":
        row = conn.execute("SELECT amount_minor FROM l1_internal_record WHERE id = %s", (subject_id,)).fetchone()
        return int(row[0] or 0) if row else 0
    if subject_type == "line":
        row = conn.execute("SELECT document_id FROM l1_document_line WHERE id = %s", (subject_id,)).fetchone()
        if row is None:
            return 0
        doc = load_documents(conn, company_id, current_only=False, ids=[row[0]]).get(row[0])
        line = next((ln for ln in doc.lines if ln.id == subject_id), None) if doc else None
        return int(line.memorial_minor) if line else 0
    doc = load_documents(conn, company_id, current_only=False, ids=[subject_id]).get(subject_id)
    return int(doc.memorial_minor) if doc else 0


def _target(conn: psycopg.Connection, company_id: UUID, cost_object_id: UUID) -> tuple[UUID, str]:
    """The cost object in force (following merged_into); it must be approved."""
    current = cost_object_id
    for _ in range(20):
        row = conn.execute(
            "SELECT status::text, merged_into, name, company_id FROM l3_cost_object WHERE id = %s", (current,)
        ).fetchone()
        if row is None or row[3] != company_id:
            raise AllocationError(f"Nie ma obiektu kosztowego {cost_object_id} w tej firmie")
        status, merged_into, name, _ = row
        if status == "merged" and merged_into:
            current = merged_into
            continue
        if status != "approved":
            raise AllocationError(f"Obiekt kosztowy {name!r} ma status {status} - najpierw go zatwierdź")
        return current, name
    raise AllocationError(f"Pętla w łańcuchu połączeń obiektu {cost_object_id}")


def _normalize(shares: list[tuple[UUID, Fraction]]) -> list[tuple[UUID, Fraction, int]]:
    """Merge shares of the same object, check they cover 100%, return (object, exact, micro-units)."""
    merged: dict[UUID, Fraction] = {}
    for cost_object_id, share in shares:
        if share <= 0:
            raise AllocationError("Udział musi być dodatni")
        merged[cost_object_id] = merged.get(cost_object_id, Fraction(0)) + share
    total = sum(merged.values())
    if abs(total - 1) > Fraction(1, 1_000_000):
        raise AllocationError(f"Udziały sumują się do {float(total):.6f}, a muszą do 1,000000 "
                              "(propozycja częściowa wymaga uzupełnienia reszty)")
    ids = list(merged)
    exact = [merged[i] / total for i in ids]
    micro = distribute(1_000_000, exact)
    return [(i, e, m) for i, e, m in zip(ids, exact, micro, strict=True)]


def _as_shares(items: list[dict[str, Any]] | list[tuple]) -> list[tuple[UUID, Fraction]]:
    out = []
    for item in items:
        if isinstance(item, dict):
            exact = item.get("share_exact")
            share = Fraction(str(exact)) if exact else Fraction(str(item["share"]))
            out.append((UUID(str(item["cost_object_id"])), share))
        else:
            cost_object_id, share = item
            out.append((UUID(str(cost_object_id)), Fraction(str(share)) if not isinstance(share, Fraction) else share))
    return out


def _write(conn: psycopg.Connection, *, company_id: UUID, subject_type: str, subject_id: UUID,
           shares: list[tuple[UUID, Fraction]], decided_by: str, method: str, rule_id: UUID | None,
           rule_version: int | None, reason: str | None, suggestion_id: UUID | None, signal_strength: str | None,
           cascade_step: str | None, source: str, confidence: float | None, evidence: dict[str, Any]) -> UUID:
    if subject_type not in SUBJECT_TYPES:
        raise AllocationError(f"Nie da się alokować podmiotu rodzaju {subject_type}")
    if method not in ("human", "rule"):
        raise AllocationError("Alokację zapisuje człowiek (human) albo reguła w etapie auto (rule)")
    if method == "rule" and (rule_id is None or rule_version is None):
        raise AllocationError("Automat musi wskazać regułę i jej wersję")
    resolved = [(_target(conn, company_id, cost_object_id)[0], share) for cost_object_id, share in shares]
    normalized = _normalize(resolved)
    existing = conn.execute(
        """
        SELECT id, method::text FROM l3_allocation
         WHERE company_id = %s AND subject_type = %s AND subject_id = %s AND status = 'active' FOR UPDATE
        """,
        (company_id, subject_type, subject_id),
    ).fetchall()
    if existing and method != "human":
        raise AllocationError("Podmiot ma już alokację - automat niczego nie zastępuje")
    amount = subject_amount(conn, company_id, subject_type, subject_id)
    amounts = distribute(amount, [e for _, e, _ in normalized])
    payload = {
        "shares": [{"cost_object_id": str(i), "share": m / 1_000_000, "share_exact": str(e), "amount_minor": a}
                   for (i, e, m), a in zip(normalized, amounts, strict=True)],
        "signal_strength": signal_strength, "cascade_step": cascade_step,
        "replaces": [str(r[0]) for r in existing],
    }
    decision_id = log_decision(
        conn, company_id=company_id, kind="allocation", subject_type=subject_type, subject_id=subject_id,
        decided_by=decided_by, source=source, method=method, payload=payload, rule_id=rule_id,
        rule_version=rule_version, suggestion_id=suggestion_id, reason=reason,
        confidence=None if confidence is None else round(float(confidence), 4), evidence=evidence,
    )
    if existing:
        conn.execute(
            """
            UPDATE l3_allocation SET status = 'revoked', revoked_by_decision = %s, method = 'human', updated_by = %s
             WHERE id = ANY(%s)
            """,
            (decision_id, decided_by, [r[0] for r in existing]),
        )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO l3_allocation (company_id, subject_type, subject_id, cost_object_id, share, amount_minor,
                                       decision_id, rule_id, rule_version, signal_strength, cascade_step,
                                       source, method, confidence, evidence, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(company_id, subject_type, subject_id, cost_object_id, Decimal(micro) / Decimal(1_000_000), a,
              decision_id, rule_id, rule_version, signal_strength, cascade_step, source, method,
              None if confidence is None else round(float(confidence), 4), Jsonb(evidence), decided_by)
             for (cost_object_id, _, micro), a in zip(normalized, amounts, strict=True)],
        )
    conn.execute(
        """
        UPDATE l2_suggestion SET status = CASE WHEN id = %s THEN 'accepted'::suggestion_status
                                               ELSE 'rejected'::suggestion_status END,
               resolved_at = now(), resolved_by_decision = %s
         WHERE company_id = %s AND kind = 'allocation' AND subject_type = %s AND subject_id = %s AND status = 'open'
        """,
        (suggestion_id, decision_id, company_id, subject_type, subject_id),
    )
    return decision_id


def apply_allocation(conn: psycopg.Connection, suggestion_id: UUID, *, decided_by: str, method: str = "human",
                     rule_id: UUID | None = None, rule_version: int | None = None, reason: str | None = None,
                     shares: list[dict[str, Any]] | list[tuple] | None = None, source: str | None = None) -> UUID:
    """Accept an allocation suggestion (``shares`` = the person's own split, e.g. completing a partial one)."""
    row = conn.execute(
        """
        SELECT company_id, kind::text, subject_type::text, subject_id, proposal, status::text, score, evidence
          FROM l2_suggestion WHERE id = %s FOR UPDATE
        """,
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Nie ma sugestii {suggestion_id}")
    company_id, kind, subject_type, subject_id, proposal, status, score, evidence = row
    if kind != "allocation":
        raise AllocationError(f"Sugestia {suggestion_id} jest rodzaju {kind}, nie allocation")
    if status != "open":
        raise AllocationError(f"Sugestia {suggestion_id} ma status {status}")
    evidence = evidence or {}
    if shares is None and evidence.get("partial") and method == "human":
        raise AllocationError("Propozycja częściowa: podaj pełny podział (shares), reszta do uzupełnienia")
    if method == "rule" and evidence.get("partial"):
        raise AllocationError("Automat nie przyjmuje propozycji częściowej")
    chosen = _as_shares(shares if shares is not None else proposal.get("shares") or [])
    own_split = shares is not None
    return _write(
        conn, company_id=company_id, subject_type=subject_type, subject_id=subject_id, shares=chosen,
        decided_by=decided_by, method=method, rule_id=rule_id, rule_version=rule_version, reason=reason,
        suggestion_id=suggestion_id, signal_strength=None if own_split else proposal.get("signal_strength"),
        cascade_step=None if own_split else proposal.get("cascade_step"),
        source=source or ("allocation_auto" if method == "rule" else "queue"), confidence=score,
        evidence={"suggestion_id": str(suggestion_id), "facts": evidence.get("facts", []),
                  "tags": evidence.get("tags", []), "own_split": own_split},
    )


def allocate_manually(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID,
                      shares: list[dict[str, Any]] | list[tuple], *, decided_by: str, reason: str | None = None,
                      source: str = "queue") -> UUID:
    """A person's own allocation ("Inne..." on the card) - open suggestions of the subject are rejected."""
    return _write(
        conn, company_id=company_id, subject_type=subject_type, subject_id=subject_id, shares=_as_shares(shares),
        decided_by=decided_by, method="human", rule_id=None, rule_version=None, reason=reason, suggestion_id=None,
        signal_strength=None, cascade_step=None, source=source, confidence=None, evidence={"manual": True},
    )
