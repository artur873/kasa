"""State of the cards of the queue (``queue_state``): shown, decided, deferred - never deleted.

* ``mark_shown`` - the FIRST time a card is sent (``COALESCE``): a reload never resets the clock
  (review of phase 3, point 8);
* ``mark_decided`` - closes the open cards of a subject for a decision: time of the decision
  (``duration_ms`` = decision - first shown) and the alarm "too fast": < 0.8 s on a card with the badge
  "low" (briefing, step 7), < 1.5 s on a card without a badge - a cost object candidate, a rule, an audit
  sample (review of 4C/5, point 12: nothing on such a card can be read in under 1.5 s);
* ``defer`` - "Odłóż" until a date (default: tomorrow);
* ``mark_revoked`` - a revoked decision: its cards say so (the subject returns at the next build).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

FAST_MS = 800              # briefing, step 7: a decision under 0.8 s on a "low" card is an alarm
FAST_NO_BADGE_MS = 1500    # review of 4C/5, point 12: under 1.5 s on a card without a badge
NO_BADGE_KINDS = ("cost_object_candidate", "rule_candidate", "rule_ready", "audit")


def get(conn: psycopg.Connection, item_id: UUID) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM queue_state WHERE id = %s", (item_id,)).fetchone()
    if row is None:
        raise LookupError(f"Nie ma karty {item_id}")
    return row


def mark_shown(conn: psycopg.Connection, item_id: UUID) -> datetime:
    row = conn.execute(
        "UPDATE queue_state SET shown_at = coalesce(shown_at, clock_timestamp()) WHERE id = %s RETURNING shown_at",
        (item_id,),
    ).fetchone()
    sentinel = conn.execute("SELECT sentinel_id FROM queue_state WHERE id = %s", (item_id,)).fetchone()
    if sentinel and sentinel[0]:
        conn.execute("UPDATE queue_sentinel SET shown_at = coalesce(shown_at, %s) WHERE id = %s AND shown_at IS NULL",
                     (row[0], sentinel[0]))
    return row[0]


def _close(conn: psycopg.Connection, where: str, params: tuple, *, decision_id: UUID | None, decided_by: str,
           outcome: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            UPDATE queue_state
               SET decided_at = clock_timestamp(), decided_by = %s, decision_id = %s, outcome = %s,
                   closed_at = clock_timestamp(),
                   duration_ms = CASE WHEN shown_at IS NOT NULL
                       THEN greatest(0, (extract(epoch FROM clock_timestamp() - shown_at) * 1000))::integer END,
                   fast_alarm = coalesce(shown_at IS NOT NULL AND (
                       (badge = 'low' AND card_kind <> ALL(%s)
                        AND extract(epoch FROM clock_timestamp() - shown_at) * 1000 < %s)
                       OR ((badge IS NULL OR card_kind = ANY(%s))
                           AND extract(epoch FROM clock_timestamp() - shown_at) * 1000 < %s)), false)
             WHERE closed_at IS NULL AND {where}
            RETURNING *
            """,
            (decided_by, decision_id, outcome, list(NO_BADGE_KINDS), FAST_MS, list(NO_BADGE_KINDS), FAST_NO_BADGE_MS,
             *params),
        ).fetchall()


def mark_decided(conn: psycopg.Connection, company_id: UUID, subject_type: str, subject_id: UUID,
                 card_kinds: tuple[str, ...], *, decision_id: UUID | None, decided_by: str,
                 outcome: str) -> list[dict[str, Any]]:
    if not card_kinds:
        return []
    return _close(conn, "company_id = %s AND subject_type = %s AND subject_id = %s AND card_kind = ANY(%s)",
                  (company_id, subject_type, subject_id, list(card_kinds)), decision_id=decision_id,
                  decided_by=decided_by, outcome=outcome)


def close_item(conn: psycopg.Connection, item_id: UUID, *, decision_id: UUID | None, decided_by: str,
               outcome: str) -> dict[str, Any] | None:
    rows = _close(conn, "id = %s", (item_id,), decision_id=decision_id, decided_by=decided_by, outcome=outcome)
    return rows[0] if rows else None


def _until(until: datetime | date | None) -> datetime:
    if until is None:
        return datetime.now(timezone.utc) + timedelta(days=1)
    if isinstance(until, datetime):
        return until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    return datetime.combine(until, time(6, 0), tzinfo=timezone.utc)


def defer(conn: psycopg.Connection, company_id: UUID, card_kind: str, subject_type: str, subject_id: UUID,
          until: datetime | date | None, *, decided_by: str, suggestion_ids: list[UUID] | None = None) -> datetime:
    """The card goes to the end of the queue until ``until``; created if the queue was not built yet."""
    moment = _until(until)
    row = conn.execute(
        """
        UPDATE queue_state SET deferred_until = %s, skipped_at = clock_timestamp()
         WHERE company_id = %s AND card_kind = %s AND subject_type = %s AND subject_id = %s AND closed_at IS NULL
        RETURNING id
        """,
        (moment, company_id, card_kind, subject_type, subject_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO queue_state (company_id, card_kind, subject_type, subject_id, suggestion_ids, rank_reason,
                                     priority, deferred_until, skipped_at)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s, clock_timestamp())
            """,
            (company_id, card_kind, subject_type, subject_id, suggestion_ids or [],
             f"odłożone przez {decided_by}", moment),
        )
    return moment


def note_rejection(conn: psycopg.Connection, company_id: UUID, kind: str, subject_type: str, subject_id: UUID,
                   suggestion_id: UUID) -> None:
    """A rejected proposal leaves the card; the card stays (other proposals or "Inne…")."""
    conn.execute(
        """
        UPDATE queue_state SET suggestion_ids = array_remove(suggestion_ids, %s)
         WHERE company_id = %s AND card_kind = %s AND subject_type = %s AND subject_id = %s AND closed_at IS NULL
        """,
        (suggestion_id, company_id, "sensitive" if kind == "category" else kind, subject_type, subject_id),
    )


def mark_revoked(conn: psycopg.Connection, decision_id: UUID, revoke_id: UUID) -> None:
    """Nothing to delete: the closed card keeps its history; a new card comes with the next build."""
    conn.execute(
        "UPDATE queue_state SET rank_reason = rank_reason || ' (decyzja cofnięta)' WHERE decision_id = %s",
        (decision_id,),
    )
