"""Shelf L0: store raw payloads exactly as fetched. INSERT only, deduplicated by SHA-256.

Every ingestion channel (bank, ksef, file, pdf, email) calls ``store_raw`` before
parsing anything. Repeating a fetch never creates a duplicate: the same bytes for
the same company return the existing row id with ``created=False``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

Channel = str  # 'bank' | 'ksef' | 'file' | 'pdf' | 'email' (enum raw_channel in the database)


@dataclass(frozen=True)
class RawRecord:
    id: UUID
    sha256: str
    created: bool


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def store_raw(
    conn: psycopg.Connection,
    company_id: UUID,
    channel: Channel,
    payload: bytes,
    *,
    content_type: str,
    external_ref: str | None = None,
    meta: dict[str, Any] | None = None,
) -> RawRecord:
    """Insert a raw payload into l0_raw, or return the existing row for identical bytes."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes (store the file exactly as fetched)")
    digest = sha256_hex(payload)
    row = conn.execute(
        """
        INSERT INTO l0_raw (company_id, channel, external_ref, sha256, content_type, payload, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (company_id, sha256) DO NOTHING
        RETURNING id
        """,
        (company_id, channel, external_ref, digest, content_type, bytes(payload), json.dumps(meta or {})),
    ).fetchone()
    if row is not None:
        return RawRecord(id=row[0], sha256=digest, created=True)
    existing = conn.execute(
        "SELECT id FROM l0_raw WHERE company_id = %s AND sha256 = %s", (company_id, digest)
    ).fetchone()
    return RawRecord(id=existing[0], sha256=digest, created=False)


def load_raw(conn: psycopg.Connection, raw_id: UUID) -> tuple[bytes, dict[str, Any]]:
    """Return (payload, meta) of a stored raw record."""
    row = conn.execute("SELECT payload, meta FROM l0_raw WHERE id = %s", (raw_id,)).fetchone()
    if row is None:
        raise KeyError(f"l0_raw {raw_id} not found")
    return bytes(row[0]), row[1]
