"""Health checks behind ``python -m kasa check``.

Phase 1 checks the skeleton: connection, migrations, extensions, companies,
the guards that enforce the four shelves, and provenance columns. Truth checks
(balances, match coverage, allocations, KSeF completeness, MPP) join later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from kasa.core import migrate
from kasa.core.db import connect
from kasa.core.extension import Extension, ExtensionError, discover_extensions

L0_TABLES = ("l0_raw",)
L1_TABLES = (
    "l1_document",
    "l1_document_line",
    "l1_document_observation",
    "l1_bank_account",
    "l1_bank_transaction",
    "l1_fx_rate",
    "l1_title_semantics",
    "l1_internal_record",
)
L2_TABLES = ("l2_suggestion",)
L3_TABLES = (
    "l3_rule",
    "l3_decision_log",
    "l3_cost_object",
    "l3_match",
    "l3_allocation",
    "l3_category_decision",
    "l3_transaction_status",
)
SUBJECT_TABLES = ("l2_suggestion", "l3_decision_log", "l3_allocation", "l3_category_decision")

PROVENANCE_COLUMNS: dict[str, str] = {
    "company_id": "uuid",
    "source": "text",
    "method": "provenance_method",
    "confidence": "numeric",
    "evidence": "jsonb",
    "schema_version": "integer",
    "prompt_version": "text",
    "created_at": "timestamp with time zone",
    "created_by": "text",
}


def expected_triggers() -> set[tuple[str, str]]:
    """(table, trigger) pairs that must exist and be enabled."""
    expected = {("l0_raw", "l0_raw_append_only"), ("l0_raw", "l0_raw_no_truncate")}
    for table in (*L1_TABLES, *L2_TABLES):
        expected |= {(table, f"{table}_no_delete"), (table, f"{table}_no_truncate")}
    for table in L3_TABLES:
        expected |= {(table, f"{table}_guard"), (table, f"{table}_no_truncate")}
    for table in SUBJECT_TABLES:
        expected.add((table, f"{table}_subject_guard"))
    expected |= {
        ("l3_decision_log", "l3_decision_log_append_only"),
        ("l3_allocation", "l3_allocation_target_guard"),
        ("company", "company_unassigned"),
    }
    return expected


def missing_triggers(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.relname, t.tgname
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE NOT t.tgisinternal AND t.tgenabled <> 'D'
        """
    ).fetchall()
    present = {(r[0], r[1]) for r in rows}
    return sorted(f"{table}.{trigger}" for table, trigger in expected_triggers() - present)


def provenance_gaps(conn: psycopg.Connection) -> list[str]:
    """Provenance columns missing (or of a wrong type) on L1-L3 tables."""
    rows = conn.execute(
        """
        SELECT table_name, column_name,
               CASE WHEN data_type = 'USER-DEFINED' THEN udt_name ELSE data_type END
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name ~ '^l[123]_'
        """
    ).fetchall()
    columns: dict[str, dict[str, str]] = {}
    for table, column, data_type in rows:
        columns.setdefault(table, {})[column] = data_type
    gaps = []
    for table in sorted(columns):
        for column, data_type in PROVENANCE_COLUMNS.items():
            actual = columns[table].get(column)
            if actual != data_type:
                gaps.append(f"{table}.{column} ({actual or 'brak'}, oczekiwano {data_type})")
    return gaps


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def run_checks(url: str | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []

    def record(name: str, fn: Callable[[], str]) -> bool:
        try:
            detail = fn()
        except (psycopg.Error, migrate.MigrationError, ExtensionError, AssertionError, LookupError) as exc:
            results.append(CheckResult(name, False, str(exc).strip()))
            return False
        results.append(CheckResult(name, True, detail))
        return True

    extensions: dict[str, Extension] = {}

    def check_extensions() -> str:
        extensions.update(discover_extensions())
        return ", ".join(f"{e.slug} ({e.cost_object_kind})" for e in extensions.values()) or "brak rozszerzeń"

    record("Rozszerzenia", check_extensions)

    try:
        conn = connect(url, autocommit=True)
    except psycopg.Error as exc:
        results.append(CheckResult("Połączenie z bazą", False, str(exc).strip()))
        return results

    with conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        results.append(CheckResult("Połączenie z bazą", True, f"PostgreSQL {version}"))

        def check_migrations() -> str:
            done = migrate.applied(conn)
            pending = migrate.verify(migrate.discover(), done)
            assert not pending, "oczekujące migracje: " + ", ".join(m.label for m in pending)
            return f"zastosowane: {len(done)}, oczekujące: 0"

        if not record("Migracje", check_migrations):
            return results

        def check_guards() -> str:
            missing = missing_triggers(conn)
            assert not missing, "brak lub wyłączone: " + ", ".join(missing)
            return f"{len(expected_triggers())} triggerów aktywnych"

        record("Strażnicy półek", check_guards)

        def check_provenance() -> str:
            gaps = provenance_gaps(conn)
            assert not gaps, "braki: " + ", ".join(gaps)
            return "wszystkie tabele L1-L3 mają kolumny pochodzenia"

        record("Pochodzenie", check_provenance)

        def check_companies() -> str:
            rows = conn.execute(
                """
                SELECT c.slug, c.extension_slug,
                       (SELECT count(*) FROM l3_cost_object co
                         WHERE co.company_id = c.id AND co.system_code = 'UNASSIGNED') AS unassigned
                  FROM company c ORDER BY c.slug
                """
            ).fetchall()
            problems = []
            for slug, extension_slug, unassigned in rows:
                if extensions and extension_slug not in extensions:
                    problems.append(f"{slug}: brak rozszerzenia {extension_slug!r}")
                if unassigned != 1:
                    problems.append(f"{slug}: UNASSIGNED x{unassigned}")
            assert not problems, "; ".join(problems)
            if not rows:
                return "brak firm (uruchom: python -m kasa init-db)"
            return ", ".join(row[0] for row in rows) + " - każda ma UNASSIGNED"

        record("Firmy", check_companies)

        for name, check in TRUTH_CHECKS:      # defined at the end of this module
            record(name, lambda check=check: check(conn))

    return results


# ---------------------------------------------------------------------------
# Truth check 4 (briefing, "Kontrola prawdy"): KSeF completeness.
# The number and the sum of invoices in kasa equal the KSeF list, both ways.
# ---------------------------------------------------------------------------

KSEF_COMPLETENESS_CHECK = "ksef_completeness"


@dataclass(frozen=True)
class KsefCompletenessResult:
    ok: bool
    period_start: date
    period_end: date
    ksef_count: int
    kasa_count: int
    ksef_totals: dict[str, dict[str, int]]          # "sales/PLN" -> {"count", "gross_minor"}
    kasa_totals: dict[str, dict[str, int]]
    missing_in_kasa: list[str]                     # on the KSeF list, not among our documents
    missing_in_ksef: list[str]                     # ours (with a KSeF number), not on the KSeF list
    mismatches: list[dict[str, Any]]               # same number, different amount/currency/direction
    recorded: bool = False

    def detail(self) -> dict[str, Any]:
        return {
            "ksef_count": self.ksef_count,
            "kasa_count": self.kasa_count,
            "ksef_totals": self.ksef_totals,
            "kasa_totals": self.kasa_totals,
            "missing_in_kasa": self.missing_in_kasa,
            "missing_in_ksef": self.missing_in_ksef,
            "mismatches": self.mismatches,
        }

    def summary(self) -> str:
        if self.ok:
            from kasa.core.normalize import from_minor

            totals = ", ".join(f"{k}: {v['count']} szt. / {from_minor(v['gross_minor'], k.split('/')[1])}"
                               for k, v in sorted(self.kasa_totals.items()))
            return f"kompletna: {self.kasa_count} faktur ({totals})"
        return (f"NIEKOMPLETNA: w KSeF {self.ksef_count}, u nas {self.kasa_count}; brakuje u nas "
                f"{len(self.missing_in_kasa)}, brak w KSeF {len(self.missing_in_ksef)}, "
                f"różne kwoty {len(self.mismatches)}")


def check_ksef_completeness(conn: psycopg.Connection, company: Any, provider: Any, date_from: date,
                            date_to: date, *, record: bool = True) -> KsefCompletenessResult:
    """Compare current documents having a KSeF number with ``provider.list_invoice_numbers``.

    The window is the date of acceptance by KSeF (Polish time; for documents without
    ``ksef_accepted_at`` the date encoded in the KSeF number). Counts and sums are
    compared per direction and currency, and number by number in both directions.
    With ``record`` the result goes to ``truth_check_result`` when that table exists.
    """
    refs = provider.list_invoice_numbers(company, date_from, date_to)
    theirs = {r.ksef_number: r for r in refs}
    rows = conn.execute(
        """
        SELECT ksef_number, direction::text, currency, gross_minor
          FROM l1_document
         WHERE company_id = %s AND superseded_by IS NULL AND ksef_number IS NOT NULL
           AND coalesce((ksef_accepted_at AT TIME ZONE 'Europe/Warsaw')::date,
                        to_date(substr(ksef_number, 12, 8), 'YYYYMMDD')) BETWEEN %s AND %s
        """,
        (company.id, date_from, date_to),
    ).fetchall()
    ours = {row[0]: row for row in rows}

    def totals(items: list[tuple[str, str, int]]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for direction, currency, gross in items:
            bucket = out.setdefault(f"{direction}/{currency}", {"count": 0, "gross_minor": 0})
            bucket["count"] += 1
            bucket["gross_minor"] += gross
        return out

    ksef_totals = totals([(r.direction, r.currency, r.gross_minor) for r in refs])
    kasa_totals = totals([(r[1], r[2], r[3]) for r in rows])
    mismatches = []
    for number in sorted(set(theirs) & set(ours)):
        ref, row = theirs[number], ours[number]
        if (ref.direction, ref.currency, ref.gross_minor) != (row[1], row[2], row[3]):
            mismatches.append({"ksef_number": number,
                               "ksef": {"direction": ref.direction, "currency": ref.currency,
                                        "gross_minor": ref.gross_minor},
                               "kasa": {"direction": row[1], "currency": row[2], "gross_minor": row[3]}})
    missing_in_kasa = sorted(set(theirs) - set(ours))
    missing_in_ksef = sorted(set(ours) - set(theirs))
    ok = not missing_in_kasa and not missing_in_ksef and not mismatches and ksef_totals == kasa_totals
    result = KsefCompletenessResult(ok, date_from, date_to, len(refs), len(rows), ksef_totals, kasa_totals,
                                    missing_in_kasa, missing_in_ksef, mismatches)
    if record and conn.execute("SELECT to_regclass('truth_check_result') IS NOT NULL").fetchone()[0]:
        from psycopg.types.json import Jsonb

        conn.execute(
            """
            INSERT INTO truth_check_result (company_id, check_name, scope, period_start, period_end, ok, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (company.id, KSEF_COMPLETENESS_CHECK, f"ksef:{getattr(provider, 'name', '?')}", date_from, date_to,
             ok, Jsonb(result.detail())),
        )
        result = KsefCompletenessResult(**{**result.__dict__, "recorded": True})
    return result


# ---------------------------------------------------------------------------
# Truth checks 1 and 5 (briefing, "Kontrola prawdy"): balance continuity and MPP.
# Results go to truth_check_result (one row per run, append-only).
# ---------------------------------------------------------------------------

BALANCE_CONTINUITY_CHECK = "balance_continuity"
MPP_CHECK = "mpp_vat"

# Phase 2 tables with the same guards as the phase 1 L1 tables.
L1_TABLES = (*L1_TABLES, "l1_counterparty", "l1_bank_balance")


@dataclass(frozen=True)
class TruthCheck:
    company_id: Any
    check_name: str
    scope: str | None
    period_start: date
    period_end: date
    ok: bool
    detail: dict[str, Any]

    def summary(self) -> str:
        d = self.detail
        if self.check_name == BALANCE_CONTINUITY_CHECK:
            if d.get("error") or d.get("note"):
                return f"{self.scope}: {d.get('error') or d.get('note')}"
            text = (f"{self.scope}: otwarcie {d.get('opening_minor', 0) / 100:.2f} + operacje "
                    f"{d.get('booked_sum_minor', 0) / 100:.2f} = {d.get('closing_minor', 0) / 100:.2f}")
            if d.get("closing_reported_minor") is not None:
                text += f" (bank: {d['closing_reported_minor'] / 100:.2f})"
            if d.get("mismatches"):
                text += f"; niezgodności: {len(d['mismatches'])}"
            return text
        if self.check_name == MPP_CHECK:
            return (f"bloki /VAT/ we wpływach {d['vat_blocks_minor'] / 100:.2f}, wpływy na rachunki VAT "
                    f"{d['vat_account_credits_minor'] / 100:.2f}"
                    + ("" if self.ok else f", różnica {d['difference_minor'] / 100:.2f}"))
        return str(d)


def record_truth_check(conn: psycopg.Connection, result: TruthCheck) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        """
        INSERT INTO truth_check_result (company_id, check_name, scope, period_start, period_end, ok, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (result.company_id, result.check_name, result.scope, result.period_start, result.period_end, result.ok,
         Jsonb(result.detail)),
    )


def _day_chain(opening: int, rows: list[tuple]) -> tuple[int, list[dict[str, Any]], int]:
    """Walk one day's booked operations from the opening balance through their balances after.

    The order inside a day is rebuilt from the balances themselves (the next operation
    is the one whose balance before equals the running balance), so the check does not
    depend on the order rows were stored in. Returns (closing, problems, bank balances checked).
    """
    remaining = list(rows)
    running = opening
    problems: list[dict[str, Any]] = []
    checked = 0
    while remaining:
        nxt = next((r for r in remaining if r[3] is not None and not r[4] and r[3] - r[2] == running), None) \
            or next((r for r in remaining if r[3] is not None and r[3] - r[2] == running), None) \
            or next((r for r in remaining if r[3] is None), None)
        if nxt is None:
            problems.append({"kind": "chain_break", "running_minor": running,
                             "candidates": [{"id": str(r[0]), "ref": r[1], "amount_minor": r[2],
                                             "balance_after_minor": r[3]} for r in remaining]})
            running += sum(r[2] for r in remaining)
            break
        remaining.remove(nxt)
        running += nxt[2]
        if nxt[3] is not None and not nxt[4]:
            checked += 1
    return running, problems, checked


def check_balance_continuity(conn: psycopg.Connection, company_id: Any, period_start: date, period_end: date, *,
                             record: bool = True) -> list[TruthCheck]:
    """Truth check 1, per account and day, to the grosz: opening + booked operations = closing.

    Opening: the bank's opening balance (OPBD) of the first day, else its closing
    balance (CLBD) of the day before, else derived from the first balance after given
    by the bank. Each day is checked against the balances after operations (given by
    the bank or computed) and against every reported OPBD/CLBD; the period closing
    against the bank's CLBD. Without any independent balance the check is red.
    """
    from datetime import timedelta

    accounts = conn.execute(
        "SELECT id, iban, currency FROM l1_bank_account WHERE company_id = %s ORDER BY iban", (company_id,)
    ).fetchall()
    results = []
    for account_id, iban, currency in accounts:
        snapshots = conn.execute(
            """
            SELECT balance_type, reference_date, amount_minor, source FROM l1_bank_balance
             WHERE account_id = %s AND superseded_by IS NULL AND balance_type IN ('OPBD', 'PRCD', 'CLBD')
               AND reference_date BETWEEN %s AND %s
            """,
            (account_id, period_start - timedelta(days=1), period_end + timedelta(days=1)),
        ).fetchall()
        rows = conn.execute(
            """
            SELECT id, entry_reference, amount_minor, balance_after_minor, balance_after_computed, booking_date
              FROM l1_bank_transaction
             WHERE account_id = %s AND superseded_by IS NULL AND status = 'booked'
               AND booking_date BETWEEN %s AND %s
             ORDER BY booking_date, created_at, id
            """,
            (account_id, period_start, period_end),
        ).fetchall()
        pending = int(conn.execute(
            """
            SELECT coalesce(sum(amount_minor), 0) FROM l1_bank_transaction
             WHERE account_id = %s AND superseded_by IS NULL AND status = 'pending'
            """,
            (account_id,),
        ).fetchone()[0])
        if not rows and not snapshots:
            continue
        # OPBD = balance at the start of its day; CLBD and PRCD (previous closing) = at its end
        opening_at = {d: a for t, d, a, _s in snapshots if t == "OPBD"}
        closing_at = {d: a for t, d, a, _s in snapshots if t in ("CLBD", "PRCD")}
        by_day: dict[date, list[tuple]] = {}
        for row in rows:
            by_day.setdefault(row[5], []).append(row)

        opening = opening_at.get(period_start)
        opening_source = "OPBD"
        if opening is None and (period_start - timedelta(days=1)) in closing_at:
            opening, opening_source = closing_at[period_start - timedelta(days=1)], "CLBD of the day before"
        if opening is None:
            # the last closing balance before the period, carried forward by the operations in between
            earlier = conn.execute(
                """
                SELECT b.reference_date, b.amount_minor + coalesce((
                       SELECT sum(t.amount_minor) FROM l1_bank_transaction t
                        WHERE t.account_id = b.account_id AND t.superseded_by IS NULL AND t.status = 'booked'
                          AND t.booking_date > b.reference_date AND t.booking_date < %s), 0)
                  FROM l1_bank_balance b
                 WHERE b.account_id = %s AND b.superseded_by IS NULL AND b.balance_type IN ('CLBD', 'PRCD')
                   AND b.reference_date < %s
                 ORDER BY b.reference_date DESC LIMIT 1
                """,
                (period_start, account_id, period_start),
            ).fetchone()
            if earlier is not None:
                opening = int(earlier[1])
                opening_source = f"CLBD/PRCD of {earlier[0].isoformat()} + operations until the period"
        if opening is None and rows:
            first_day = by_day[min(by_day)]
            afters = {r[3] for r in first_day if r[3] is not None}
            starts = [r for r in first_day if r[3] is not None and not r[4] and (r[3] - r[2]) not in afters]
            if starts:
                opening, opening_source = starts[0][3] - starts[0][2], "derived from the bank's balance after"
        detail: dict[str, Any] = {"iban": iban, "currency": currency.strip(), "booked_count": len(rows)}
        if opening is None and not rows:
            detail["note"] = "brak operacji w okresie"
            results.append(TruthCheck(company_id, BALANCE_CONTINUITY_CHECK, iban, period_start, period_end,
                                      True, detail))
            continue
        if opening is None:
            detail["error"] = "brak salda otwarcia (OPBD/CLBD ani salda po operacji z banku)"
            results.append(TruthCheck(company_id, BALANCE_CONTINUITY_CHECK, iban, period_start, period_end,
                                      False, detail))
            continue

        mismatches: list[dict[str, Any]] = []
        references = 0
        running = opening
        day = period_start
        while day <= period_end:
            if day in opening_at and opening_at[day] != running and not (day == period_start
                                                                         and opening_source == "OPBD"):
                mismatches.append({"kind": "opening", "date": day.isoformat(), "expected_minor": running,
                                   "reported_minor": opening_at[day]})
            if day in opening_at:
                references += 1
            if day in by_day:
                running, problems, checked = _day_chain(running, by_day[day])
                references += checked
                for p in problems:
                    p["date"] = day.isoformat()
                mismatches.extend(problems)
            if day in closing_at:
                references += 1
                if closing_at[day] != running:
                    mismatches.append({"kind": "closing", "date": day.isoformat(), "expected_minor": running,
                                       "reported_minor": closing_at[day]})
            day += timedelta(days=1)
        if period_end + timedelta(days=1) in opening_at:
            references += 1
            if opening_at[period_end + timedelta(days=1)] != running:
                mismatches.append({"kind": "opening_next_day", "expected_minor": running,
                                   "reported_minor": opening_at[period_end + timedelta(days=1)]})
        reported = closing_at.get(period_end)
        detail.update({
            "opening_minor": opening,
            "opening_source": opening_source,
            "booked_sum_minor": sum(r[2] for r in rows),
            "credits_minor": sum(r[2] for r in rows if r[2] > 0),
            "debits_minor": sum(r[2] for r in rows if r[2] < 0),
            "closing_minor": running,
            "closing_reported_minor": reported,
            "pending_sum_minor": pending,
            "available_minor": running + pending,
            "days_with_operations": len(by_day),
            "independent_references": references,
            "mismatches": mismatches,
        })
        if references == 0 and rows:
            detail["error"] = "brak niezależnego salda z banku - nie da się potwierdzić ciągłości"
        ok = not mismatches and (references > 0 or not rows)    # no operations: the balance simply carries
        results.append(TruthCheck(company_id, BALANCE_CONTINUITY_CHECK, iban, period_start, period_end, ok, detail))
    if record:
        for result in results:
            record_truth_check(conn, result)
    return results


def check_mpp(conn: psycopg.Connection, company_id: Any, period_start: date, period_end: date, *,
              record: bool = True) -> TruthCheck:
    """Truth check 5: the /VAT/ blocks of incoming split payments = credits on the VAT accounts.

    Blocks are summed on current (non-VAT) accounts only - the VAT leg repeats the title.
    Own transfers into a VAT account are not split payments and are left out.
    """
    vat_blocks, blocks_count = conn.execute(
        """
        SELECT coalesce(sum((t.extracted -> 'mpp' ->> 'vat_minor')::bigint), 0), count(*)
          FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked' AND a.kind <> 'vat'
           AND t.amount_minor > 0 AND t.extracted -> 'mpp' ->> 'vat_minor' IS NOT NULL
           AND t.booking_date BETWEEN %s AND %s
        """,
        (company_id, period_start, period_end),
    ).fetchone()
    credits, credits_count, own_credits = conn.execute(
        """
        SELECT coalesce(sum(t.amount_minor) FILTER (WHERE t.op_type <> 'own'), 0),
               count(*) FILTER (WHERE t.op_type <> 'own'),
               coalesce(sum(t.amount_minor) FILTER (WHERE t.op_type = 'own'), 0)
          FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked' AND a.kind = 'vat'
           AND t.amount_minor > 0 AND t.booking_date BETWEEN %s AND %s
        """,
        (company_id, period_start, period_end),
    ).fetchone()
    ungrouped = conn.execute(
        """
        SELECT t.entry_reference FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked' AND t.mpp_group_id IS NULL
           AND t.booking_date BETWEEN %s AND %s
           AND ((a.kind <> 'vat' AND t.extracted -> 'mpp' ->> 'vat_minor' IS NOT NULL)
                OR (a.kind = 'vat' AND t.op_type = 'vat'))
         ORDER BY t.booking_date, t.entry_reference
        """,
        (company_id, period_start, period_end),
    ).fetchall()
    out_blocks, vat_debits = conn.execute(
        """
        SELECT coalesce(sum((t.extracted -> 'mpp' ->> 'vat_minor')::bigint)
                        FILTER (WHERE a.kind <> 'vat' AND t.amount_minor < 0), 0),
               coalesce(sum(t.amount_minor) FILTER (WHERE a.kind = 'vat' AND t.amount_minor < 0
                                                    AND t.op_type = 'vat'), 0)
          FROM l1_bank_transaction t JOIN l1_bank_account a ON a.id = t.account_id
         WHERE t.company_id = %s AND t.superseded_by IS NULL AND t.status = 'booked'
           AND t.booking_date BETWEEN %s AND %s
        """,
        (company_id, period_start, period_end),
    ).fetchone()
    detail = {
        "vat_blocks_minor": int(vat_blocks),
        "incoming_split_payments": blocks_count,
        "vat_account_credits_minor": int(credits),
        "vat_account_credits_count": credits_count,
        "own_transfers_into_vat_minor": int(own_credits),
        "difference_minor": int(vat_blocks) - int(credits),
        "outgoing_vat_blocks_minor": int(out_blocks),
        "vat_account_split_debits_minor": int(vat_debits),
        "legs_without_group": [r[0] for r in ungrouped],
    }
    result = TruthCheck(company_id, MPP_CHECK, None, period_start, period_end, int(vat_blocks) == int(credits),
                        detail)
    if record:
        record_truth_check(conn, result)
    return result


def run_truth_checks(conn: psycopg.Connection, company_id: Any, period_start: date, period_end: date, *,
                     record: bool = True) -> list[TruthCheck]:
    """Truth checks of the bank channel for one company and period (1: balances, 5: MPP)."""
    results = check_balance_continuity(conn, company_id, period_start, period_end, record=record)
    if results or conn.execute(
        "SELECT EXISTS (SELECT 1 FROM l1_bank_transaction WHERE company_id = %s)", (company_id,)
    ).fetchone()[0]:
        results.append(check_mpp(conn, company_id, period_start, period_end, record=record))
    return results


def _bank_months(conn: psycopg.Connection) -> list[tuple[Any, str, date, date]]:
    return conn.execute(
        """
        SELECT DISTINCT t.company_id, c.slug, date_trunc('month', t.booking_date)::date AS month_start,
               (date_trunc('month', t.booking_date) + interval '1 month - 1 day')::date AS month_end
          FROM l1_bank_transaction t JOIN company c ON c.id = t.company_id
         WHERE t.status = 'booked' AND t.superseded_by IS NULL
         ORDER BY c.slug, month_start
        """
    ).fetchall()


def _bank_truth_check(check: Callable[..., Any]) -> Callable[[psycopg.Connection], str]:
    def run(conn: psycopg.Connection) -> str:
        months = _bank_months(conn)
        if not months:
            return "brak operacji bankowych"
        failed: list[str] = []
        passed = 0
        for company_id, slug, start, end in months:
            outcome = check(conn, company_id, start, end)
            for item in outcome if isinstance(outcome, list) else [outcome]:
                if item.ok:
                    passed += 1
                else:
                    failed.append(f"{slug} {start:%Y-%m} {item.summary()}")
        assert not failed, "; ".join(failed)
        return f"zielone: {passed} (firmy x miesiące x rachunki)"

    return run


# (name shown by ``python -m kasa check``, check) - run_checks runs every entry.
TRUTH_CHECKS: list[tuple[str, Callable[[psycopg.Connection], str]]] = [
    ("Kontrola prawdy 1: ciągłość sald", _bank_truth_check(check_balance_continuity)),
    ("Kontrola prawdy 5: MPP", _bank_truth_check(check_mpp)),
]
