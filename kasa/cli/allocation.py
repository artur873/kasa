"""``python -m kasa cost-objects|allocate|report ...`` - cost object candidates, allocation, company report."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from uuid import UUID

import psycopg

from kasa.core.allocation.run import allocation_status, run_allocation
from kasa.core.companies import Company, ensure_company, get_company
from kasa.core.cost_objects_discovery import decide as co_decide
from kasa.core.cost_objects_discovery.discover import discover
from kasa.core.cost_objects_discovery.pairs import load_model, retrain
from kasa.core.db import connect, transaction
from kasa.core.extension import get_extension
from kasa.core.reports.company import company_report, fmt_pln, render
from kasa.core.reports.export import export_csv


def _company(conn: psycopg.Connection, slug: str) -> Company:
    try:
        return get_company(conn, slug)
    except LookupError:
        company, _ = ensure_company(conn, get_extension(slug))
        return company


def _resolve_id(conn: psycopg.Connection, table: str, text: str, company_id: UUID | None = None) -> UUID:
    """A full id or a unique prefix (at least 6 characters) of an id."""
    try:
        return UUID(text)
    except ValueError:
        pass
    if len(text) < 6:
        raise SystemExit(f"Za krótki identyfikator {text!r} (min. 6 znaków)")
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id::text LIKE %s AND (%s::uuid IS NULL OR company_id = %s::uuid) LIMIT 2",
        (text + "%", company_id, company_id),
    ).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"Identyfikator {text!r}: {'brak' if not rows else 'niejednoznaczny'}")
    return rows[0][0]


def _who(args: argparse.Namespace) -> str:
    return args.by or getpass.getuser()


# ---------------------------------------------------------------------------
# cost-objects
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        report = discover(conn, company, created_by=_who(args))
        if args.dry_run:
            print(report.summary())
            print("Próba: nic nie zapisano.")
            raise _Rollback()
    print(report.summary())
    return 0


class _Rollback(Exception):
    pass


def _run(fn, args) -> int:
    try:
        return fn(args)
    except _Rollback:
        return 0
    except (co_decide.CostObjectDecisionError, PermissionError, LookupError, ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        extension = get_extension(company.extension_slug)
        rows = conn.execute(
            """
            SELECT co.id, co.status::text, co.method::text, co.name, co.attrs,
                   (SELECT count(*) FROM l2_cost_object_member m WHERE m.candidate_id = co.id
                      AND m.status IN ('open', 'accepted')),
                   (SELECT s.proposal ->> 'weakest_link' FROM l2_suggestion s WHERE s.subject_id = co.id
                      AND s.kind = 'cost_object_candidate' ORDER BY s.created_at DESC LIMIT 1),
                   (SELECT s.proposal ->> 'revenue_minor' FROM l2_suggestion s WHERE s.subject_id = co.id
                      AND s.kind = 'cost_object_candidate' ORDER BY s.created_at DESC LIMIT 1),
                   (SELECT s.proposal ->> 'cost_minor' FROM l2_suggestion s WHERE s.subject_id = co.id
                      AND s.kind = 'cost_object_candidate' ORDER BY s.created_at DESC LIMIT 1)
              FROM l3_cost_object co
             WHERE co.company_id = %s AND co.system_code IS NULL AND (%s = 'all' OR co.status::text = %s)
             ORDER BY co.attrs ->> 'date_start', co.name
            """,
            (company.id, args.status, args.status),
        ).fetchall()
    print(f"{extension.cost_object_label} - {company.name} ({args.status}): {len(rows)}")
    for oid, status, method, name, attrs, members, weakest, revenue, cost in rows:
        extra = ", ".join(f"{k}={v}" for k, v in attrs.items() if k != "name")
        money = ""
        if revenue is not None:
            money = f" | przychód {fmt_pln(int(revenue))}, koszt {fmt_pln(int(cost))}, najsłabsze {weakest}"
        print(f"  {str(oid)[:8]}  {status:9} {method:5} {name} [{extra}] rekordy: {members}{money}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        oid = _resolve_id(conn, "l3_cost_object", args.id)
        obj = conn.execute("SELECT name, status::text, attrs, evidence FROM l3_cost_object WHERE id = %s",
                           (oid,)).fetchone()
        print(f"{obj[0]} ({obj[1]}) {json.dumps(obj[2], ensure_ascii=False)}")
        card = conn.execute(
            "SELECT evidence FROM l2_suggestion WHERE subject_id = %s AND kind = 'cost_object_candidate' "
            "ORDER BY created_at DESC LIMIT 1", (oid,)).fetchone()
        for fact in (card[0] if card else {}).get("facts", []):
            print(f"  - {fact}")
        for mid, rtype, rid, strength, anchor, status in conn.execute(
            """
            SELECT m.id, m.record_type::text, m.record_id, m.link_strength, m.is_anchor, m.status::text
              FROM l2_cost_object_member m WHERE m.candidate_id = %s ORDER BY m.is_anchor DESC, m.link_strength DESC
            """,
            (oid,),
        ).fetchall():
            text = conn.execute(
                """
                SELECT coalesce((SELECT d.number_raw || ': ' || l.name FROM l1_document_line l
                                   JOIN l1_document d ON d.id = l.document_id WHERE l.id = %(id)s),
                                (SELECT title_pseudonymized FROM l1_bank_transaction WHERE id = %(id)s),
                                (SELECT coalesce(cost_object_ref, '') || ' ' || coalesce(record_date::text, '')
                                   FROM l1_internal_record WHERE id = %(id)s),
                                (SELECT number_raw FROM l1_document WHERE id = %(id)s), '?')
                """,
                {"id": rid},
            ).fetchone()[0]
            mark = "*" if anchor else " "
            print(f"  {mark} {str(mid)[:8]} {rtype:15} {float(strength):.2f} {status:8} {text[:80]}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    override = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        override[key.strip()] = value.strip()
    with transaction(url=args.database_url) as conn:
        oid = _resolve_id(conn, "l3_cost_object", args.id)
        decision = co_decide.approve_candidate(conn, oid, decided_by=_who(args), attrs_override=override or None,
                                               reason=args.reason)
    print(f"Zatwierdzono {oid} (decyzja {decision}).")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        oid = _resolve_id(conn, "l3_cost_object", args.id)
        decision = co_decide.reject_candidate(conn, oid, decided_by=_who(args), reason=args.reason)
    print(f"Odrzucono {oid} (decyzja {decision}).")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        ids = [_resolve_id(conn, "l3_cost_object", i) for i in args.ids]
        into = _resolve_id(conn, "l3_cost_object", args.into) if args.into else None
        decision = co_decide.merge_candidates(conn, ids, decided_by=_who(args), into=into, reason=args.reason)
    print(f"Połączono {len(ids)} obiekty (decyzja {decision}).")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        oid = _resolve_id(conn, "l3_cost_object", args.id)
        groups = [[_resolve_id(conn, "l2_cost_object_member", m.strip()) for m in g.split(",") if m.strip()]
                  for g in args.group]
        result = co_decide.split_candidate(conn, oid, groups, decided_by=_who(args), reason=args.reason)
    print(f"Podzielono {oid} na {len(result.new_ids)}: " + ", ".join(str(i)[:8] for i in result.new_ids))
    return 0


def cmd_distinct(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        a = _resolve_id(conn, "l3_cost_object", args.a)
        b = _resolve_id(conn, "l3_cost_object", args.b)
        decision = co_decide.mark_distinct(conn, a, b, decided_by=_who(args), reason=args.reason)
    print(f"Zapisano: na pewno różne (decyzja {decision}).")
    return 0


def cmd_retrain(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        version = retrain(conn, company.id, created_by=_who(args))
        model = load_model(conn, company.id)
    print(f"Model par: wersja {model.version} (decyzji: {model.trained_on_decisions})"
          + ("" if version else " - bez zmian"))
    return 0


# ---------------------------------------------------------------------------
# allocate
# ---------------------------------------------------------------------------


def cmd_allocate_run(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        report = run_allocation(conn, company, dry_run=args.dry_run, auto=not args.no_auto, created_by=_who(args))
    print(report.summary())
    for failure in report.auto.get("failed", []):
        print(f"  automat pominął {failure['suggestion_id']}: {failure['error']}")
    return 0


def cmd_allocate_status(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        status = allocation_status(conn, company.id)
    decided = status["decided"]
    print(f"Alokacje w mocy: człowiek {decided['human']}, automat (reguła) {decided['rule']}")
    print("Otwarte sugestie (rank 1): podmiot / odznaka / sygnał / liczba / częściowe")
    for row in status["open"]:
        print(f"  {row['subject_type']:16} {row['badge'] or '-':7} {row['signal_strength'] or '-':3} "
              f"{row['count']:5} {row['partial']:3}")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def cmd_report_company(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        report = company_report(conn, company.id, args.month)
        label = f"obiekty kosztowe ({get_extension(company.extension_slug).cost_object_label})"
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report, label))
    return 0 if report.memorial_difference_minor == 0 and report.cash_difference_minor == 0 else 1


def cmd_report_export(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        content = export_csv(conn, company.id, args.month)
    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"Zapisano {args.out} ({content.count(chr(10)) - 1} wierszy).")
    else:
        sys.stdout.write(content.lstrip("﻿"))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    def add_by(p):
        p.add_argument("--by", help="kto decyduje (domyślnie użytkownik systemu)")
        p.add_argument("--reason", help="dlaczego (opcjonalnie)")

    co = sub.add_parser("cost-objects", help="obiekty kosztowe: kandydaci z danych, karta, decyzje człowieka")
    co_sub = co.add_subparsers(dest="cost_objects_command", required=True, metavar="akcja")
    p = co_sub.add_parser("discover", help="odkryj kandydatów (model par Fellegi-Sunter, idempotentnie)")
    p.add_argument("--company", default="odm")
    p.add_argument("--dry-run", action="store_true", help="tylko pokaż, nic nie zapisuj")
    p.add_argument("--by", help=argparse.SUPPRESS)
    p.set_defaults(handler=lambda a: _run(cmd_discover, a))
    p = co_sub.add_parser("list", help="lista obiektów kosztowych")
    p.add_argument("--company", default="odm")
    p.add_argument("--status", default="all", choices=["all", "candidate", "approved", "rejected", "merged"])
    p.set_defaults(handler=lambda a: _run(cmd_list, a))
    p = co_sub.add_parser("show", help="karta obiektu: fakty i rekordy (z identyfikatorami członków)")
    p.add_argument("id")
    p.set_defaults(handler=lambda a: _run(cmd_show, a))
    p = co_sub.add_parser("approve", help="zatwierdź kandydata (tylko człowiek)")
    p.add_argument("id")
    p.add_argument("--set", action="append", help="popraw atrybut: --set name='Gala ACME 2026'")
    add_by(p)
    p.set_defaults(handler=lambda a: _run(cmd_approve, a))
    p = co_sub.add_parser("reject", help="odrzuć kandydata")
    p.add_argument("id")
    add_by(p)
    p.set_defaults(handler=lambda a: _run(cmd_reject, a))
    p = co_sub.add_parser("merge", help="połącz obiekty w jeden")
    p.add_argument("ids", nargs="+")
    p.add_argument("--into", help="obiekt docelowy (domyślnie zatwierdzony albo pierwszy)")
    add_by(p)
    p.set_defaults(handler=lambda a: _run(cmd_merge, a))
    p = co_sub.add_parser("split", help="podziel kandydata: --group M1,M2 --group M3 (identyfikatory członków)")
    p.add_argument("id")
    p.add_argument("--group", action="append", required=True)
    add_by(p)
    p.set_defaults(handler=lambda a: _run(cmd_split, a))
    p = co_sub.add_parser("distinct", help="na pewno różne: odkrywanie nigdy ich nie sklei")
    p.add_argument("a")
    p.add_argument("b")
    add_by(p)
    p.set_defaults(handler=lambda a: _run(cmd_distinct, a))
    p = co_sub.add_parser("retrain", help="przelicz wagi modelu par z decyzji człowieka")
    p.add_argument("--company", default="odm")
    p.add_argument("--by", help=argparse.SUPPRESS)
    p.set_defaults(handler=lambda a: _run(cmd_retrain, a))

    al = sub.add_parser("allocate", help="alokacja do obiektów kosztowych (sygnały A-D, kaskada, automat tylko A)")
    al_sub = al.add_subparsers(dest="allocate_command", required=True, metavar="akcja")
    p = al_sub.add_parser("run", help="sugestie alokacji + automat (sygnał A / dojrzała reguła)")
    p.add_argument("--company", default="odm")
    p.add_argument("--dry-run", action="store_true", help="policz i pokaż, nic nie zapisuj")
    p.add_argument("--no-auto", action="store_true", help="bez automatu (same sugestie)")
    p.add_argument("--by", help=argparse.SUPPRESS)
    p.set_defaults(handler=lambda a: _run(cmd_allocate_run, a))
    p = al_sub.add_parser("status", help="ile zdecydowane, ile w sugestiach")
    p.add_argument("--company", default="odm")
    p.set_defaults(handler=lambda a: _run(cmd_allocate_status, a))

    rp = sub.add_parser("report", help="raport firmy (memoriałowo i kasowo) i eksport dla biura")
    rp_sub = rp.add_subparsers(dest="report_command", required=True, metavar="akcja")
    p = rp_sub.add_parser("company", help="cztery wiersze × dwa ujęcia + marża per obiekt; kod 1 = różnica kontrolna")
    p.add_argument("--company", default="odm")
    p.add_argument("--month", required=True, help="RRRR-MM")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=lambda a: _run(cmd_report_company, a))
    p = rp_sub.add_parser("export", help="CSV dla biura: dokumenty z alokacją i statusem")
    p.add_argument("--company", default="odm")
    p.add_argument("--month", required=True, help="RRRR-MM")
    p.add_argument("--out", help="plik CSV (domyślnie na standardowe wyjście)")
    p.set_defaults(handler=lambda a: _run(cmd_report_export, a))
