"""One semantics pass over a company: titles, lines, internal rows (``python -m kasa semantics run``).

* Idempotent: a subject read once per (prompt version, model) is skipped - a second
  pass makes 0 calls. A subject sent to a person for a sensitive word is skipped too.
* ``dry_run=True`` (``semantics estimate`` / ``run --estimate``) counts what would be
  sent and estimates the cost - no call, no write (briefing, step 1: "szacunek kosztu
  przed startem").
* Cost: characters (prompt + answer) x ``KASA_LLM_PRICE_PER_1K`` (price of 1000
  characters, default 0). The estimate assumes an answer of ``ANSWER_CHARS`` characters.
* Each subject is written in its own transaction (or savepoint), so an interrupted
  first run keeps what was already paid for.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kasa.core.providers.llm import LlmProvider, get_llm_provider
from kasa.core.pseudonym import PseudonymizationError, load_context
from kasa.core.semantics import lines, titles
from kasa.core.semantics.gate import terms_for, estimate_cost, model_name, price_per_1k
from kasa.core.semantics.prompts import PROMPTS, build_prompt
from kasa.core.semantics.store import flagged_subjects, read_subjects
from kasa.core.sensitive import find_sensitive

log = logging.getLogger(__name__)

SCOPES = ("transactions", "lines", "internal", "all")
ANSWER_CHARS = 400          # assumed answer length in an estimate


@dataclass
class ScopeCounts:
    subjects: int = 0
    already_read: int = 0
    to_call: int = 0            # dry run: subjects that would go to the model
    calls: int = 0
    code_only: int = 0
    blocked: Counter = field(default_factory=Counter)


@dataclass
class SemanticsReport:
    scope: str
    dry_run: bool
    provider: str
    leaves_machine: bool
    price_per_1k: Decimal
    by_scope: dict[str, ScopeCounts] = field(default_factory=dict)
    chars: int = 0              # characters sent and received (estimate: would be)
    call_ids: list[UUID] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return sum(c.calls for c in self.by_scope.values())

    @property
    def blocked(self) -> Counter:
        total: Counter = Counter()
        for counts in self.by_scope.values():
            total.update(counts.blocked)
        return total

    @property
    def cost_estimate(self) -> Decimal:
        return estimate_cost(self.chars, self.price_per_1k)

    def summary(self) -> str:
        head = "Szacunek (bez wywołań)" if self.dry_run else "Semantyka"
        out = [f"{head}: dostawca {self.provider}"
               + ("" if self.leaves_machine else " (lokalny - tekst nie opuszcza maszyny)")]
        names = {"transactions": "tytuły przelewów", "lines": "pozycje dokumentów", "internal": "wiersze arkuszy"}
        for scope, c in self.by_scope.items():
            blocked = ", ".join(f"{reason}: {n}" for reason, n in sorted(c.blocked.items())) or "0"
            if self.dry_run:
                out.append(f"  {names[scope]}: {c.subjects} (przeczytane wcześniej {c.already_read}, do modelu "
                           f"{c.to_call}, bez wywołania {c.code_only}, zablokowane {blocked})")
            else:
                out.append(f"  {names[scope]}: {c.subjects} (przeczytane wcześniej {c.already_read}, wywołania "
                           f"{c.calls}, bez wywołania {c.code_only}, zablokowane {blocked})")
        out.append(f"  znaki: {self.chars}, stawka {self.price_per_1k} za 1000 znaków -> koszt "
                   f"{'szacowany' if self.dry_run else 'szac.'} {self.cost_estimate}")
        return "\n".join(out)


def _rows(conn: psycopg.Connection, query: str, params: tuple) -> Iterator[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        yield from cur.execute(query, params).fetchall()


def transactions(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    return list(_rows(conn, """
        SELECT id, title_raw, counterparty_name_raw, amount_minor, op_type::text AS op_type, sensitive, extracted
          FROM l1_bank_transaction
         WHERE company_id = %s AND superseded_by IS NULL AND status = 'booked'
         ORDER BY booking_date, entry_reference, id
    """, (company_id,)))


def document_lines(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    return list(_rows(conn, """
        SELECT l.id, l.name, l.line_kind, d.direction::text AS direction
          FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id
         WHERE d.company_id = %s AND d.superseded_by IS NULL
         ORDER BY d.issue_date, d.id, l.line_no
    """, (company_id,)))


def internal_records(conn: psycopg.Connection, company_id: UUID) -> list[dict[str, Any]]:
    return list(_rows(conn, """
        SELECT id, fields, mapping_key, evidence
          FROM l1_internal_record
         WHERE company_id = %s AND superseded_by IS NULL
         ORDER BY file_raw_id, row_no
    """, (company_id,)))


def _estimate(purpose: str, text: str, fields: dict[str, str | None], facts: dict[str, Any]) -> int:
    envelope = {"purpose": purpose, "prompt_version": PROMPTS[purpose],
                "input": {"untrusted_text": text, **{k: {"untrusted_text": v} for k, v in fields.items() if v}},
                "facts": facts}
    return len(build_prompt(purpose, envelope)) + ANSWER_CHARS


def run_semantics(conn: psycopg.Connection, company: Any, scope: str = "all", *, dry_run: bool = False,
                  provider: LlmProvider | None = None) -> SemanticsReport:
    """Read every not-yet-read title/line/row of a company (or only estimate it)."""
    if scope not in SCOPES:
        raise ValueError(f"zakres {scope!r} - dozwolone: {', '.join(SCOPES)}")
    provider = provider or get_llm_provider()
    report = SemanticsReport(scope, dry_run, provider.name, bool(getattr(provider, "leaves_machine", True)),
                             price_per_1k())
    terms = terms_for(conn, company)
    try:
        context = load_context(conn, company.id)
    except PseudonymizationError as exc:
        log.warning("Kontekst pseudonimizacji niedostępny (%s) - każde wywołanie zostanie zablokowane.", exc)
        context = None
    scopes = ("transactions", "lines", "internal") if scope == "all" else (scope,)
    for name in scopes:
        counts = report.by_scope.setdefault(name, ScopeCounts())
        if name == "transactions":
            _pass(conn, company, report, counts, "transaction", titles.PURPOSE, transactions(conn, company.id),
                  provider, context, terms, dry_run)
        elif name == "lines":
            _pass(conn, company, report, counts, "line", lines.PURPOSE, document_lines(conn, company.id),
                  provider, context, terms, dry_run)
        else:
            _pass(conn, company, report, counts, "internal_record", lines.PURPOSE,
                  internal_records(conn, company.id), provider, context, terms, dry_run)
    if report.call_ids and not dry_run:
        row = conn.execute(
            "SELECT coalesce(sum(input_chars + coalesce(output_chars, 0)) FILTER (WHERE status <> 'blocked'), 0) "
            "FROM llm_call_log WHERE id = ANY(%s)", (report.call_ids,),
        ).fetchone()
        report.chars = int(row[0])
    return report


def _pass(conn: psycopg.Connection, company: Any, report: SemanticsReport, counts: ScopeCounts, subject_type: str,
          purpose: str, rows: list[dict[str, Any]], provider: LlmProvider, context: Any, terms: Any,
          dry_run: bool) -> None:
    done = read_subjects(conn, company.id, subject_type, purpose, PROMPTS[purpose], model_name(provider, purpose))
    flagged = flagged_subjects(conn, company.id, subject_type)
    for row in rows:
        counts.subjects += 1
        if row["id"] in done or row["id"] in flagged:
            counts.already_read += 1
            continue
        if dry_run:
            _dry(company, report, counts, subject_type, purpose, row, context, terms)
            continue
        with conn.transaction():
            if subject_type == "transaction":
                outcome = titles.read_title(conn, company, row, provider=provider, pseudonym_context=context,
                                            terms=terms)
            elif subject_type == "line":
                outcome = lines.read_line(conn, company, row, provider=provider, pseudonym_context=context,
                                          terms=terms)
            else:
                outcome = lines.read_internal_record(conn, company, row, provider=provider,
                                                     pseudonym_context=context, terms=terms)
        if outcome.call_id is not None:
            report.call_ids.append(outcome.call_id)
        if outcome.status == "read":
            counts.calls += 1
        elif outcome.status == "code":
            counts.code_only += 1
        elif outcome.status == "done":
            counts.already_read += 1
        elif outcome.blocked is not None:
            counts.blocked[outcome.blocked.reason] += 1


def _dry(company: Any, report: SemanticsReport, counts: ScopeCounts, subject_type: str, purpose: str,
         row: dict[str, Any], context: Any, terms: Any) -> None:
    """What a real pass would do with one subject - computed without the gate, writes nothing."""
    if subject_type == "transaction":
        if row.get("sensitive"):
            counts.blocked["sensitive"] += 1
            return
        text = row.get("title_raw") or ""
        fields = {"counterparty": row.get("counterparty_name_raw")}
        facts = {"direction": "in" if (row.get("amount_minor") or 0) > 0 else "out", "op_type": row.get("op_type")}
        if titles.only_codes(text, context):
            counts.code_only += 1
            return
    elif subject_type == "line":
        text, fields, facts = row.get("name") or "", {}, {"source": "document_line"}
    else:
        text, fields, facts = lines.internal_record_text(company, row), {}, {"source": "internal_record"}
    if not text.strip():
        counts.code_only += 1
        return
    if find_sensitive(" \n ".join([text, *(v for v in fields.values() if v)]), terms):
        counts.blocked["sensitive"] += 1
        return
    counts.to_call += 1
    report.chars += _estimate(purpose, text, fields, facts)
