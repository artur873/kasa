"""``python -m kasa documents ...`` - KSeF, file import wizard, PDF from e-mails, browsing documents."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from kasa.core.companies import Company, ensure_company, get_company
from kasa.core.db import transaction
from kasa.core.extension import ColumnSpec, ExtensionError, MappingError, get_extension
from kasa.core.normalize import from_minor, normalize_invoice_number
from kasa.core.providers.ksef import WARSAW

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"data w formacie RRRR-MM-DD, nie {text!r}") from None


def _company(conn: psycopg.Connection, slug: str) -> Company:
    try:
        return get_company(conn, slug)
    except LookupError:
        company, _ = ensure_company(conn, get_extension(slug))
        print(f"Utworzono firmę {company.slug} z rozszerzenia {company.extension_slug}.")
        return company


def _money(minor: int | None, currency: str) -> str:
    if minor is None:
        return "-"
    value = from_minor(minor, currency)
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} {currency}"


def _num(value: Any) -> str:
    """Quantity or price as printed: no trailing zeros of the numeric column."""
    if value is None:
        return ""
    text = format(value.normalize(), "f") if hasattr(value, "normalize") else str(value)
    return text.replace(".", ",")


def _print_report(report: Any) -> int:
    print(report.summary())
    for warning in report.warnings:
        print(f"  uwaga: {warning}")
    for skipped in report.skipped:
        print(f"  pominięto: {skipped}")
    for error in report.errors:
        print(f"  błąd: {error}", file=sys.stderr)
    return 1 if report.errors else 0


# ---------------------------------------------------------------------------
# KSeF
# ---------------------------------------------------------------------------


def _last_sync(conn: psycopg.Connection, company: Company) -> date | None:
    row = conn.execute(
        "SELECT max((ksef_accepted_at AT TIME ZONE 'Europe/Warsaw')::date) FROM l1_document WHERE company_id = %s",
        (company.id,),
    ).fetchone()
    return row[0]


def cmd_ksef_sync(args: argparse.Namespace) -> int:
    from kasa.core.checks import check_ksef_completeness
    from kasa.core.documents.ingest import ingest_ksef
    from kasa.core.providers.ksef import get_ksef_provider

    _setup_logging()
    provider = get_ksef_provider()
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        date_to = args.date_to or datetime.now(WARSAW).date()
        date_from = args.date_from or (_last_sync(conn, company) or date_to - timedelta(days=30))
        print(f"KSeF ({provider.name}): {company.slug}, faktury przyjęte {date_from} - {date_to}")
        report = ingest_ksef(conn, company, provider, date_from, date_to, direction=args.direction)
        code = _print_report(report)
        if not args.no_check and args.direction is None:
            result = check_ksef_completeness(conn, company, provider, date_from, date_to)
            print(f"Kontrola kompletności KSeF: {result.summary()}")
            code = code or (0 if result.ok else 1)
    return code


def cmd_ksef_check(args: argparse.Namespace) -> int:
    from kasa.core.checks import check_ksef_completeness
    from kasa.core.providers.ksef import get_ksef_provider

    _setup_logging()
    provider = get_ksef_provider()
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        result = check_ksef_completeness(conn, company, provider, args.date_from, args.date_to)
    print(f"Kontrola kompletności KSeF ({provider.name}, {args.date_from} - {args.date_to}): {result.summary()}")
    for number in result.missing_in_kasa:
        print(f"  brak u nas: {number}")
    for number in result.missing_in_ksef:
        print(f"  brak w KSeF: {number}")
    for mismatch in result.mismatches:
        print(f"  różnica: {mismatch}")
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _parse_map(values: list[str]) -> dict[str, str]:
    answers = {}
    for value in values or []:
        field, sep, header = value.partition("=")
        if not sep or not field.strip() or not header.strip():
            raise SystemExit(f"--map oczekuje pole=Nagłówek, nie {value!r}")
        answers[field.strip()] = header.strip()
    return answers


def _ask(name: str, spec: ColumnSpec, headers: list[str]) -> str | None:
    print(f"Pole {name!r} (w rozszerzeniu: {spec.source!r}) - która kolumna pliku?")
    for i, header in enumerate(headers, start=1):
        print(f"  {i}. {header}")
    answer = input("Numer kolumny (Enter = pomiń): ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(headers):
        return headers[int(answer) - 1]
    return answer or None


def cmd_import(args: argparse.Namespace) -> int:
    from kasa.core.documents.csv_import import MappingIncomplete, import_table

    _setup_logging()
    path = Path(args.file)
    interactive = not args.no_input and sys.stdin.isatty()
    try:
        with transaction(url=args.database_url) as conn:
            company = _company(conn, args.company)
            report = import_table(conn, company, path.read_bytes(), filename=path.name, mapping_key=args.mapping,
                                  answers=_parse_map(args.map), ask=_ask if interactive else None,
                                  created_by=f"cli:{args.user}")
    except MappingIncomplete as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    except (MappingError, ExtensionError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    print(f"Import {path.name} (mapowanie {args.mapping}):")
    return _print_report(report)


def cmd_import_jpk(args: argparse.Namespace) -> int:
    from kasa.core.documents.ingest import ingest_jpk
    from kasa.core.documents.jpk import JpkError, parse_jpk

    _setup_logging()
    path = Path(args.file)
    payload = path.read_bytes()
    try:
        with transaction(url=args.database_url) as conn:
            if args.company:
                company = _company(conn, args.company)
            else:
                company = _company_by_jpk(conn, parse_jpk(payload).subject_nip)
            report = ingest_jpk(conn, company, payload, filename=path.name)
    except (JpkError, LookupError, ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    print(f"Import {path.name} ({company.slug}):")
    return _print_report(report)


def _company_by_jpk(conn: psycopg.Connection, nip: str | None) -> Company:
    if nip:
        row = conn.execute("SELECT slug FROM company WHERE nip = %s", (nip,)).fetchone()
        if row:
            return get_company(conn, row[0])
    rows = conn.execute("SELECT slug FROM company ORDER BY slug").fetchall()
    if len(rows) == 1:
        return get_company(conn, rows[0][0])
    raise LookupError(f"nie wiem, której firmy dotyczy JPK (NIP {nip}) - podaj --company")


def cmd_import_pdf(args: argparse.Namespace) -> int:
    from kasa.core.documents.pdf import import_pdfs
    from kasa.core.providers.llm import get_llm_provider

    _setup_logging()
    llm = get_llm_provider()
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        report = import_pdfs(conn, company, [Path(p) for p in args.paths],
                             meta_path=Path(args.meta) if args.meta else None, llm=llm)
    print(f"PDF ({llm.name}): {len(report.outcomes)} plików")
    for outcome in report.outcomes:
        flag = " [do przeglądu]" if outcome.needs_review else ""
        print(f"  {outcome.filename}: {outcome.status} {outcome.detail}{flag}")
    return _print_report(report)


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company)
        conditions = ["d.company_id = %(company)s", "d.superseded_by IS NULL"]
        params: dict[str, Any] = {"company": company.id}
        if args.direction:
            conditions.append("d.direction = %(direction)s")
            params["direction"] = args.direction
        if args.month:
            start = datetime.strptime(args.month, "%Y-%m").date()
            params["start"] = start
            params["end"] = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            conditions.append("d.issue_date >= %(start)s AND d.issue_date < %(end)s")
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                f"""
                SELECT d.id, d.direction, d.kind, d.number_raw, d.issue_date, d.currency, d.gross_minor,
                       CASE d.direction WHEN 'sales' THEN d.buyer_name ELSE d.seller_name END AS party,
                       CASE d.direction WHEN 'sales' THEN coalesce(d.buyer_nip, d.buyer_foreign_tax_id)
                                        ELSE coalesce(d.seller_nip, d.seller_foreign_tax_id) END AS tax_id,
                       (SELECT string_agg(DISTINCT o.source, '+' ORDER BY o.source)
                          FROM document_lineage l JOIN l1_document_observation o ON o.document_id = l.document_id
                         WHERE l.head_id = d.id) AS channels,
                       (d.evidence ? 'possible_duplicate') AS duplicate,
                       coalesce((d.evidence ->> 'needs_review')::boolean, false) AS review
                  FROM l1_document d
                 WHERE {' AND '.join(conditions)}
                 ORDER BY d.issue_date, d.direction, d.number_raw
                """,
                params,
            ).fetchall()
    for row in rows:
        flags = (" [możliwy duplikat]" if row["duplicate"] else "") + (" [do przeglądu]" if row["review"] else "")
        print(f"{str(row['id'])[:8]}  {row['issue_date']}  {row['direction']:<8} {row['kind']:<10} "
              f"{row['number_raw']:<20} {_money(row['gross_minor'], row['currency']):>18}  "
              f"{(row['party'] or '-')[:32]:<32} {row['tax_id'] or '-':<14} {row['channels'] or '-'}{flags}")
    print(f"Dokumentów: {len(rows)}")
    return 0


def _find_documents(conn: psycopg.Connection, ref: str, company: Company | None) -> list[dict[str, Any]]:
    scope = "AND company_id = %(company)s" if company else ""
    params: dict[str, Any] = {"company": company.id if company else None}
    with conn.cursor(row_factory=dict_row) as cur:
        looks_like_id = re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F-]{0,27})?", ref) is not None
        if looks_like_id:
            params["prefix"] = ref.lower() + "%"
            rows = cur.execute(f"SELECT * FROM l1_document WHERE id::text LIKE %(prefix)s {scope}", params).fetchall()
            if rows:
                return rows
        params["ksef"] = ref
        try:
            params["number"] = normalize_invoice_number(ref)
        except ValueError:
            params["number"] = None
        return cur.execute(
            f"""
            SELECT * FROM l1_document
             WHERE superseded_by IS NULL AND (ksef_number = %(ksef)s OR number_normalized = %(number)s) {scope}
             ORDER BY issue_date, direction
            """,
            params,
        ).fetchall()


def cmd_show(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        company = _company(conn, args.company) if args.company else None
        documents = _find_documents(conn, args.ref, company)
        if not documents:
            print(f"Nie znaleziono dokumentu {args.ref!r}.", file=sys.stderr)
            return 1
        for index, document in enumerate(documents):
            if index:
                print("-" * 72)
            _show_document(conn, document)
    return 0


def _show_document(conn: psycopg.Connection, d: dict[str, Any]) -> None:
    cur = conn.cursor(row_factory=dict_row)
    head = cur.execute("SELECT head_id FROM document_lineage WHERE document_id = %s", (d["id"],)).fetchone()
    print(f"Dokument {d['id']}" + ("" if d["superseded_by"] is None else f" (wersja zastąpiona -> {head['head_id']})"))
    print(f"  {d['kind']} / {d['direction']}  numer: {d['number_raw']} ({d['number_normalized']})"
          f"  KSeF: {d['ksef_number'] or '-'}")
    print(f"  wystawiona {d['issue_date']}, sprzedaż {d['sale_date'] or '-'}, okres "
          f"{d['service_period_start'] or '-'} - {d['service_period_end'] or '-'}, termin {d['due_date'] or 'brak'}")
    print(f"  sprzedawca: {d['seller_name']} ({d['seller_nip'] or d['seller_foreign_tax_id'] or '-'})")
    print(f"  nabywca:    {d['buyer_name']} ({d['buyer_nip'] or d['buyer_foreign_tax_id'] or '-'})")
    if d.get("counterparty_id"):
        party = cur.execute("SELECT name, nip, foreign_tax_id, kind FROM l1_counterparty WHERE id = %s",
                            (d["counterparty_id"],)).fetchone()
        if party:
            print(f"  kontrahent: {party['name']} [{party['kind']}] {party['nip'] or party['foreign_tax_id'] or ''}")
    print(f"  kwoty: netto {_money(d['net_minor'], d['currency'])}, VAT {_money(d['vat_minor'], d['currency'])}, "
          f"brutto {_money(d['gross_minor'], d['currency'])}"
          + (f", PLN {_money(d['gross_pln_minor'], 'PLN')} (kurs {d['fx_rate']} {d['fx_rate_source']})"
             if d["currency"] != "PLN" else ""))
    for rate in d["vat_summary"]:
        print(f"    stawka {rate.get('rate')}: netto {_money(rate.get('net_minor'), d['currency'])}"
              f", VAT {_money(rate.get('vat_minor'), d['currency'])}")
    flags = [name for name, on in (("MPP", d["mpp_required"]), ("metoda kasowa", d["cash_method"]),
                                   ("odwrotne obciążenie", d["reverse_charge"])) if on]
    print(f"  flagi: {', '.join(flags) or '-'};  rachunek do zapłaty: {d['payment_iban'] or '-'}")
    for label, column in (("koryguje", "corrects_document_id"), ("rozlicza zaliczkę", "settles_advance_document_id")):
        if d[column]:
            other = cur.execute("SELECT number_raw, ksef_number FROM l1_document WHERE id = %s", (d[column],)).fetchone()
            print(f"  {label}: {other['number_raw']} ({d[column]})")
    if d["corrects_ref"] and not d["corrects_document_id"]:
        print(f"  koryguje (jeszcze nie znaleziony): {d['corrects_ref']}")
    related = cur.execute(
        """
        SELECT DISTINCT r.number_raw, r.kind, r.id FROM l1_document r
          JOIN document_lineage l ON l.document_id IN (r.corrects_document_id, r.settles_advance_document_id)
         WHERE l.head_id = %s AND r.superseded_by IS NULL
        """,
        (head["head_id"],),
    ).fetchall()
    for r in related:
        print(f"  powiązany: {r['kind']} {r['number_raw']} ({r['id']})")
    evidence = d["evidence"] or {}
    if evidence.get("possible_duplicate"):
        print(f"  MOŻLIWY DUPLIKAT: {evidence['possible_duplicate']}")
    if evidence.get("needs_review"):
        print("  DO PRZEGLĄDU: pozycje nie sumują się do kwoty dokumentu")
    print(f"  pochodzenie: {d['source']} / {d['method']}"
          + (f" / prompt {d['prompt_version']}" if d["prompt_version"] else "")
          + f", parser {evidence.get('parser')}@{evidence.get('parser_version')}, zapisano {d['created_at']:%Y-%m-%d %H:%M}")
    versions = cur.execute(
        """
        SELECT v.id, v.source, v.created_at, v.superseded_by, v.evidence ->> 'reason' AS reason
          FROM document_lineage l JOIN l1_document v ON v.id = l.document_id
         WHERE l.head_id = %s ORDER BY v.created_at
        """,
        (head["head_id"],),
    ).fetchall()
    if len(versions) > 1:
        print("  wersje:")
        for v in versions:
            state = "bieżąca" if v["superseded_by"] is None else f"zastąpiona przez {str(v['superseded_by'])[:8]}"
            print(f"    {v['id']} {v['source']} {v['created_at']:%Y-%m-%d %H:%M} {state} {v['reason'] or ''}")
    observations = cur.execute(
        """
        SELECT o.source, o.linked_by, o.locator, o.method, o.prompt_version, o.created_at, r.external_ref,
               r.channel, r.sha256, r.meta ->> 'filename' AS filename
          FROM document_lineage l
          JOIN l1_document_observation o ON o.document_id = l.document_id
          JOIN l0_raw r ON r.id = o.raw_id
         WHERE l.head_id = %s ORDER BY o.created_at
        """,
        (head["head_id"],),
    ).fetchall()
    print(f"  obserwacje ({len(observations)}):")
    for o in observations:
        where = o["filename"] or o["external_ref"] or o["sha256"][:12]
        print(f"    {o['source']:<5} {o['linked_by']:<16} {where}{(' @ ' + o['locator']) if o['locator'] else ''}"
              f"  [{o['method']}{(' ' + o['prompt_version']) if o['prompt_version'] else ''}]")
    lines = cur.execute(
        "SELECT * FROM l1_document_line WHERE document_id = %s ORDER BY line_no", (d["id"],)
    ).fetchall()
    if lines:
        print(f"  pozycje ({len(lines)}):")
    for line in lines:
        kind = {"order": " [zamówienie]", "correction": " [korekta: po - przed]"}.get(line["line_kind"], "")
        print(f"    {line['line_no']}. {line['name']} | {_num(line['quantity'])} {line['unit'] or ''} x "
              f"{_num(line['unit_price'])} | netto {_money(line['net_minor'], d['currency'])} "
              f"| {line['vat_rate'] or '-'} | brutto {_money(line['gross_minor'], d['currency'])}{kind}")
    extraction = cur.execute(
        "SELECT model, prompt_version, lines_sum_ok, pseudonymized, needs_review FROM l1_pdf_extraction "
        "WHERE document_id IN (SELECT document_id FROM document_lineage WHERE head_id = %s)",
        (head["head_id"],),
    ).fetchall()
    for e in extraction:
        print(f"  odczyt PDF: model {e['model']} ({e['prompt_version']}), pozycje sumują się: {e['lines_sum_ok']}, "
              f"pseudonimizacja: {e['pseudonymized']}")
    suggestions = cur.execute(
        "SELECT kind, status, proposal FROM l2_suggestion WHERE subject_type = 'document' AND subject_id = %s",
        (d["id"],),
    ).fetchall()
    for s in suggestions:
        print(f"  sugestia {s['kind']} ({s['status']}): {s['proposal']}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    import getpass

    parser = sub.add_parser("documents", help="dokumenty: KSeF, import plików (JPK, CSV, XLSX), PDF z maili")
    actions = parser.add_subparsers(dest="documents_command", required=True, metavar="akcja")

    def company_arg(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument("--company", required=required, help="slug firmy (np. odm)")

    p = actions.add_parser("ksef-sync", help="pobierz faktury z KSeF (KASA_KSEF_PROVIDER=mock|ksef)")
    company_arg(p)
    p.add_argument("--from", dest="date_from", type=_date, help="od daty przyjęcia (domyślnie ostatnia znana)")
    p.add_argument("--to", dest="date_to", type=_date, help="do daty przyjęcia (domyślnie dziś)")
    p.add_argument("--direction", choices=("sales", "purchase"))
    p.add_argument("--no-check", action="store_true", help="bez kontroli kompletności po pobraniu")
    p.set_defaults(handler=cmd_ksef_sync)

    p = actions.add_parser("ksef-check", help="kontrola prawdy nr 4: kompletność KSeF w obie strony")
    company_arg(p)
    p.add_argument("--from", dest="date_from", type=_date, required=True)
    p.add_argument("--to", dest="date_to", type=_date, required=True)
    p.set_defaults(handler=cmd_ksef_check)

    p = actions.add_parser("import", help="kreator importu CSV/XLSX z mapowaniem z rozszerzenia")
    company_arg(p)
    p.add_argument("--mapping", required=True, help="klucz mapowania z rozszerzenia (np. document, pm_sheet)")
    p.add_argument("--map", action="append", metavar="POLE=NAGŁÓWEK",
                   help="wskaż kolumnę dla pola (zapisywane w profilu mapowania)")
    p.add_argument("--no-input", action="store_true", help="nie pytaj - brak kolumny to błąd")
    p.add_argument("--user", default=getpass.getuser(), help="kto zatwierdza mapowanie (do profilu)")
    p.add_argument("file")
    p.set_defaults(handler=cmd_import)

    p = actions.add_parser("import-jpk", help="import JPK_V7M/V7K lub JPK_FA")
    company_arg(p, required=False)
    p.add_argument("file")
    p.set_defaults(handler=cmd_import_jpk)

    p = actions.add_parser("import-pdf", help="odczyt PDF-ów z maili modelem (KASA_LLM_PROVIDER=mock|vertex)")
    company_arg(p)
    p.add_argument("--meta", help="index.json archiwum maili (nadawca, data, temat)")
    p.add_argument("paths", nargs="+", help="pliki PDF lub katalogi")
    p.set_defaults(handler=cmd_import_pdf)

    p = actions.add_parser("list", help="lista bieżących dokumentów")
    company_arg(p)
    p.add_argument("--direction", choices=("sales", "purchase"))
    p.add_argument("--month", help="RRRR-MM (data wystawienia)")
    p.set_defaults(handler=cmd_list)

    p = actions.add_parser("show", help="dokument z obserwacjami, pozycjami i pochodzeniem")
    company_arg(p, required=False)
    p.add_argument("ref", help="id (lub jego początek), numer KSeF albo numer faktury")
    p.set_defaults(handler=cmd_show)

