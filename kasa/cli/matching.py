"""``python -m kasa match ...`` - matching transfers to invoices (briefing, step 4)."""

from __future__ import annotations

import argparse
import calendar
import logging
from datetime import date

import psycopg

from kasa.core import control_sets as cs
from kasa.core.checks import check_match_coverage
from kasa.core.companies import Company, get_company
from kasa.core.db import connect
from kasa.core.matching.calibrate import calibrate, latest_metrics, untrustworthy_warning
from kasa.core.matching.engine import money
from kasa.core.matching.explain import explain, find_transaction, render
from kasa.core.matching.run import run_matching


def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _company(conn: psycopg.Connection, slug: str) -> Company:
    return get_company(conn, slug)


def _month(text: str | None) -> tuple[date, date] | None:
    if not text:
        return None
    year, month = (int(part) for part in text.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging()
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        period = _month(args.month)
        report = run_matching(conn, company, period=period, dry_run=args.dry_run, auto_decide=not args.no_auto)
        coverage = None
        if args.dry_run:
            conn.rollback()
        else:
            # truth check 2 after every run (CLAUDE.md, rule 9), recorded in truth_check_result
            months = [period] if period else [tuple(r) for r in conn.execute(
                """
                SELECT DISTINCT date_trunc('month', booking_date)::date,
                       (date_trunc('month', booking_date) + interval '1 month - 1 day')::date
                  FROM l1_bank_transaction WHERE company_id = %s AND status = 'booked' AND superseded_by IS NULL
                 ORDER BY 1
                """,
                (company.id,),
            ).fetchall()]
            coverage = [check_match_coverage(conn, company.id, start, end) for start, end in months]
            conn.commit()
    print(report.summary())
    for check in coverage or []:
        print(f"  kontrola prawdy 2 ({check.period_start:%Y-%m}): {'zielona' if check.ok else 'CZERWONA'} - "
              f"{check.summary()}")
    return 0 if not coverage or all(c.ok for c in coverage) else 1


def cmd_calibrate(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        set_id = cs.find_set(conn, args.control_set)
        report = calibrate(conn, company.id, set_id, record=not args.no_record)
        conn.commit()
    print(report.summary())
    print(f"Zapisano w calibration_result (run {report.run_id})." if not args.no_record else "Bez zapisu.")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company_id = _company(conn, args.company).id if args.company else None
        transaction_id = find_transaction(conn, args.ref, company_id)
        data = explain(conn, transaction_id)
        conn.rollback()
    print(render(data))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    period = _month(args.month)
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        start, end = period or conn.execute(
            """
            SELECT coalesce(min(booking_date), current_date), coalesce(max(booking_date), current_date)
              FROM l1_bank_transaction WHERE company_id = %s AND status = 'booked' AND superseded_by IS NULL
            """,
            (company.id,),
        ).fetchone()
        booked, booked_sum = conn.execute(
            """
            SELECT count(*), coalesce(sum(abs(amount_minor)), 0) FROM l1_bank_transaction
             WHERE company_id = %s AND status = 'booked' AND superseded_by IS NULL AND booking_date BETWEEN %s AND %s
            """,
            (company.id, start, end),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT s.final_status::text, coalesce(s.subtype, '-'), s.method::text, count(*), sum(abs(t.amount_minor))
              FROM l3_transaction_status s JOIN l1_bank_transaction t ON t.id = s.transaction_id
             WHERE s.company_id = %s AND s.status = 'active' AND t.booking_date BETWEEN %s AND %s
             GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            (company.id, start, end),
        ).fetchall()
        queue = conn.execute(
            """
            SELECT count(DISTINCT g.subject_id),
                   count(*) FILTER (WHERE g.rank = 1 AND g.evidence ->> 'badge' = 'high'),
                   count(*) FILTER (WHERE g.rank = 1 AND g.evidence ->> 'badge' = 'medium'),
                   count(*) FILTER (WHERE g.rank = 1 AND g.evidence ->> 'badge' = 'low')
              FROM l2_suggestion g JOIN l1_bank_transaction t ON t.id = g.subject_id
             WHERE g.company_id = %s AND g.kind = 'match' AND g.status = 'open' AND t.booking_date BETWEEN %s AND %s
            """,
            (company.id, start, end),
        ).fetchone()
        coverage = check_match_coverage(conn, company.id, start, end, record=False)
        warning = untrustworthy_warning(latest_metrics(conn, company.id))
        conn.rollback()
    decided = sum(r[3] for r in rows)
    print(f"Dopasowanie {company.slug}, {start} - {end}: zaksięgowanych przelewów {booked} "
          f"({money(booked_sum)} obrotu)")
    print(f"  z decyzją: {decided} ({decided / booked:.0%})" if booked else "  brak przelewów")
    for status, subtype, method, count, amount in rows:
        print(f"    {status:<13} {subtype:<14} {method:<6} {count:>4}  {money(amount)}")
    print(f"  w kolejce (otwarte sugestie): {queue[0]} - odznaka pierwszej propozycji: wysoka {queue[1]}, "
          f"średnia {queue[2]}, niska {queue[3]}")
    print(f"  bez decyzji i bez sugestii: {booked - decided - queue[0]}")
    print(f"  kontrola prawdy 2: {'zielona' if coverage.ok else 'CZERWONA'} - {coverage.summary()}")
    if warning:
        print(f"  {warning}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    match = sub.add_parser("match", help="dopasowanie przelew ↔ faktura (krok 4)")
    match_sub = match.add_subparsers(dest="match_command", required=True, metavar="akcja")

    run = match_sub.add_parser("run", help="sugestie dopasowań + automat (trzy warunki naraz)")
    run.add_argument("--company", required=True)
    run.add_argument("--month", help="RRRR-MM (domyślnie wszystkie przelewy bez decyzji)")
    run.add_argument("--dry-run", action="store_true", help="tylko raport, nic nie zapisuje")
    run.add_argument("--no-auto", action="store_true", help="same sugestie, bez decyzji automatu")
    run.set_defaults(handler=cmd_run)

    cal = match_sub.add_parser("calibrate", help="progi automatu na zbiorze kontrolnym (0 fałszywych automatów)")
    cal.add_argument("--company", required=True)
    cal.add_argument("--control-set", required=True, help="id, prefiks id albo nazwa zbioru kontrolnego")
    cal.add_argument("--no-record", action="store_true", help="bez zapisu do calibration_result")
    cal.set_defaults(handler=cmd_calibrate)

    exp = match_sub.add_parser("explain", help="łańcuch: fakty → sygnały → kandydaci → sugestie → decyzja")
    exp.add_argument("ref", help="entry_reference albo id przelewu")
    exp.add_argument("--company")
    exp.set_defaults(handler=cmd_explain)

    st = match_sub.add_parser("status", help="rozkład statusów końcowych i pokrycie")
    st.add_argument("--company", required=True)
    st.add_argument("--month", help="RRRR-MM")
    st.set_defaults(handler=cmd_status)
