"""``python -m kasa bank ...``: bank accounts, ingestion, statement files, consent, balance check."""

from __future__ import annotations

import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

from kasa.core.bank.fx import FixtureNbpSource, NbpApiSource, load_rates
from kasa.core.bank.ingest import IngestResult, import_statement_file, ingest_company
from kasa.core.checks import BALANCE_CONTINUITY_CHECK, run_truth_checks
from kasa.core.companies import Company, ensure_company, get_company
from kasa.core.db import transaction
from kasa.core.extension import ExtensionError, get_extension
from kasa.core.providers.bank import MockEnableBanking, ProviderError, get_bank_provider


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"niepoprawna data {text!r} (oczekiwano RRRR-MM-DD)") from None


def _month(text: str) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in text.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"niepoprawny miesiąc {text!r} (oczekiwano RRRR-MM)") from None


def _company(conn, slug: str) -> Company:
    try:
        return get_company(conn, slug)
    except LookupError:
        try:
            company, _ = ensure_company(conn, get_extension(slug))
        except ExtensionError as exc:
            raise SystemExit(f"Błąd: nie ma firmy {slug!r} ani rozszerzenia o tej nazwie ({exc})") from None
        print(f"Utworzono firmę {company.slug} z rozszerzenia {company.extension_slug}.")
        return company


def _print_results(results: list[IngestResult]) -> None:
    for result in results:
        print(result.summary())
        for warning in result.warnings:
            print(f"  uwaga: {warning}")


def _print_truth(checks) -> bool:
    ok = True
    for check in checks:
        mark = "OK  " if check.ok else "BŁĄD"
        label = "ciągłość sald" if check.check_name == BALANCE_CONTINUITY_CHECK else "MPP"
        print(f"[{mark}] {label}: {check.summary()}")
        ok = ok and check.ok
    return ok


def cmd_accounts(args: argparse.Namespace) -> int:
    provider = get_bank_provider(args.provider)
    print(f"Dostawca: {type(provider).__name__}")
    for account in provider.list_accounts(args.company):
        print(f"  {account.iban}  {account.currency}  {account.kind:<8} {account.bank_name or '-'}  "
              f"{account.details or account.product or ''}")
    with transaction(url=args.database_url) as conn:
        rows = conn.execute(
            """
            SELECT a.iban, a.currency, a.kind::text, a.bank, p.iban AS parent,
                   (SELECT count(*) FROM l1_bank_transaction t WHERE t.account_id = a.id AND t.superseded_by IS NULL
                       AND t.status = 'booked')
              FROM l1_bank_account a JOIN company c ON c.id = a.company_id
              LEFT JOIN l1_bank_account p ON p.id = a.parent_account_id
             WHERE c.slug = %s ORDER BY a.iban
            """,
            (args.company,),
        ).fetchall()
    if rows:
        print("W bazie:")
        for iban, currency, kind, bank, parent, booked in rows:
            suffix = f" (rachunek VAT do {parent})" if parent else ""
            print(f"  {iban}  {currency}  {kind:<8} {bank or '-'}  operacji: {booked}{suffix}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    if args.date_to < args.date_from:
        print("Błąd: --to jest wcześniej niż --from", file=sys.stderr)
        return 2
    provider = get_bank_provider(args.provider)
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        fx_source = args.fx or ("fixture" if isinstance(provider, MockEnableBanking) else "nbp")
        if fx_source != "none":
            source = FixtureNbpSource() if fx_source == "fixture" else NbpApiSource()
            from datetime import timedelta

            loaded = load_rates(conn, source, args.currencies.split(","), args.date_from - timedelta(days=10),
                                args.date_to)
            print(f"Kursy NBP ({fx_source}): dopisano {loaded}")
        results = ingest_company(conn, company, provider, args.date_from, args.date_to, ibans=args.account or None)
        _print_results(results)
        checks = run_truth_checks(conn, company.id, args.date_from, args.date_to)
        ok = _print_truth(checks)
    print(f"Zapisano (dostawca {type(provider).__name__}).")
    return 0 if ok else 1


def _import_file(args: argparse.Namespace, fmt: str) -> int:
    path = Path(args.file)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        try:
            results = import_statement_file(conn, company, payload, fmt=fmt, filename=path.name,
                                            account_kind=args.kind)
        except ValueError as exc:
            print(f"Błąd: {exc}", file=sys.stderr)
            return 2
        _print_results(results)
        days = [d for r in results for d in (r.first_booking, r.last_booking) if d]
        ok = _print_truth(run_truth_checks(conn, company.id, min(days), max(days))) if days else True
    return 0 if ok else 1


def cmd_import_camt(args: argparse.Namespace) -> int:
    return _import_file(args, "camt053")


def cmd_import_mt940(args: argparse.Namespace) -> int:
    return _import_file(args, "mt940")


def cmd_consent(args: argparse.Namespace) -> int:
    provider = get_bank_provider(args.provider)
    try:
        consent = provider.consent_status()
    except ProviderError as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    until = consent.valid_until.date().isoformat() if consent.valid_until else "nieznana"
    print(f"Dostawca: {type(provider).__name__}; status zgody: {consent.status}; ważna do {until}"
          + (f" (zostało {consent.days_left} dni)" if consent.days_left is not None else ""))
    print(f"Rachunki objęte zgodą: {len(consent.accounts)}")
    if consent.alert:
        print("UWAGA: zgoda wygasa w ciągu 14 dni - odnów ją w banku przez EnableBanking (docs/BANK.md).")
        return 1
    return 0


def cmd_balance_check(args: argparse.Namespace) -> int:
    start, end = args.month
    with transaction(url=args.database_url) as conn:
        company = get_company(conn, args.company)
        checks = run_truth_checks(conn, company.id, start, end)
    if not checks:
        print(f"Brak danych bankowych firmy {company.slug} za {start:%Y-%m}.")
        return 1
    return 0 if _print_truth(checks) else 1


def register(sub: argparse._SubParsersAction) -> None:
    bank = sub.add_parser("bank", help="bank: rachunki, pobranie operacji, wyciągi, zgoda, bilans")
    bank_sub = bank.add_subparsers(dest="bank_command", required=True, metavar="akcja")
    providers = ["mock", "enablebanking"]

    p = bank_sub.add_parser("accounts", help="rachunki u dostawcy i w bazie")
    p.add_argument("--company", default="odm", help="slug firmy (domyślnie odm)")
    p.add_argument("--provider", choices=providers, default=None, help="domyślnie KASA_BANK_PROVIDER")
    p.set_defaults(handler=cmd_accounts)

    p = bank_sub.add_parser("ingest", help="pobierz operacje i salda za okres (L0 + L1), potem kontrole prawdy")
    p.add_argument("--company", required=True)
    p.add_argument("--from", dest="date_from", type=_date, required=True)
    p.add_argument("--to", dest="date_to", type=_date, required=True)
    p.add_argument("--provider", choices=providers, default=None, help="domyślnie KASA_BANK_PROVIDER")
    p.add_argument("--account", action="append", help="tylko ten IBAN (można powtórzyć)")
    p.add_argument("--fx", choices=["fixture", "nbp", "none"], default=None,
                   help="źródło kursów NBP (domyślnie fixture dla mock, nbp dla EnableBanking)")
    p.add_argument("--currencies", default="EUR,USD", help="waluty kursów NBP (domyślnie EUR,USD)")
    p.set_defaults(handler=cmd_ingest)

    for name, handler, label in (("import-camt", cmd_import_camt, "camt.053"), ("import-mt940", cmd_import_mt940,
                                                                                "MT940")):
        p = bank_sub.add_parser(name, help=f"wczytaj wyciąg {label} z pliku (kanał zapasowy)")
        p.add_argument("file")
        p.add_argument("--company", default="odm")
        p.add_argument("--kind", choices=["current", "vat", "savings"], default=None,
                       help="rodzaj rachunku, jeśli nie ma go jeszcze w bazie (domyślnie current)")
        p.set_defaults(handler=handler)

    p = bank_sub.add_parser("consent", help="ważność zgody bankowej (alert 14 dni przed końcem)")
    p.add_argument("--provider", choices=providers, default=None)
    p.set_defaults(handler=cmd_consent)

    p = bank_sub.add_parser("balance-check", help="kontrole prawdy: ciągłość sald i MPP za miesiąc")
    p.add_argument("--company", required=True)
    p.add_argument("--month", type=_month, required=True, help="RRRR-MM")
    p.set_defaults(handler=cmd_balance_check)
