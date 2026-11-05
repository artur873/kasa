"""The state of the data behind every answer (briefing, step 8: "stan danych na dzień").

* ``as_of`` - the last booked day of the bank (the day the numbers describe), never "today";
* the last import per channel (``l0_raw``: bank, KSeF, files, PDF);
* the truth checks (``truth_check_result``): the latest result of every check and scope whose period
  overlaps the period of the answer - a red one becomes a warning of every metric of that period;
  a period without any recorded check is a warning too ("kontrole prawdy nie liczone").
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import psycopg

CHECK_PL = {
    "balance_continuity": "ciągłość sald",
    "match_coverage": "pokrycie dopasowań",
    "allocation_shares": "alokacje = 100%",
    "ksef_completeness": "kompletność KSeF",
    "mpp_vat": "MPP",
}
CHANNEL_PL = {"bank": "bank", "ksef": "KSeF", "file": "pliki (JPK, arkusze)", "pdf": "PDF z maili", "email": "maile"}


def data_as_of(conn: psycopg.Connection, company_id: UUID) -> date | None:
    """The last day with booked bank operations (or a closing balance) of the company."""
    row = conn.execute(
        """
        SELECT greatest(
            (SELECT max(booking_date) FROM l1_bank_transaction
              WHERE company_id = %(c)s AND status = 'booked' AND superseded_by IS NULL),
            (SELECT max(reference_date) FROM l1_bank_balance
              WHERE company_id = %(c)s AND superseded_by IS NULL AND balance_type = 'CLBD'))
        """,
        {"c": company_id},
    ).fetchone()
    return row[0] if row else None


def coverage_start(conn: psycopg.Connection, company_id: UUID) -> date | None:
    """The first day the bank history covers (before it, payments are not visible to kasa)."""
    row = conn.execute(
        "SELECT min(booking_date) FROM l1_bank_transaction WHERE company_id = %s AND status = 'booked' "
        "AND superseded_by IS NULL",
        (company_id,),
    ).fetchone()
    return row[0] if row else None


def last_imports(conn: psycopg.Connection, company_id: UUID) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT channel::text, max(fetched_at), count(*) FROM l0_raw WHERE company_id = %s GROUP BY 1 ORDER BY 1",
        (company_id,),
    ).fetchall()
    return {channel: {"label": CHANNEL_PL.get(channel, channel), "last_import": fetched.isoformat(), "files": n}
            for channel, fetched, n in rows}


def truth_checks(conn: psycopg.Connection, company_id: UUID, start: date | None, end: date | None) -> list[dict]:
    """The latest result of every (check, scope) whose period overlaps [start, end]."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (check_name, coalesce(scope, '')) check_name, scope, period_start, period_end, ok, run_at
          FROM truth_check_result
         WHERE company_id = %(c)s
           AND (%(start)s::date IS NULL OR period_end >= %(start)s::date)
           AND (%(end)s::date IS NULL OR period_start <= %(end)s::date)
         ORDER BY check_name, coalesce(scope, ''), run_at DESC
        """,
        {"c": company_id, "start": start, "end": end},
    ).fetchall()
    return [{"check": name, "label": CHECK_PL.get(name, name), "scope": scope, "period_start": ps.isoformat(),
             "period_end": pe.isoformat(), "ok": ok, "run_at": run_at.isoformat()}
            for name, scope, ps, pe, ok, run_at in rows]


def data_state(conn: psycopg.Connection, company_id: UUID, start: date | None = None,
               end: date | None = None) -> dict[str, Any]:
    as_of = data_as_of(conn, company_id)
    checks = truth_checks(conn, company_id, start, end)
    red = [c for c in checks if not c["ok"]]
    warnings = []
    for c in red:
        scope = f" ({c['scope']})" if c["scope"] else ""
        warnings.append(f"kontrola prawdy CZERWONA w okresie: {c['label']}{scope}, {c['period_start']} - "
                        f"{c['period_end']} - liczby z tego okresu z ostrzeżeniem")
    if not checks:
        warnings.append("kontrole prawdy nie były liczone dla tego okresu - liczby bez potwierdzenia kontrolą")
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "coverage_start": (cs.isoformat() if (cs := coverage_start(conn, company_id)) else None),
        "last_imports": last_imports(conn, company_id),
        "truth_checks": checks,
        "truth_checks_green": bool(checks) and not red,
        "red_checks": len(red),
        "warnings": warnings,
    }
