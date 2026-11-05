"""Command line: ``python -m kasa <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from kasa.core import migrate as migrations
from kasa.core.checks import run_checks
from kasa.core.companies import init_companies
from kasa.core.db import DATABASE_URL_ENV, database_url, transaction
from kasa.core.extension import ExtensionError, discover_extensions, extension_search_paths


def cmd_migrate(args: argparse.Namespace) -> int:
    applied = migrations.migrate(args.database_url)
    if not applied:
        print("Brak nowych migracji - baza jest aktualna.")
    for migration in applied:
        print(f"Zastosowano {migration.label}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    cmd_migrate(args)
    extensions = discover_extensions()
    with transaction(url=args.database_url) as conn:
        results = init_companies(conn, extensions.values())
    for company, created in results:
        state = "utworzona" if created else "już istnieje"
        print(f"Firma {company.slug} ({company.name}, rozszerzenie {company.extension_slug}): {state}")
    if not results:
        print("Nie znaleziono rozszerzeń - nie utworzono żadnej firmy.")
    return 0


def cmd_extensions_list(args: argparse.Namespace) -> int:
    extensions = discover_extensions()
    print("Ścieżki: " + os.pathsep.join(str(p) for p in extension_search_paths()))
    if not extensions:
        print("Brak rozszerzeń.")
        return 0
    header = ("slug", "firma", "obiekt kosztowy", "kategorie", "mapowania importu", "katalog")
    rows = [
        (
            e.slug,
            e.name,
            e.cost_object_kind,
            str(len(e.categories)),
            ", ".join(e.import_mappings) or "-",
            str(getattr(e, "directory", "-")),
        )
        for e in extensions.values()
    ]
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(len(header))]
    for row in (header, *rows):
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    results = run_checks(args.database_url)
    for result in results:
        mark = "OK  " if result.ok else "BŁĄD"
        print(f"[{mark}] {result.name}: {result.detail}")
    failed = [r for r in results if not r.ok]
    print("Wszystkie kontrole zaliczone." if not failed else f"Niezaliczone kontrole: {len(failed)}.")
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kasa", description="Kasa - segment finansowy Spinacza 2.0")
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"adres bazy (domyślnie zmienna {DATABASE_URL_ENV} albo lokalna baza kasa)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="komenda")

    sub.add_parser("migrate", help="zastosuj oczekujące migracje").set_defaults(handler=cmd_migrate)
    sub.add_parser("init-db", help="migracje + firmy z rozszerzeń (z obiektem UNASSIGNED)").set_defaults(
        handler=cmd_init_db
    )
    ext = sub.add_parser("extensions", help="rozszerzenia firm")
    ext_sub = ext.add_subparsers(dest="extensions_command", required=True, metavar="akcja")
    ext_sub.add_parser("list", help="lista załadowanych rozszerzeń").set_defaults(handler=cmd_extensions_list)
    sub.add_parser("check", help="kontrola stanu bazy, migracji i rozszerzeń").set_defaults(handler=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.database_url = args.database_url or database_url()
    try:
        return args.handler(args)
    except (migrations.MigrationError, ExtensionError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
