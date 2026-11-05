"""Retention (briefing, step 9; decision D6).

Kasa is not a statutory archive (KSeF keeps invoices for 10 years, the accounting
office keeps the books). What kasa keeps and for how long:

* source files (L0): until the end of the contract - then their content is purged
  by ``kasa_purge_raw`` (the row, the SHA-256 digest and the metadata stay, so the
  facts remain traceable and the same file is never stored again);
* records: contract + 1 year, then persons are anonymized (``anonymize_persons``: the raw
  columns, every derived text, the person counterparties) and with ``--shred`` their pseudonym
  keys are shredded (``value_encrypted -> NULL``: pseudonyms stay, nobody can reverse them) and
  the source files with persons still kept are purged;
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


# ---------------------------------------------------------------------------
# Phase 5 (migration 0012): the model input of l1_semantics, anonymization of persons, one run
# (reviews: phase 1 point 3, phase 3 points 4 and 10). Nothing is deleted: texts are emptied or
# overwritten by their pseudonymous form only by the database functions, which log what they did.
# ---------------------------------------------------------------------------

DEFAULT_PERSON_KEYS = ("osoba", "person", "imię i nazwisko", "imie i nazwisko", "nazwisko", "pracownik", "wykonawca")
DEFAULT_DROP_KEYS = ("uwagi", "notes", "telefon", "tel", "tel.", "phone", "iban", "rachunek", "konto", "nr konta",
                     "adres", "address", "pesel", "email", "e-mail")


def expire_semantics_inputs(conn: psycopg.Connection, *, now: datetime | None = None) -> int:
    """Empty the model inputs of ``l1_semantics`` past their 90 days (``kasa_expire_semantics_inputs``);
    the SHA-256 and the length stay in ``input_sha256`` / ``input_chars``."""
    if now is None:
        return conn.execute("SELECT kasa_expire_semantics_inputs()").fetchone()[0]
    return conn.execute("SELECT kasa_expire_semantics_inputs(%s)", (now,)).fetchone()[0]


def overdue_payloads(conn: psycopg.Connection, *, now: datetime | None = None) -> dict[str, int]:
    """Model texts kept past their retention (should be 0 after ``retention run``)."""
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM llm_call_payload WHERE expired_at IS NULL AND expires_at <= coalesce(%(n)s, now())),
               (SELECT count(*) FROM l1_semantics WHERE input_expired_at IS NULL
                   AND input_expires_at <= coalesce(%(n)s, now()))
        """,
        {"n": now},
    ).fetchone()
    return {"llm_call_payload": int(row[0]), "l1_semantics_input": int(row[1])}


def person_keys_for(extension_slug: str | None) -> list[str]:
    """Columns of internal files that name a person (the ``person`` column of every import mapping)."""
    keys = set(DEFAULT_PERSON_KEYS)
    if extension_slug:
        try:
            from kasa.core.extension import get_extension

            for mapping in get_extension(extension_slug).import_mappings.values():
                spec = (getattr(mapping, "columns", None) or {}).get("person")
                if spec is not None:
                    keys |= {spec.source.casefold(), *(a.casefold() for a in spec.aliases)}
        except Exception:  # noqa: BLE001 - the defaults cover the known files
            pass
    return sorted(keys)


def anonymize_persons(conn: psycopg.Connection, company_id: UUID, before: date, *, actor: str | None = None,
                      reason: str = "retencja: umowa + 1 rok (Krok 9)", shred_keys: bool = False) -> UUID | None:
    """Overwrite the raw personal columns of the records dated before ``before`` with their pseudonymous form
    (``kasa_anonymize_persons`` - the only UPDATE path the database allows for them, logged in
    ``anonymization_log``), together with every text derived from those records (the queue's rank reasons,
    facts of suggestions and decisions, rules, notes of control-set labels), every version of a person
    counterparty, what a channel saw of a person's document and the model payloads of the records
    (migration 0014; review of 4C/5, point 2).

    ``shred_keys`` (CLI ``--shred``): also destroy the person keys created before that day and purge the
    source files (L0) with persons whose records are all older than that day - the files are kept only until
    the end of the contract (D6), so after "contract + 1 year" nothing of them may stay (``kasa_purge_raw``,
    logged in ``l0_purge_log``)."""
    slug = conn.execute("SELECT extension_slug FROM company WHERE id = %s", (company_id,)).fetchone()
    log_id = conn.execute(
        "SELECT kasa_anonymize_persons(%s, %s, %s, %s, %s, %s, %s)",
        (company_id, before, actor, reason, person_keys_for(slug[0] if slug else None), list(DEFAULT_DROP_KEYS),
         shred_keys),
    ).fetchone()[0]
    if shred_keys:
        shred_pseudonyms(conn, company_id, created_before=datetime.combine(before, datetime.min.time()))
    return log_id


def anonymization_log(conn: psycopg.Connection, company_id: UUID) -> list[dict]:
    rows = conn.execute(
        "SELECT id, before_date, reason, counts, anonymized_by, anonymized_at FROM anonymization_log "
        "WHERE company_id = %s ORDER BY anonymized_at, id", (company_id,),
    ).fetchall()
    return [{"id": r[0], "before": r[1], "reason": r[2], "counts": r[3], "by": r[4], "at": r[5]} for r in rows]


def run_retention(conn: psycopg.Connection, *, now: datetime | None = None) -> dict[str, int]:
    """One retention run (``python -m kasa retention run``): model prompts/answers and model inputs past 90 days."""
    return {"llm_call_payload": expire_llm_payloads(conn, now=now),
            "l1_semantics_input": expire_semantics_inputs(conn, now=now)}
