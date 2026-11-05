"""``python -m kasa metrics|retention ...`` and the export for the office (``report export --format``).

* ``metrics list | show <key> | get <key> [--period 2026-10] [--as-of 2026-10-31] [--dim cost_object]
  [--filter cost_object=Gala] [--json] | sync | lock | golden [--write [--force]] | proposals`` - the metric
  layer (the same registry as the agent and the dashboard); ``golden`` compares (or records) the value of
  every metric on the fixtures after the clicks (``kasa/core/metrics/golden.lock.json``);
* ``retention run [--now ...] | status | anonymize --company odm --before 2025-11-01 [--shred]`` - retention
  of model texts (90 days) and anonymization of persons (contract + 1 year);
* ``report export --company odm --month 2026-10 --format csv|xlsx --out <katalog> [--by artur]`` - the full
  hand-over for the accounting office, logged in ``export_log`` (without ``--format`` the command keeps its
  phase 4 behaviour: one CSV to standard output or to the file ``--out``).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date, datetime
from pathlib import Path

from kasa.core.companies import get_company
from kasa.core.db import connect, transaction
from kasa.core.metrics import registry
from kasa.core.metrics.base import DefinitionChanged, MetricError, UnknownMetric


def _company_id(conn, slug: str):
    try:
        return get_company(conn, slug).id
    except LookupError:
        raise SystemExit(f"Nie ma firmy {slug!r} (uruchom: python -m kasa init-db)") from None


def _filters(values: list[str] | None) -> dict[str, str]:
    out = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"Filtr {item!r}: oczekiwano klucz=wartość")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def cmd_metrics_list(args: argparse.Namespace) -> int:
    for key in registry.keys():
        cls = registry.latest(key)
        print(f"{cls.ref():28} {cls.unit:10} {cls.grain:12} {cls.name_pl}")
    return 0


def cmd_metrics_show(args: argparse.Namespace) -> int:
    try:
        info = registry.describe(registry.get(args.key, args.version))
    except UnknownMetric:
        raise SystemExit(f"Nie ma metryki {args.key!r}") from None
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_metrics_get(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        registry.sync_definitions(conn)
        try:
            result = registry.compute(conn, _company_id(conn, args.company), args.key, version=args.version,
                                      period=args.period, as_of=args.as_of, dimensions=args.dim or [],
                                      filters=_filters(args.filter)).as_dict()
        except (UnknownMetric, MetricError, DefinitionChanged) as exc:
            print(f"Odmowa: {exc}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    state = result["data_state"]
    print(f"{result['metric']['name_pl']} ({result['metric']['ref']}): {result.get('value_pl')}")
    print(f"  okres: {(result.get('period') or {}).get('label') or result['as_of']}; dane na dzień {state['as_of']}; "
          f"w sugestiach {result['unconfirmed_share_pl']}; kontrole prawdy "
          f"{'zielone' if state['truth_checks_green'] else 'nie zielone'}")
    for row in result["values"][:30]:
        print("  - " + json.dumps(row, ensure_ascii=False, default=str))
    for warning in result["warnings"]:
        print(f"  uwaga: {warning}")
    return 0


def cmd_metrics_sync(args: argparse.Namespace) -> int:
    with transaction(url=args.database_url) as conn:
        try:
            report = registry.sync_definitions(conn)
        except DefinitionChanged as exc:
            print(f"Błąd: {exc}", file=sys.stderr)
            return 1
    print(f"Definicje metryk: nowe {len(report['added'])}, bez zmian {len(report['unchanged'])}, wycofane "
          f"{len(report['deprecated'])}"
          + (f", nowy skrót (schemat 2, kod bez zmian) {len(report['rehashed'])}" if report.get("rehashed") else ""))
    for ref in report["added"]:
        print(f"  + {ref}")
    for ref in report["deprecated"]:
        print(f"  - {ref} (deprecated: jest nowsza wersja)")
    return 0


def cmd_metrics_lock(args: argparse.Namespace) -> int:
    try:
        entries = registry.write_lock()
    except DefinitionChanged as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    print(f"Zapisano {registry.LOCK_FILE} ({len(entries)} definicji)")
    return 0


def _fixtures_database() -> str:
    """The clicked fixtures database of the tests (pipeline of phase 4 + the person's clicks), built from zero."""
    try:
        from tests.metrics.helpers import clicked_database
    except ImportError:
        raise SystemExit("Złoty wynik liczy się na bazie z fixtures - uruchom z katalogu repozytorium "
                         "(potrzebne fixtures/ i tests/), albo podaj --url bazy po kliknięciach") from None
    return clicked_database()


def cmd_metrics_golden(args: argparse.Namespace) -> int:
    from kasa.core.metrics import golden

    url = args.url or _fixtures_database()
    with connect(url) as conn:
        with conn.transaction(force_rollback=True):
            registry.sync_definitions(conn, refresh_views=False)
            current = golden.golden_values(conn, _company_id(conn, args.company))
    if not args.write:
        problems = golden.compare(current, golden.read_golden())
        for problem in problems:
            print(f"  {problem}")
        print("Złoty wynik: zgodny." if not problems else f"Złoty wynik: różnice {len(problems)} "
              "(python -m kasa metrics golden --write po świadomej zmianie wersji)")
        return 0 if not problems else 1
    try:
        report = golden.write_golden(current, force=args.force)
    except DefinitionChanged as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    print(f"Zapisano {golden.GOLDEN_FILE} ({len(current)} metryk; nowe {len(report['added'])}"
          + (f", nadpisane --force {len(report['changed'])}" if report["changed"] else "") + ")")
    return 0


def cmd_metrics_proposals(args: argparse.Namespace) -> int:
    with connect(args.database_url) as conn:
        rows = conn.execute(
            "SELECT created_at, proposed_by, requested_key, question, status FROM metric_proposal p "
            "JOIN company c ON c.id = p.company_id WHERE c.slug = %s ORDER BY created_at DESC LIMIT %s",
            (args.company, args.limit),
        ).fetchall()
    if not rows:
        print("Brak propozycji metryk.")
    for created, who, key, question, status in rows:
        print(f"{created:%Y-%m-%d %H:%M} [{status}] {who}: {key or '-'} - {question}")
    return 0


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


def cmd_retention_run(args: argparse.Namespace) -> int:
    from kasa.core.retention import overdue_payloads, run_retention

    now = datetime.fromisoformat(args.now) if args.now else None
    with transaction(url=args.database_url) as conn:
        if args.dry_run:
            print("Próba (nic nie zmieniono): " + json.dumps(overdue_payloads(conn, now=now)))
            return 0
        done = run_retention(conn, now=now)
    print(f"Retencja: wygaszono {done['llm_call_payload']} promptów/odpowiedzi modelu i {done['l1_semantics_input']} "
          "wejść modelu w l1_semantics (skróty SHA-256 i długości zostają)")
    return 0


def cmd_retention_status(args: argparse.Namespace) -> int:
    from kasa.core.retention import POLICY, overdue_payloads

    with connect(args.database_url) as conn:
        overdue = overdue_payloads(conn)
        logs = conn.execute("SELECT count(*), max(anonymized_at) FROM anonymization_log").fetchone()
    print(f"Polityka: pliki źródłowe {POLICY.raw_files}; rekordy umowa + {POLICY.records_years_after_contract} rok, "
          f"potem anonimizacja osób; prompty i odpowiedzi modelu {POLICY.model_prompts_days} dni; log dostępu "
          f"{POLICY.access_log_years} lata")
    print(f"Po terminie: {overdue['llm_call_payload']} promptów/odpowiedzi, {overdue['l1_semantics_input']} wejść "
          f"l1_semantics" + (" - uruchom: python -m kasa retention run" if any(overdue.values()) else ""))
    print(f"Anonimizacje osób: {logs[0]}" + (f", ostatnia {logs[1]:%Y-%m-%d}" if logs[1] else ""))
    return 1 if any(overdue.values()) else 0


def cmd_retention_anonymize(args: argparse.Namespace) -> int:
    from kasa.core.retention import anonymize_persons, anonymization_log

    before = date.fromisoformat(args.before)
    with transaction(url=args.database_url) as conn:
        company_id = _company_id(conn, args.company)
        log_id = anonymize_persons(conn, company_id, before, actor=args.by or getpass.getuser(),
                                   shred_keys=args.shred)
        entry = next((e for e in anonymization_log(conn, company_id) if e["id"] == log_id), None)
    if log_id is None:
        print("Nic do anonimizacji (rekordy sprzed tej daty są już w formie pseudonimowej).")
        return 0
    print(f"Anonimizacja osób w rekordach sprzed {before} (anonymization_log {log_id}): "
          + ", ".join(f"{k} {v}" for k, v in (entry or {}).get("counts", {}).items()))
    return 0


# ---------------------------------------------------------------------------
# report export (extends the phase 4 command)
# ---------------------------------------------------------------------------


def cmd_report_export(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else None
    if not args.format and not (out is not None and (out.is_dir() or str(args.out).endswith(("/", "\\")))):
        from kasa.cli.allocation import cmd_report_export as legacy

        return legacy(args)
    if out is None:
        raise SystemExit("Podaj --out <katalog> (eksport dla biura zapisuje pliki i loguje udostępnienie)")
    from kasa.core.reports.export import write_office_export

    with transaction(url=args.database_url) as conn:
        result = write_office_export(conn, _company_id(conn, args.company), args.month, fmt=args.format or "csv",
                                     out_dir=out, exported_by=args.by or getpass.getuser(),
                                     recipient=args.recipient)
    print(result.summary())
    for f in result.files:
        print(f"  {out / f['name']}")
    return 0


def _extend_report_export(sub: argparse._SubParsersAction) -> None:
    report = sub.choices.get("report")
    if report is None:                       # the phase 4 command is missing: register a fresh one
        report = sub.add_parser("report", help="raport firmy i eksport dla biura")
        report.add_subparsers(dest="report_command", required=True, metavar="akcja")
    actions = [a for a in report._actions if isinstance(a, argparse._SubParsersAction)]
    export = actions[0].choices.get("export")
    if export is None:
        export = actions[0].add_parser("export", help="eksport dla biura")
        export.add_argument("--company", default="odm")
        export.add_argument("--month", required=True, help="RRRR-MM")
        export.add_argument("--out")
    export.description = ("Bez --format: CSV dokumentów na wyjście albo do pliku --out (faza 4). Z --format: pełny "
                          "eksport dla biura do katalogu --out (dokumenty z dowodem decyzji, przelewy bez dokumentu, "
                          "obiekty), każde udostępnienie w export_log.")
    export.add_argument("--format", choices=["csv", "xlsx"], help="pełny eksport dla biura (do katalogu --out)")
    export.add_argument("--by", help="kto udostępnia (domyślnie użytkownik systemu)")
    export.add_argument("--recipient", default="biuro rachunkowe", help="komu (do logu)")
    export.set_defaults(handler=cmd_report_export)


def register(sub: argparse._SubParsersAction) -> None:
    mp = sub.add_parser("metrics", help="warstwa metryk: lista, definicja, wartość, synchronizacja wersji")
    m_sub = mp.add_subparsers(dest="metrics_command", required=True, metavar="akcja")
    m_sub.add_parser("list", help="metryki z wersjami").set_defaults(handler=cmd_metrics_list)
    p = m_sub.add_parser("show", help="definicja metryki (pułapki, wymiary, filtry, skrót kodu)")
    p.add_argument("key")
    p.add_argument("--version", type=int)
    p.set_defaults(handler=cmd_metrics_show)
    p = m_sub.add_parser("get", help="wartość metryki (ten sam rejestr co agent i dashboard)")
    p.add_argument("key")
    p.add_argument("--company", default="odm")
    p.add_argument("--version", type=int)
    p.add_argument("--period", help="2026-10 | 2026 | 2026-10-01..2026-10-31")
    p.add_argument("--as-of", dest="as_of", help="RRRR-MM-DD")
    p.add_argument("--dim", action="append", help="wymiar (powtarzalny)")
    p.add_argument("--filter", action="append", help="filtr klucz=wartość (powtarzalny)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_metrics_get)
    m_sub.add_parser("sync", help="zapisz brakujące definicje w bazie (zmiana bez nowej wersji = błąd)").set_defaults(
        handler=cmd_metrics_sync)
    m_sub.add_parser("lock", help="zapisz skróty kodu definicji (po dodaniu NOWEJ wersji)").set_defaults(
        handler=cmd_metrics_lock)
    p = m_sub.add_parser("golden", help="złoty wynik: wartość każdej metryki na fixtures po kliknięciach "
                                         "(bez --write: porównaj)")
    p.add_argument("--write", action="store_true", help="zapisz golden.lock.json (po świadomej zmianie wersji)")
    p.add_argument("--force", action="store_true", help="z --write: nadpisz też zmienione wyniki zapisanych wersji "
                                                       "(tylko po świadomej zmianie danych fixtures)")
    p.add_argument("--url", help="gotowa baza z fixtures po kliknięciach (domyślnie budowana od zera)")
    p.add_argument("--company", default="odm", help="firma fixtures (domyślnie odm)")
    p.set_defaults(handler=cmd_metrics_golden)
    p = m_sub.add_parser("proposals", help="propozycje metryk (pytania spoza warstwy metryk)")
    p.add_argument("--company", default="odm")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(handler=cmd_metrics_proposals)

    rt = sub.add_parser("retention", help="retencja: teksty modelu 90 dni, anonimizacja osób")
    r_sub = rt.add_subparsers(dest="retention_command", required=True, metavar="akcja")
    p = r_sub.add_parser("run", help="wygaś prompty/odpowiedzi i wejścia modelu po 90 dniach")
    p.add_argument("--now", help="moment odniesienia (ISO), domyślnie teraz")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(handler=cmd_retention_run)
    r_sub.add_parser("status", help="co jest po terminie (kod 1 = coś czeka)").set_defaults(handler=cmd_retention_status)
    p = r_sub.add_parser("anonymize", help="anonimizacja osób w rekordach sprzed daty (umowa + 1 rok)")
    p.add_argument("--company", default="odm")
    p.add_argument("--before", required=True, help="RRRR-MM-DD")
    p.add_argument("--shred", action="store_true", help="zniszcz też klucze pseudonimów osób sprzed tej daty")
    p.add_argument("--by")
    p.set_defaults(handler=cmd_retention_anonymize)

    _extend_report_export(sub)
