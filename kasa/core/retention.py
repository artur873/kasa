"""Retention (briefing, step 9; decision D6).

Kasa is not a statutory archive (KSeF keeps invoices for 10 years, the accounting
office keeps the books). What kasa keeps and for how long:

* source files (L0): until the end of the contract - then their content is purged
  by ``kasa_purge_raw`` (the row, the SHA-256 digest and the metadata stay, so the
  facts remain traceable and the same file is never stored again);
* records: contract + 1 year, then persons are anonymized - their pseudonym keys
  are shredded (``value_encrypted -> NULL``): pseudonyms stay, nobody can reverse them;
* model prompts and answers: 90 days - ``llm_call_payload`` is emptied by
  ``kasa_expire_llm_payloads()`` (``expire_llm_payloads``); the call log keeps only
  digests and lengths; access log: 3 years (enforced where those logs live).

L0 stays append-only: the purge is the only change the database allows, it is
done by one SECURITY DEFINER function and every purge is logged in ``l0_purge_log``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

import psycopg


@dataclass(frozen=True)
class RetentionPolicy:
    raw_files: str = "do końca umowy"
    records_years_after_contract: int = 1
    model_prompts_days: int = 90
    access_log_years: int = 3
    bank_consent_days: int = 180
    bank_consent_alert_days: int = 14


POLICY = RetentionPolicy()


@dataclass(frozen=True)
class PurgeEntry:
    id: UUID
    raw_id: UUID
    sha256: str
    payload_bytes: int
    reason: str
    purged_by: str
    purged_at: datetime


def purge_raw(conn: psycopg.Connection, raw_id: UUID, reason: str, *, actor: str | None = None) -> UUID:
    """Purge the content of one raw record; returns the purge log id (idempotent)."""
    return conn.execute("SELECT kasa_purge_raw(%s, %s, %s)", (raw_id, reason, actor)).fetchone()[0]


def purge_company_raw(conn: psycopg.Connection, company_id: UUID, reason: str, *, fetched_before: date | None = None,
                      actor: str | None = None) -> list[UUID]:
    """Purge every not-yet-purged raw record of a company (end of contract), optionally only older ones."""
    rows = conn.execute(
        """
        SELECT id FROM l0_raw
         WHERE company_id = %s AND purged_at IS NULL AND (%s::date IS NULL OR fetched_at < %s::date)
         ORDER BY fetched_at, id
        """,
        (company_id, fetched_before, fetched_before),
    ).fetchall()
    return [purge_raw(conn, row[0], reason, actor=actor) for row in rows]


def purge_log(conn: psycopg.Connection, company_id: UUID) -> list[PurgeEntry]:
    rows = conn.execute(
        """
        SELECT id, raw_id, sha256, payload_bytes, reason, purged_by, purged_at
          FROM l0_purge_log WHERE company_id = %s ORDER BY purged_at, id
        """,
        (company_id,),
    ).fetchall()
    return [PurgeEntry(*row) for row in rows]


def shred_pseudonyms(conn: psycopg.Connection, company_id: UUID, *, kinds: tuple[str, ...] = ("person",),
                     created_before: datetime | None = None) -> int:
    """Anonymize: destroy the encrypted values behind pseudonyms (the codes stay). Returns keys shredded."""
    return conn.execute(
        """
        UPDATE pseudonym_key SET value_encrypted = NULL, shredded_at = now()
         WHERE company_id = %s AND kind = ANY(%s::pseudonym_kind[]) AND shredded_at IS NULL
           AND (%s::timestamptz IS NULL OR created_at < %s::timestamptz)
        """,
        (company_id, list(kinds), created_before, created_before),
    ).rowcount


def expire_llm_payloads(conn: psycopg.Connection, *, now: datetime | None = None) -> int:
    """Empty prompts and answers of model calls past their 90 days (``kasa_expire_llm_payloads``)."""
    if now is None:
        return conn.execute("SELECT kasa_expire_llm_payloads()").fetchone()[0]
    return conn.execute("SELECT kasa_expire_llm_payloads(%s)", (now,)).fetchone()[0]
