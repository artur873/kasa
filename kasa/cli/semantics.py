"""``python -m kasa semantics ...`` (model readings) and ``python -m kasa control-set ...`` (control sets)."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

import psycopg

from kasa.core import control_sets as cs
from kasa.core.companies import Company, ensure_company, get_company
from kasa.core.db import connect, transaction
from kasa.core.extension import get_extension
from kasa.core.providers.llm import get_llm_provider
from kasa.core.retention import expire_llm_payloads
from kasa.core.semantics.run import SCOPES, run_semantics
from kasa.reference import ground_truth as reference


def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _company(conn: psycopg.Connection, slug: str) -> Company:
    try:
        return get_company(conn, slug)
    except LookupError:
        company, _ = ensure_company(conn, get_extension(slug))
        print(f"Utworzono firmę {company.slug} z rozszerzenia {company.extension_slug}.")
        return company


# ---------------------------------------------------------------------------
# semantics
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging()
    provider = get_llm_provider(args.provider)
    if args.estimate:
        return cmd_estimate(args)
    # autocommit: every subject is committed on its own (an interrupted first run keeps what was paid for)
    with connect(args.database_url, autocommit=True) as conn:
        company = _company(conn, args.company)
        report = run_semantics(conn, company, args.scope, provider=provider)
    print(report.summary())
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    _setup_logging()
    provider = get_llm_provider(args.provider)
    with connect(args.database_url) as conn:
        company = _company(conn, args.company)
        report = run_semantics(conn, company, args.scope, dry_run=True, provider=provider)
        conn.rollback()
    print(report.summary())
    return 0


def cmd_expire(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        count = expire_llm_payloads(conn)
    print(f"Wygasło promptów i odpowiedzi modelu: {count} (retencja 90 dni).")
    return 0


# ---------------------------------------------------------------------------
# control-set
# ---------------------------------------------------------------------------


def _print_status(status: dict) -> None:
    print(f"Zbiór {status['name']} ({status['kind']}, {status['company']}): {status['progress']}"
          + (" - zakończony" if status["completed"] else ""))
    print(f"  id: {status['id']}")
    print(f"  wartownicy: {status['sentinels']}" + (
        f" (zgodnych {status['sentinel_results']['passed']}, niezgodnych {status['sentinel_results']['failed']})"
        if "sentinel_results" in status else " (wynik po zakończeniu zbioru)"))
    if status["fixture_labels"]:
        print(f"  etykiety z prawdy referencyjnej (fixture): {status['fixture_labels']}")
    if status["median_duration_ms"] is not None:
        print(f"  mediana czasu: {status['median_duration_ms']} ms, szybszych niż 0,8 s: {status['fast_labels']}")
    if status["label_changes"]:
        print(f"  zmiany etykiet (historia): {status['label_changes']}")


def cmd_cs_create(args: argparse.Namespace) -> int:
    try:
        with transaction(url=args.database_url) as conn:
            company = _company(conn, args.company)
            expected = reference.sentinels_for(conn, company, args.kind, args.ground_truth)
            info = cs.create_control_set(conn, company, kind=args.kind, size=args.size, name=args.name, seed=args.seed,
                                         expected=expected, created_by=args.user)
            status = cs.status(conn, info.id)
    except cs.ControlSetError as exc:            # raised inside the transaction: nothing is written
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    if info.items < args.size:
        print(f"Danych jest mniej niż {args.size} - zbiór obejmuje wszystkie dostępne ({info.items}).")
    if not info.sentinels:
        print("Uwaga: brak pliku prawdy referencyjnej - zbiór bez wartowników.")
    _print_status(status)
    print(f"  ziarno losowania: {info.seed}")
    return 0


def cmd_cs_list(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        sets = cs.list_sets(conn, company.id)
    if not sets:
        print("Brak zbiorów kontrolnych.")
    for s in sets:
        print(f"{str(s['id'])[:8]}  {s['name']}  {s['kind']}  {s['labeled']}/{s['total']}"
              + ("  zakończony" if s["completed_at"] else ""))
    return 0


def cmd_cs_status(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        try:
            set_id = cs.find_set(conn, args.set)
        except LookupError as exc:
            print(f"Błąd: {exc}", file=sys.stderr)
            return 1
        _print_status(cs.status(conn, set_id))
    return 0


def cmd_cs_export(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        try:
            set_id = cs.find_set(conn, args.set)
        except LookupError as exc:
            print(f"Błąd: {exc}", file=sys.stderr)
            return 1
        data = cs.export_set(conn, set_id)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Zapisano {args.out}")
    else:
        print(text)
    return 0


def cmd_cs_import(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else reference.default_path(args.company)
    if path is None or not path.is_file():
        print("Błąd: brak pliku prawdy referencyjnej (--file)", file=sys.stderr)
        return 1
    try:
        with transaction(url=args.database_url) as conn:
            company = _company(conn, args.company)
            set_id = cs.find_set(conn, args.set) if args.set else None
            info = reference.import_ground_truth(conn, company, kind=args.kind, truth=reference.load(path),
                                                 set_id=set_id, name=args.name)
            status = cs.status(conn, info.id)
    except (cs.ControlSetError, LookupError) as exc:     # nothing is written
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    _print_status(status)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("semantics", help="odczyt tytułów i pozycji przez model (przez bramkę)")
    actions = parser.add_subparsers(dest="semantics_command", required=True, metavar="akcja")
    for name, handler, text in (("run", cmd_run, "przeczytaj tytuły, pozycje i wiersze (raz na wersję promptu)"),
                                ("estimate", cmd_estimate, "szacunek: ile wywołań i jaki koszt - bez wywołań")):
        p = actions.add_parser(name, help=text)
        p.add_argument("--company", required=True, help="slug firmy (np. odm)")
        p.add_argument("--scope", choices=SCOPES, default="all")
        p.add_argument("--provider", choices=("mock", "vertex"), help="domyślnie KASA_LLM_PROVIDER")
        if name == "run":
            p.add_argument("--estimate", action="store_true", help="tylko szacunek (jak semantics estimate)")
        p.set_defaults(handler=handler)
    p = actions.add_parser("expire-payloads", help="retencja: usuń treść promptów i odpowiedzi starszych niż 90 dni")
    p.set_defaults(handler=cmd_expire)

    parser = sub.add_parser("control-set", help="zbiory kontrolne (etykiety do kalibracji progów)")
    actions = parser.add_subparsers(dest="control_set_command", required=True, metavar="akcja")
    p = actions.add_parser("create", help="wylosuj zbiór (z wartownikami co ~50 pozycji)")
    p.add_argument("--company", required=True)
    p.add_argument("--kind", choices=cs.KINDS, required=True)
    p.add_argument("--size", type=int, required=True)
    p.add_argument("--name")
    p.add_argument("--seed", type=int, help="ziarno losowania (powtarzalny zbiór)")
    p.add_argument("--ground-truth", help="plik prawdy referencyjnej dla wartowników "
                                          "(domyślnie fixtures/<firma>/ground_truth.json)")
    p.add_argument("--user", default=getpass.getuser())
    p.set_defaults(handler=cmd_cs_create)
    p = actions.add_parser("list", help="zbiory firmy")
    p.add_argument("--company", required=True)
    p.set_defaults(handler=cmd_cs_list)
    p = actions.add_parser("status", help="postęp zbioru (wynik wartowników po zakończeniu)")
    p.add_argument("--set", required=True, help="id (lub jego początek) albo nazwa")
    p.set_defaults(handler=cmd_cs_status)
    p = actions.add_parser("export", help="etykiety w układzie ground_truth.json")
    p.add_argument("--set", required=True)
    p.add_argument("--out", help="plik wynikowy (domyślnie na ekran)")
    p.set_defaults(handler=cmd_cs_export)
    p = actions.add_parser("import-ground-truth", help="etykiety z ground_truth.json jako etykiety 'fixture'")
    p.add_argument("--company", required=True)
    p.add_argument("--kind", choices=cs.KINDS, default="transaction")
    p.add_argument("--set", help="istniejący zbiór (domyślnie nowy z całą prawdą referencyjną)")
    p.add_argument("--name")
    p.add_argument("--file", help="plik prawdy referencyjnej (domyślnie fixtures/<firma>/ground_truth.json)")
    p.set_defaults(handler=cmd_cs_import)
