"""Building the queue (``queue_state``): which cards, in which order, and why (``rank_reason``, Polish).

Order (briefing, step 7): first what blocks the metrics and costs most when wrong, then the rest:

| class (priority)                         | card                                                        |
| ---------------------------------------- | ----------------------------------------------------------- |
| 900 kandydat na obiekt kosztowy           | blocks every allocation to the object (revenue + costs)     |
| 800 wypływ bez dokumentu / do wyjaśnienia | large outflow without a document first                      |
| 780 przeterminowana należność             | an inflow whose candidate is an overdue receivable          |
| 700 audyt automatu                        | 2% of the automation's decisions (marked "audyt")          |
| 650 dopasowanie                           | other match cards (ties included)                           |
| 600 alokacja                              | costs and revenues to cost objects                          |
| 550 słowo wrażliwe                        | a person must categorize (the model never read it)          |
| 500 duplikat                              | a possible duplicate document                               |
| 400 reguła gotowa do auto                 | one click saves future minutes                              |
| 300 kandydat na regułę                    | a business question                                         |

Within a class: the amount (``10 * log10(1 + PLN)``, at most +99). Deferred cards go to the end until
their date. A sentinel (known answer, hidden result) about every 50 cards.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kasa.core.matching.engine import money

SENTINEL_EVERY = 50
MIN_FOR_SENTINEL = 10
BADGE_PL = {"high": "wysoka", "medium": "średnia", "low": "niska"}


@dataclass
class Item:
    card_kind: str
    subject_type: str
    subject_id: UUID
    priority: float
    rank_reason: str
    suggestion_ids: list[UUID] = field(default_factory=list)
    badge: str | None = None
    amount_minor: int | None = None
    batch_key: str | None = None
    audit_sample_id: UUID | None = None
    sentinel_id: UUID | None = None

    @property
    def key(self) -> tuple[str, str, UUID]:
        return self.card_kind, self.subject_type, self.subject_id


@dataclass
class BuildReport:
    company: str
    items: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    created: int = 0
    updated: int = 0
    closed: int = 0
    sentinels_added: int = 0
    deferred: int = 0
    rules: str | None = None
    auto_categories: int = 0

    def summary(self) -> str:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(self.by_kind.items(), key=lambda kv: -kv[1]))
        lines = [f"Kolejka {self.company}: kart {self.items} ({kinds})",
                 f"  nowe {self.created}, odświeżone {self.updated}, zamknięte {self.closed}, odłożone {self.deferred}, "
                 f"wartownicy dodani {self.sentinels_added}"]
        if self.auto_categories:
            lines.append(f"  reguły auto nadały kategorie: {self.auto_categories}")
        if self.rules:
            lines.append("  " + self.rules.replace("\n", "\n  "))
        return "\n".join(lines)


def amount_score(amount_minor: int | None) -> float:
    if not amount_minor:
        return 0.0
    return min(99.0, 10 * math.log10(1 + abs(amount_minor) / 100))


def _company_id(company: Any) -> UUID:
    return company.id if hasattr(company, "id") else company


# ---------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------


def _open_by_subject(conn: psycopg.Connection, company_id: UUID, kind: str) -> dict[tuple[str, UUID], list[dict]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT id, company_id, kind::text AS kind, subject_type::text AS subject_type, subject_id, proposal,
                   score, rank, status::text AS status, evidence, source, created_at
              FROM l2_suggestion
             WHERE company_id = %s AND kind = %s AND status = 'open' AND subject_id IS NOT NULL
             ORDER BY subject_id, rank NULLS LAST, created_at
            """,
            (company_id, kind),
        ).fetchall()
    out: dict[tuple[str, UUID], list[dict]] = {}
    for row in rows:
        out.setdefault((row["subject_type"], row["subject_id"]), []).append(row)
    return out


def _overdue_receivable(conn: psycopg.Connection, proposals: list[dict]) -> tuple[str, int] | None:
    ids = [i["document_id"] for p in proposals for i in (p["proposal"] or {}).get("items") or [] if i.get("document_id")]
    if not ids:
        return None
    row = conn.execute(
        """
        SELECT number_raw, current_date - due_date FROM l1_document
         WHERE id = ANY(%s::uuid[]) AND direction = 'sales' AND due_date < current_date
         ORDER BY due_date LIMIT 1
        """,
        (ids,),
    ).fetchone()
    return (row[0], int(row[1])) if row else None


def _match_items(conn: psycopg.Connection, company_id: UUID) -> list[Item]:
    from kasa.core.decisions import batch_key

    decided = {r[0] for r in conn.execute(
        "SELECT transaction_id FROM l3_transaction_status WHERE company_id = %s AND status = 'active'",
        (company_id,)).fetchall()}
    items = []
    groups = _open_by_subject(conn, company_id, "match")
    for (subject_type, subject_id), proposals in groups.items():
        if subject_type != "transaction" or subject_id in decided:
            continue
        tx = conn.execute("SELECT amount_minor, counterparty_name_raw, title_raw FROM l1_bank_transaction WHERE id = %s",
                          (subject_id,)).fetchone()
        amount = int(tx[0])
        top = proposals[0]
        evidence = top["evidence"] or {}
        badge = evidence.get("badge")
        status = (top["proposal"] or {}).get("final_status")
        tie = "tie" in (evidence.get("tags") or []) or (evidence.get("detail") or {}).get("tie")
        who = tx[1] or "?"
        if tie:
            priority = 650 + amount_score(amount) + 5
            reason = f"remis - niejednoznaczne: {money(abs(amount))}, {who}; zdecyduj, którą fakturę"
        elif amount < 0 and status in ("no_document", "to_clarify"):
            big = abs(amount) >= 100_000
            priority = 800 + amount_score(amount)
            label = "duży wypływ" if big else "wypływ"
            reason = (f"{label} bez dokumentu: {money(abs(amount))} ({who}) - blokuje koszty bez dokumentu"
                      if status == "no_document" else
                      f"{label} do wyjaśnienia: {money(abs(amount))} ({who}) - nie wiadomo, za co")
        elif amount > 0 and (overdue := _overdue_receivable(conn, proposals)):
            priority = 780 + amount_score(amount)
            reason = (f"przeterminowana należność bez dopasowania: {overdue[0]} ({overdue[1]} dni po terminie), "
                      f"wpływ {money(amount)}")
        else:
            priority = 650 + amount_score(amount)
            reason = (f"dopasowanie przelewu {money(abs(amount))} ({who}), odznaka "
                      f"{BADGE_PL.get(badge, badge or '-')}")
        items.append(Item("match", subject_type, subject_id, round(priority, 3), reason,
                          [p["id"] for p in proposals], badge, abs(amount), batch_key(conn, top)))
    # every proposal of a transfer rejected by the person: the card stays with "Inne…"
    rows = conn.execute(
        """
        SELECT DISTINCT s.subject_id, t.amount_minor, t.counterparty_name_raw FROM l2_suggestion s
          JOIN l1_bank_transaction t ON t.id = s.subject_id
         WHERE s.company_id = %s AND s.kind = 'match' AND s.status = 'rejected' AND s.resolved_by_decision IS NULL
           AND NOT EXISTS (SELECT 1 FROM l2_suggestion o WHERE o.subject_id = s.subject_id AND o.kind = 'match'
                            AND o.status = 'open')
           AND NOT EXISTS (SELECT 1 FROM l3_transaction_status x WHERE x.transaction_id = s.subject_id
                            AND x.status = 'active')
        """,
        (company_id,),
    ).fetchall()
    for subject_id, amount, who in rows:
        items.append(Item("match", "transaction", subject_id, round(640 + amount_score(amount), 3),
                          f"odrzucone wszystkie propozycje: {money(abs(int(amount)))} ({who or '?'}) - „Inne…”",
                          [], "low", abs(int(amount))))
    return items


def _allocation_items(conn: psycopg.Connection, company_id: UUID) -> list[Item]:
    from kasa.core.decisions import batch_key

    statuses = {r[0] for r in conn.execute(
        "SELECT transaction_id FROM l3_transaction_status WHERE company_id = %s AND status = 'active'",
        (company_id,)).fetchall()}
    items = []
    for (subject_type, subject_id), proposals in _open_by_subject(conn, company_id, "allocation").items():
        top = proposals[0]
        evidence = top["evidence"] or {}
        if subject_type == "transaction" and "awaiting_matching" in (evidence.get("tags") or []) \
                and subject_id not in statuses:
            continue            # first the matching decides whether the transfer has a document
        shares = (top["proposal"] or {}).get("shares") or []
        amount = sum(abs(int(s.get("amount_minor") or 0)) for s in shares)
        names = ", ".join(str(s.get("cost_object_name") or "?") for s in shares)
        unassigned = len(shares) == 1 and str(shares[0].get("cost_object_name")) == "UNASSIGNED"
        priority = (560 if unassigned else 600) + amount_score(amount)
        reason = (f"alokacja {money(amount)}: {names}" + (" (koszt ogólny?)" if unassigned else "")
                  + (" - propozycja częściowa, uzupełnij" if evidence.get("partial") else ""))
        items.append(Item("allocation", subject_type, subject_id, round(priority, 3), reason,
                          [p["id"] for p in proposals], evidence.get("badge"), amount, batch_key(conn, top)))
    return items


def _category_items(conn: psycopg.Connection, company_id: UUID) -> list[Item]:
    items = []
    for (subject_type, subject_id), proposals in _open_by_subject(conn, company_id, "category").items():
        sensitive = [p for p in proposals if (p["proposal"] or {}).get("needs_human")]
        amount = 0
        if subject_type == "transaction":
            amount = abs(int(conn.execute("SELECT amount_minor FROM l1_bank_transaction WHERE id = %s",
                                          (subject_id,)).fetchone()[0]))
        if sensitive:
            items.append(Item("sensitive", subject_type, subject_id, round(550 + amount_score(amount), 3),
                              f"słowo wrażliwe - kategorię nadaje człowiek (model tego nie czytał), {money(amount)}",
                              [p["id"] for p in sensitive], "low", amount))
        rest = [p for p in proposals if p not in sensitive]
        if rest:
            items.append(Item("category", subject_type, subject_id, round(540 + amount_score(amount), 3),
                              f"kategoria: {(rest[0]['proposal'] or {}).get('category')}",
                              [p["id"] for p in rest], (rest[0]["evidence"] or {}).get("badge"), amount))
    return items


def _simple_items(conn: psycopg.Connection, company_id: UUID) -> list[Item]:
    items = []
    for (subject_type, subject_id), proposals in _open_by_subject(conn, company_id, "duplicate").items():
        number = (proposals[0]["proposal"] or {}).get("number")
        items.append(Item("duplicate", subject_type, subject_id, 500.0, f"możliwy duplikat dokumentu {number}",
                          [p["id"] for p in proposals], "medium"))
    for (subject_type, subject_id), proposals in _open_by_subject(conn, company_id, "cost_object_candidate").items():
        p = proposals[0]["proposal"] or {}
        amount = int(p.get("revenue_minor") or 0) + int(p.get("cost_minor") or 0)
        members = len(p.get("members") or []) + 1
        items.append(Item("cost_object_candidate", subject_type, subject_id, round(900 + amount_score(amount), 3),
                          f"kandydat na obiekt kosztowy blokuje alokacje: {p.get('name')}, {members} rekordów, "
                          f"przychód {money(int(p.get('revenue_minor') or 0))}, koszt "
                          f"{money(int(p.get('cost_minor') or 0))}",
                          [proposals[0]["id"]], (proposals[0]["evidence"] or {}).get("badge"), amount))
    for (subject_type, subject_id), proposals in _open_by_subject(conn, company_id, "rule_candidate").items():
        p = proposals[0]["proposal"] or {}
        items.append(Item("rule_candidate", subject_type, subject_id, 300.0 + min(50, int(p.get("hits") or 0)),
                          f"kandydat na regułę: {p.get('hits')} z {p.get('total')} - {p.get('question')}",
                          [proposals[0]["id"]], (proposals[0]["evidence"] or {}).get("badge")))
    for rule_id, rule_key, agreed, observations, wilson, question in conn.execute(
        """
        SELECT rule_id, rule_key, agreed, observations, wilson_lower, question FROM rule_stage_effective
         WHERE company_id = %s AND ready_for_auto AND stored_stage <> 'auto'
        """,
        (company_id,),
    ).fetchall():
        items.append(Item("rule_ready", "rule", rule_id, 400.0,
                          f"reguła {rule_key} gotowa do auto: {agreed} zgodnych z {observations}, dolna granica "
                          f"{float(wilson):.3f} - {question or ''}".strip(" -"), [], "high"))
    for sample_id, _decision, subject_type, subject_id, amount, month, rule_key in conn.execute(
        """
        SELECT s.id, s.decision_id, s.subject_type::text, s.subject_id, s.amount_minor, s.month, r.rule_key
          FROM l2_audit_sample s LEFT JOIN l3_rule r ON r.id = s.rule_id
         WHERE s.company_id = %s AND s.verdict IS NULL
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = s.decision_id)
        """,
        (company_id,),
    ).fetchall():
        items.append(Item("audit", subject_type, subject_id, round(700 + amount_score(amount), 3),
                          f"audyt automatu ({month}): decyzja reguły {rule_key}, {money(int(amount or 0))} - "
                          "potwierdź albo oznacz błąd", [], None, int(amount or 0), audit_sample_id=sample_id))
    return items


def collect(conn: psycopg.Connection, company_id: UUID) -> list[Item]:
    items = (_match_items(conn, company_id) + _allocation_items(conn, company_id) + _category_items(conn, company_id)
             + _simple_items(conn, company_id))
    keys: dict[tuple, Item] = {}
    for item in items:
        keys.setdefault(item.key, item)
    return list(keys.values())


# ---------------------------------------------------------------------------
# sentinels
# ---------------------------------------------------------------------------


def normalize_answer(proposal: dict[str, Any]) -> dict[str, Any]:
    """A match answer (proposal or label) in a comparable shape."""
    status = proposal.get("final_status") or proposal.get("status")
    subtype = proposal.get("subtype") if status == "no_document" else None
    subtype = {"interest": "fee"}.get(subtype, subtype)
    targets = set()
    for item in proposal.get("items") or []:
        for key, kind in (("document_id", "document"), ("internal_record_id", "internal_record"),
                          ("related_transaction_id", "transaction")):
            if item.get(key):
                targets.add(f"{kind}:{item[key]}")
    for doc in proposal.get("documents") or []:
        targets.add(f"{doc['type']}:{doc['id']}")
    return {"final_status": status, "subtype": subtype, "targets": sorted(targets) if status == "matched" else []}


def _sentinel_pool(conn: psycopg.Connection, company_id: UUID, busy: set[UUID]) -> list[dict[str, Any]]:
    """Subjects with a known answer and a shown proposal set: decisions of a person, then control-set labels."""
    used = {r[0] for r in conn.execute("SELECT subject_id FROM queue_sentinel WHERE company_id = %s",
                                       (company_id,)).fetchall()}
    pool = []
    for decision_id, subject_id, payload in conn.execute(
        """
        SELECT d.id, d.subject_id, d.payload FROM l3_decision_log d
         WHERE d.company_id = %s AND d.kind = 'match' AND d.method = 'human' AND d.subject_type = 'transaction'
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
         ORDER BY d.decided_at
        """,
        (company_id,),
    ).fetchall():
        pool.append({"subject_id": subject_id, "expected": normalize_answer(payload or {}),
                     "source": "decision", "ref": decision_id})
    for item_id, subject_id, label in conn.execute(
        """
        SELECT i.id, i.subject_id, i.label FROM control_set_item i JOIN control_set c ON c.id = i.set_id
         WHERE i.company_id = %s AND c.kind = 'transaction' AND i.label IS NOT NULL AND i.subject_type = 'transaction'
         ORDER BY i.set_id, i.order_no
        """,
        (company_id,),
    ).fetchall():
        pool.append({"subject_id": subject_id, "expected": normalize_answer(label or {}),
                     "source": "control_set", "ref": item_id})
    out, seen = [], set()
    for entry in pool:
        subject = entry["subject_id"]
        if subject in busy or subject in used or subject in seen:
            continue
        shown = conn.execute(
            """
            SELECT array_agg(id ORDER BY rank) FROM (
                SELECT DISTINCT ON (rank) id, rank FROM l2_suggestion
                 WHERE company_id = %s AND kind = 'match' AND subject_id = %s AND rank IS NOT NULL
                   AND source <> 'queue.other'
                 ORDER BY rank, created_at DESC) s
            """,
            (company_id, subject),
        ).fetchone()[0]
        if not shown:
            continue
        seen.add(subject)
        out.append({**entry, "suggestion_ids": list(shown)})
    return out


def _sentinel_positions(n: int, k: int) -> list[int]:
    return [max(1, round((i + 1) * n / (k + 1))) for i in range(k)]


def _add_sentinels(conn: psycopg.Connection, company_id: UUID, regular: int, busy: set[UUID]) -> list[Item]:
    if regular < MIN_FOR_SENTINEL:
        return []
    wanted = max(1, round(regular / SENTINEL_EVERY))
    waiting = conn.execute(
        "SELECT count(*) FROM queue_state WHERE company_id = %s AND card_kind = 'sentinel' AND closed_at IS NULL",
        (company_id,),
    ).fetchone()[0]
    missing = wanted - waiting
    if missing <= 0:
        return []
    pool = _sentinel_pool(conn, company_id, busy)
    if not pool:
        return []
    rng = random.Random(f"{company_id}:{regular}:{waiting}")
    chosen = rng.sample(pool, min(missing, len(pool)))
    items = []
    for entry in chosen:
        sentinel_id = conn.execute(
            """
            INSERT INTO queue_sentinel (company_id, subject_type, subject_id, suggestion_id, suggestion_ids, expected,
                                        expected_source, source_ref)
            VALUES (%s, 'transaction', %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (company_id, entry["subject_id"], entry["suggestion_ids"][0], entry["suggestion_ids"],
             Jsonb(entry["expected"]), entry["source"], entry["ref"]),
        ).fetchone()[0]
        amount = conn.execute("SELECT amount_minor FROM l1_bank_transaction WHERE id = %s",
                              (entry["subject_id"],)).fetchone()[0]
        badge = conn.execute("SELECT evidence ->> 'badge' FROM l2_suggestion WHERE id = %s",
                             (entry["suggestion_ids"][0],)).fetchone()[0]
        # the reason reads like any match card: the person must not know it is a sentinel
        items.append(Item("sentinel", "transaction", entry["subject_id"], 0.0,
                          f"dopasowanie przelewu {money(abs(int(amount)))}, odznaka {BADGE_PL.get(badge, '-')}",
                          entry["suggestion_ids"], badge, abs(int(amount)), sentinel_id=sentinel_id))
    return items


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _upsert(conn: psycopg.Connection, company_id: UUID, item: Item) -> str:
    row = conn.execute(
        """
        UPDATE queue_state
           SET suggestion_ids = %s, rank_reason = %s, priority = %s, badge = %s, amount_minor = %s, batch_key = %s,
               audit_sample_id = coalesce(%s, audit_sample_id), built_at = clock_timestamp()
         WHERE company_id = %s AND card_kind = %s AND subject_type = %s AND subject_id = %s AND closed_at IS NULL
        RETURNING id
        """,
        (item.suggestion_ids, item.rank_reason, item.priority, item.badge, item.amount_minor, item.batch_key,
         item.audit_sample_id, company_id, item.card_kind, item.subject_type, item.subject_id),
    ).fetchone()
    if row:
        return "updated"
    conn.execute(
        """
        INSERT INTO queue_state (company_id, card_kind, subject_type, subject_id, suggestion_ids, audit_sample_id,
                                 sentinel_id, rank_reason, priority, badge, amount_minor, batch_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (company_id, item.card_kind, item.subject_type, item.subject_id, item.suggestion_ids, item.audit_sample_id,
         item.sentinel_id, item.rank_reason, item.priority, item.badge, item.amount_minor, item.batch_key),
    )
    return "created"


def _reorder(conn: psycopg.Connection, company_id: UUID) -> int:
    """Positions: regular cards by priority, sentinels spread evenly (~ every 50), deferred cards last."""
    rows = conn.execute(
        """
        SELECT id, card_kind, (deferred_until IS NOT NULL AND deferred_until > now()) AS deferred FROM queue_state
         WHERE company_id = %s AND closed_at IS NULL
         ORDER BY priority DESC, amount_minor DESC NULLS LAST, created_at, id
        """,
        (company_id,),
    ).fetchall()
    regular = [r[0] for r in rows if r[1] != "sentinel" and not r[2]]
    sentinels = [r[0] for r in rows if r[1] == "sentinel" and not r[2]]
    deferred = [r[0] for r in rows if r[2]]
    ordered = list(regular)
    for offset, (sentinel, position) in enumerate(zip(sentinels, _sentinel_positions(len(regular), len(sentinels)),
                                                      strict=False)):
        ordered.insert(min(len(ordered), position + offset), sentinel)
    ordered += deferred
    with conn.cursor() as cur:
        cur.executemany("UPDATE queue_state SET position = %s WHERE id = %s",
                        [(n, item_id) for n, item_id in enumerate(ordered, start=1)])
    return len(deferred)


def build(conn: psycopg.Connection, company: Any, *, mine_rules: bool = True, min_months: int = 2,
          auto_rules: bool = True, sentinels: bool = True, created_by: str = "kasa.queue") -> BuildReport:
    """Rebuild the queue of a company (idempotent; cards keep their first shown time and deferral)."""
    company_id = _company_id(company)
    slug = getattr(company, "slug", str(company_id))
    report = BuildReport(slug)
    if auto_rules:
        from kasa.core.rules.auto import apply_category_rules

        report.auto_categories = apply_category_rules(conn, company_id).applied
    if mine_rules:
        from kasa.core.rules.mine import mine

        report.rules = mine(conn, company_id, min_months=min_months, created_by=created_by).summary()
    items = collect(conn, company_id)
    wanted = {item.key for item in items}
    for item in items:
        if _upsert(conn, company_id, item) == "created":
            report.created += 1
        else:
            report.updated += 1
    # cards whose decision happened elsewhere (CLI, automation, another card) leave the queue
    stale = conn.execute(
        """
        SELECT id, card_kind, subject_type::text, subject_id FROM queue_state
         WHERE company_id = %s AND closed_at IS NULL AND card_kind <> 'sentinel'
        """,
        (company_id,),
    ).fetchall()
    for item_id, card_kind, subject_type, subject_id in stale:
        if (card_kind, subject_type, subject_id) not in wanted:
            conn.execute("UPDATE queue_state SET closed_at = clock_timestamp(), outcome = 'resolved_elsewhere' "
                         "WHERE id = %s", (item_id,))
            report.closed += 1
    if sentinels:
        busy = {i.subject_id for i in items}
        regular = conn.execute(
            """
            SELECT count(*) FROM queue_state WHERE company_id = %s AND closed_at IS NULL AND card_kind <> 'sentinel'
               AND (deferred_until IS NULL OR deferred_until <= now())
            """,
            (company_id,),
        ).fetchone()[0]
        for sentinel in _add_sentinels(conn, company_id, regular, busy):
            _upsert(conn, company_id, sentinel)
            report.sentinels_added += 1
    report.deferred = _reorder(conn, company_id)
    for kind, count in conn.execute(
        "SELECT card_kind, count(*) FROM queue_state WHERE company_id = %s AND closed_at IS NULL GROUP BY 1",
        (company_id,),
    ).fetchall():
        report.by_kind[kind] = count
    report.items = sum(report.by_kind.values())
    return report


def today() -> date:
    return date.today()
