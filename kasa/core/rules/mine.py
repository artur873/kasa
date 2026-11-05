"""Mining rule candidates from a person's decisions and hard identifiers (briefing, step 6).

Evidence is ONLY:
* decisions of a person in force (``method = 'human'``, not revoked) - categories, "no document"
  subtypes, allocations, matches;
* for match strategies also matches that rest on a hard identifier in the title (KSeF number, MPP block,
  invoice number) - the identifier is the evidence, not the automation's decision: a decision of the
  automation counts ONLY when the identifiers of the title decide the group by themselves (every number of
  the title names exactly one document, and those are exactly the documents of the decision -
  ``title_resolves``; review of 4C/5, point 3); otherwise it is skipped;
never a decision of the automation alone (the system must not confirm itself).

Patterns (each per canonical counterparty, ``kasa.core.rules.facts``):

| family          | evidence                                    | action                                                   |
| --------------- | ------------------------------------------- | -------------------------------------------------------- |
| ``category``    | category decisions (sender/IBAN -> category) | ``{"kind": "category", "category": C}``                 |
| ``no_document`` | final status no_document with a subtype     | ``{"kind": "final_status", "final_status": "no_document", "subtype": S}`` |
| ``allocation``  | allocations 100% to one cost object         | ``{"kind": "allocation", "cost_object_id": X, "share": 1}`` |
| ``monthly_bulk``| one transfer for several invoices           | ``{"kind": "match_strategy", "strategy": "monthly_bulk"}`` |
| ``payer_alias`` | a payer's IBAN + a word of the title = client X (Revolut) | ``{"kind": "match_strategy", "strategy": "payer_alias", "counterparty_id": X}`` |

A candidate needs >= 5 cases (``min_cases``) in >= 2 months (``min_months``; the CLI allows
``--min-months 1`` for the one month of test data) and a majority of the counterparty's decisions.
The candidate is an ``l3_rule`` row (stage ``candidate``, method ``code``) plus a card
``l2_suggestion`` (``kind = rule_candidate``) with the business question, "X z Y u N kontrahentów",
3 examples and ALL exceptions. Absence of cases never creates a negative rule; a rejected candidate
(an explicit "no") is never proposed again.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kasa.core.matching.engine import money
from kasa.core.rules.facts import canonical_counterparty, counterparty_name, month_of, subject_facts
from kasa.core.sensitive import fold

SOURCE = "kasa.rules.mine"
MIN_CASES = 5
MIN_MONTHS = 2
HARD_TAGS = ("ksef_number", "mpp_block", "invoice_number")
SUBTYPE_LABELS = {
    "zus": "ZUS", "tax": "podatek (US)", "fee": "prowizja / opłata", "interest": "odsetki", "advance": "zaliczka",
    "deposit": "kaucja", "card_awaiting": "karta - czeka na fakturę", "salary": "wynagrodzenie",
    "refund": "zwrot", "duplicate": "duplikat", "other": "inne",
}
STOP_WORDS = {"sp", "z", "o", "oo", "sa", "s", "a", "spolka", "zoo", "ltd", "inc", "gmbh", "the", "polska", "bank"}


@dataclass
class Case:
    family: str
    counterparty: UUID
    outcome_key: str
    outcome: dict[str, Any]
    subject_type: str
    subject_id: UUID
    day: date | None
    amount_minor: int
    label: str
    decision_id: UUID | None
    source: str                       # human | hard_identifier
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def month(self) -> str | None:
        return month_of(self.day)

    def example(self) -> dict[str, Any]:
        return {"subject_type": self.subject_type, "subject_id": str(self.subject_id),
                "date": self.day.isoformat() if self.day else None, "amount": money(self.amount_minor),
                "label": self.label[:120], "decision_id": str(self.decision_id) if self.decision_id else None,
                "outcome": describe(self.family, self.outcome)}


@dataclass
class MineReport:
    created: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped_decided: int = 0
    below_threshold: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Kandydaci na reguły: nowi {len(self.created)}, odświeżeni {len(self.refreshed)}, bez zmian "
                 f"{self.unchanged}, już rozstrzygnięci przez człowieka {self.skipped_decided}"]
        for key in self.created + self.refreshed:
            lines.append(f"  {key}")
        if self.below_threshold:
            lines.append(f"  poniżej progu: {len(self.below_threshold)} wzorców")
        return "\n".join(lines)


def describe(family: str, outcome: dict[str, Any]) -> str:
    if family == "category":
        return f"kategoria {outcome.get('category')}"
    if family == "no_document":
        return f"bez dokumentu: {SUBTYPE_LABELS.get(outcome.get('subtype'), outcome.get('subtype'))}"
    if family == "allocation":
        return f"obiekt {outcome.get('name') or outcome.get('cost_object_id')}"
    if family == "monthly_bulk":
        return "jeden przelew za kilka faktur" if outcome.get("bulk") else "przelew za jedną fakturę"
    if family == "payer_alias":
        return f"klient {outcome.get('client_name') or outcome.get('client')}"
    return str(outcome)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


_SUBJECT_PARTY = """
    CASE {alias}.subject_type::text
        WHEN 'transaction' THEN (SELECT t.counterparty_id FROM l1_bank_transaction t WHERE t.id = {alias}.subject_id)
        WHEN 'document' THEN (SELECT d.counterparty_id FROM l1_document d WHERE d.id = {alias}.subject_id)
        WHEN 'line' THEN (SELECT d.counterparty_id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id
                           WHERE l.id = {alias}.subject_id)
    END
"""


def aliases(conn: psycopg.Connection, canonical: set[UUID]) -> set[UUID]:
    """Every counterparty row that may resolve to one of the canonical ones (same IBAN or name)."""
    rows = conn.execute(
        """
        SELECT DISTINCT c.id FROM l1_counterparty c JOIN l1_counterparty n ON n.company_id = c.company_id
         WHERE n.id = ANY(%s) AND (c.id = n.id OR c.ibans && n.ibans
                                   OR (c.name_key IS NOT NULL AND c.name_key = n.name_key))
        """,
        ([str(c) for c in canonical],),
    ).fetchall()
    return {r[0] for r in rows} | set(canonical)


def _wanted(raw: Any, only: set[UUID] | None) -> bool:
    return only is None or (raw is not None and raw in only)


def _canonical(conn: psycopg.Connection, cache: dict[Any, UUID | None], counterparty_id: Any) -> UUID | None:
    if counterparty_id not in cache:
        cache[counterparty_id] = canonical_counterparty(conn, counterparty_id) if counterparty_id else None
    return cache[counterparty_id]


def _case(conn, cache, company_id, family, subject_type, subject_id, outcome_key, outcome, decision_id,
          source="human", extra=None) -> Case | None:
    facts = subject_facts(conn, company_id, subject_type, subject_id)
    party = _canonical(conn, cache, facts.get("counterparty_id")) if facts else None
    if party is None:
        return None
    return Case(family, party, outcome_key, outcome, subject_type, subject_id, facts.get("date"),
                int(facts.get("amount_minor") or 0), facts.get("label") or "", decision_id, source, extra or {})


def _category_cases(conn: psycopg.Connection, company_id: UUID, cache: dict, only: set[UUID] | None) -> list[Case]:
    rows = conn.execute(
        f"""
        SELECT x.subject_type::text, x.subject_id, x.category, x.decision_id, {_SUBJECT_PARTY.format(alias="x")}
          FROM l3_category_decision x
         WHERE x.company_id = %s AND x.status = 'active' AND x.method = 'human'
        """,
        (company_id,),
    ).fetchall()
    out = []
    for subject_type, subject_id, category, decision_id, raw in rows:
        if not _wanted(raw, only):
            continue
        case = _case(conn, cache, company_id, "category", subject_type, subject_id, category,
                     {"category": category}, decision_id)
        if case:
            out.append(case)
    return out


def _status_cases(conn: psycopg.Connection, company_id: UUID, cache: dict, only: set[UUID] | None) -> list[Case]:
    """Final statuses decided by a person (the main leg only): no_document with subtype = a hit, others = exceptions."""
    rows = conn.execute(
        """
        SELECT s.transaction_id, s.final_status::text, s.subtype, s.decision_id, t.counterparty_id
          FROM l3_transaction_status s JOIN l1_bank_transaction t ON t.id = s.transaction_id
         WHERE s.company_id = %s AND s.status = 'active' AND s.method = 'human'
           AND (s.evidence ->> 'derived_from') IS NULL
        """,
        (company_id,),
    ).fetchall()
    out = []
    for tx, status, subtype, decision_id, raw in rows:
        if not _wanted(raw, only):
            continue
        key = f"no_document:{subtype}" if status == "no_document" and subtype else status
        case = _case(conn, cache, company_id, "no_document", "transaction", tx, key,
                     {"final_status": status, "subtype": subtype}, decision_id)
        if case:
            out.append(case)
    return out


def _allocation_cases(conn: psycopg.Connection, company_id: UUID, cache: dict, only: set[UUID] | None) -> list[Case]:
    rows = conn.execute(
        f"""
        SELECT a.subject_type::text, a.subject_id, a.decision_id, array_agg(a.cost_object_id ORDER BY a.cost_object_id),
               array_agg(a.share ORDER BY a.cost_object_id), array_agg(co.name ORDER BY a.cost_object_id),
               min(({_SUBJECT_PARTY.format(alias="a")})::text)
          FROM l3_allocation a JOIN l3_cost_object co ON co.id = a.cost_object_id
         WHERE a.company_id = %s AND a.status = 'active' AND a.method = 'human'
         GROUP BY 1, 2, 3
        """,
        (company_id,),
    ).fetchall()
    out = []
    for subject_type, subject_id, decision_id, objects, shares, names, raw in rows:
        if not _wanted(UUID(raw) if raw else None, only):
            continue
        if len(objects) == 1:
            key, outcome = str(objects[0]), {"cost_object_id": str(objects[0]), "name": names[0]}
        else:
            key = "split:" + ",".join(str(o) for o in objects)
            outcome = {"split": [{"cost_object_id": str(o), "share": str(s), "name": n}
                                 for o, s, n in zip(objects, shares, names, strict=True)]}
        case = _case(conn, cache, company_id, "allocation", subject_type, subject_id, key, outcome, decision_id)
        if case:
            out.append(case)
    return out


def _title_token(title: str, name: str | None) -> str | None:
    """A word of the client's name that the payer wrote in the title (Revolut: 'KWADRAT DEVELOPMENT ...')."""
    if not name:
        return None
    words = [w for w in re.findall(r"[^\W\d_]{3,}", fold(name)) if w not in STOP_WORDS]
    folded = fold(title or "")
    for word in words:
        if re.search(rf"\b{re.escape(word)}\b", folded):
            return word
    return None


def _one_document(conn: psycopg.Connection, company_id: UUID, direction: str, where: str,
                  params: tuple) -> UUID | None:
    rows = conn.execute(
        f"""
        SELECT DISTINCT l.head_id FROM l1_document d JOIN document_lineage l ON l.document_id = d.id
         WHERE d.company_id = %s AND d.direction = %s AND d.superseded_by IS NULL AND ({where})
        """,
        (company_id, direction, *params),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def title_resolves(conn: psycopg.Connection, company_id: UUID, transaction_id: UUID, documents: list[Any]) -> bool:
    """The identifiers written in the title (read by code: KSeF numbers, the MPP block, invoice numbers) decide
    the group by themselves: each names exactly one current document of the company in the direction of the
    transfer, and together they are exactly the documents of the decision (compared as chains of versions).

    Only then is an automatic match evidence for mining - the identifier is the evidence, the automation's
    choice is not (a title with a number of another invoice, a number shared by two documents, a number that
    names fewer documents than the decision settled: skipped). Review of 4C/5, point 3."""
    row = conn.execute("SELECT amount_minor, extracted FROM l1_bank_transaction WHERE id = %s",
                       (transaction_id,)).fetchone()
    if row is None or not documents:
        return False
    direction = "sales" if int(row[0]) > 0 else "purchase"
    extracted = row[1] or {}
    named: set[UUID] = set()
    identifiers = 0
    for item in extracted.get("ksef_numbers") or []:
        if not item.get("number") or not item.get("checksum_ok", True):
            return False
        doc = _one_document(conn, company_id, direction, "d.ksef_number = %s", (item["number"],))
        if doc is None:
            return False
        named.add(doc)
        identifiers += 1
    mpp = extracted.get("mpp") or {}
    if mpp.get("inv_normalized"):
        doc = _one_document(conn, company_id, direction,
                            "d.number_normalized = %s AND (%s::text IS NULL OR d.seller_nip = %s::text)",
                            (mpp["inv_normalized"], mpp.get("idc_nip"), mpp.get("idc_nip")))
        if doc is None:
            return False
        named.add(doc)
        identifiers += 1
    for item in extracted.get("invoice_numbers") or []:
        forms = [f for f in [item.get("normalized"), *(item.get("alt") or [])] if f]
        doc = _one_document(conn, company_id, direction, "d.number_normalized = ANY(%s)", (forms,)) if forms else None
        if doc is None:
            return False
        named.add(doc)
        identifiers += 1
    if not identifiers:
        return False
    heads = {r[0] for r in conn.execute(
        "SELECT DISTINCT head_id FROM document_lineage WHERE document_id = ANY(%s::uuid[])",
        ([str(d) for d in documents],)).fetchall()}
    return named == heads


def _match_cases(conn: psycopg.Connection, company_id: UUID, cache: dict, only: set[UUID] | None) -> list[Case]:
    rows = conn.execute(
        """
        SELECT d.id, d.subject_id, d.payload, d.method::text, coalesce(d.evidence -> 'tags', '[]'::jsonb),
               (SELECT t.counterparty_id FROM l1_bank_transaction t WHERE t.id = d.subject_id)
          FROM l3_decision_log d
         WHERE d.company_id = %s AND d.kind = 'match' AND d.subject_type = 'transaction'
           AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
           AND (d.method = 'human' OR coalesce(d.evidence -> 'tags', '[]'::jsonb) ?| %s)
        """,
        (company_id, list(HARD_TAGS)),
    ).fetchall()
    out = []
    for decision_id, tx, payload, method, _tags, raw in rows:
        if not _wanted(raw, only):
            continue
        payload = payload or {}
        if payload.get("final_status") != "matched":
            continue
        documents = [i["document_id"] for i in payload.get("items") or [] if i.get("document_id")]
        if not documents:
            continue
        if method != "human" and not title_resolves(conn, company_id, tx, documents):
            continue                    # the automation chose more than the title says: not evidence
        source = "human" if method == "human" else "hard_identifier"
        bulk = len(documents) >= 2
        case = _case(conn, cache, company_id, "monthly_bulk", "transaction", tx, "bulk" if bulk else "single",
                     {"bulk": bulk, "documents": len(documents)}, decision_id, source)
        if case:
            out.append(case)
        # payer alias: the transfer comes from another party (an operator) than the invoices' counterparty
        parties = {r[0] for r in conn.execute(
            "SELECT DISTINCT counterparty_id FROM l1_document WHERE id = ANY(%s::uuid[])", (documents,)).fetchall()}
        clients = {_canonical(conn, cache, p) for p in parties if p}
        facts = subject_facts(conn, company_id, "transaction", tx)
        payer = _canonical(conn, cache, facts.get("counterparty_id"))
        if len(clients) == 1 and payer and payer not in clients and facts.get("counterparty_iban"):
            client = next(iter(clients))
            name = counterparty_name(conn, client)
            token = _title_token(facts.get("title") or "", name)
            if token:
                out.append(Case("payer_alias", payer, f"{facts['counterparty_iban']}|{token}|{client}",
                                {"client": str(client), "client_name": name, "iban": facts["counterparty_iban"],
                                 "token": token}, "transaction", tx, facts.get("date"),
                                int(facts.get("amount_minor") or 0), facts.get("label") or "", decision_id, source))
    return out


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


def _pattern_action(family: str, party: UUID, outcome: dict[str, Any], subject_types: list[str]
                    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    scope = {"counterparties": [str(party)], "subject_types": sorted(set(subject_types)),
             "note": "nie obejmuje nowych kontrahentów"}
    by_party = {"all": [{"field": "counterparty_id", "op": "eq", "value": str(party)}]}
    if family == "category":
        return by_party, {"kind": "category", "category": outcome["category"]}, scope
    if family == "no_document":
        if outcome.get("final_status") != "no_document" or not outcome.get("subtype"):
            return None
        return by_party, {"kind": "final_status", "final_status": "no_document", "subtype": outcome["subtype"]}, scope
    if family == "allocation":
        if "cost_object_id" not in outcome:
            return None
        return by_party, {"kind": "allocation", "cost_object_id": outcome["cost_object_id"], "share": 1}, scope
    if family == "monthly_bulk":
        if not outcome.get("bulk"):
            return None
        return by_party, {"kind": "match_strategy", "strategy": "monthly_bulk"}, scope
    if family == "payer_alias":
        pattern = {"all": [{"field": "counterparty_iban", "op": "eq", "value": outcome["iban"]},
                           {"field": "title_regex", "op": "regex", "value": f"(?i){re.escape(outcome['token'])}"}]}
        return pattern, {"kind": "match_strategy", "strategy": "payer_alias", "counterparty_id": outcome["client"]}, scope
    return None


def _question(family: str, party_name: str, outcome: dict[str, Any]) -> str:
    who = party_name or "ten kontrahent"
    if family == "category":
        return f"Czy {who} to zawsze kategoria „{outcome['category']}”?"
    if family == "no_document":
        return f"Czy przelewy {who} to zawsze „bez dokumentu: {SUBTYPE_LABELS.get(outcome['subtype'], outcome['subtype'])}”?"
    if family == "allocation":
        return f"Czy koszty od {who} zawsze idą w 100% do „{outcome.get('name')}”?"
    if family == "monthly_bulk":
        return f"Czy {who} płaci zbiorczo (jeden przelew za kilka faktur)?"
    if family == "payer_alias":
        return (f"Czy wpłata z rachunku {outcome['iban'][:6]}… ({who}) ze słowem „{outcome['token']}” w tytule "
                f"to zawsze klient {outcome.get('client_name')}?")
    return f"Czy {who} → {outcome}?"


def _next_key(conn: psycopg.Connection, company_id: UUID) -> str:
    row = conn.execute(
        "SELECT coalesce(max((substring(rule_key FROM '^R-([0-9]+)$'))::int), 0) + 1 FROM l3_rule WHERE company_id = %s",
        (company_id,),
    ).fetchone()
    return f"R-{row[0]}"


def _existing(conn: psycopg.Connection, company_id: UUID, signature: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, rule_key, version, stage::text, method::text, evidence_count, exceptions FROM l3_rule
         WHERE company_id = %s AND evidence ->> 'signature' = %s ORDER BY version DESC LIMIT 1
        """,
        (company_id, signature),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "rule_key", "version", "stage", "method", "evidence_count", "exceptions")
    return dict(zip(keys, row, strict=True))


def _card(conn: psycopg.Connection, company_id: UUID, rule_id: UUID, proposal: dict[str, Any],
          evidence: dict[str, Any], score: float, created_by: str) -> UUID:
    conn.execute(
        """
        UPDATE l2_suggestion SET status = 'expired', resolved_at = now()
         WHERE kind = 'rule_candidate' AND subject_type = 'rule' AND subject_id = %s AND status = 'open'
        """,
        (rule_id,),
    )
    return conn.execute(
        """
        INSERT INTO l2_suggestion (company_id, kind, subject_type, subject_id, proposal, score, rank, source, method,
                                   confidence, evidence, created_by)
        VALUES (%s, 'rule_candidate', 'rule', %s, %s, %s, 1, %s, 'code', %s, %s, %s) RETURNING id
        """,
        (company_id, rule_id, Jsonb(proposal), round(score, 6), SOURCE, round(min(score, 1.0), 4), Jsonb(evidence),
         created_by),
    ).fetchone()[0]


def collect(conn: psycopg.Connection, company_id: UUID, only: set[UUID] | None = None) -> list[Case]:
    """Every case of the company (``only`` = raw counterparty ids to look at - incremental mining)."""
    cache: dict[Any, UUID | None] = {}
    return (_category_cases(conn, company_id, cache, only) + _status_cases(conn, company_id, cache, only)
            + _allocation_cases(conn, company_id, cache, only) + _match_cases(conn, company_id, cache, only))


def mine(conn: psycopg.Connection, company: Any, *, min_cases: int = MIN_CASES, min_months: int = MIN_MONTHS,
         counterparties: set[UUID] | None = None, created_by: str = SOURCE, dry_run: bool = False) -> MineReport:
    """Find candidates (idempotent). ``counterparties`` limits the run (incremental mining after a decision)."""
    company_id = company.id if hasattr(company, "id") else company
    report = MineReport()
    cases = collect(conn, company_id, aliases(conn, counterparties) if counterparties else None)
    by_family_party: dict[tuple[str, UUID], list[Case]] = defaultdict(list)
    for case in cases:
        by_family_party[(case.family, case.counterparty)].append(case)
    for (family, party), group in sorted(by_family_party.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        if counterparties is not None and party not in counterparties:
            continue
        outcomes: dict[str, list[Case]] = defaultdict(list)
        for case in group:
            outcomes[case.outcome_key].append(case)
        total = len(group)
        for outcome_key, hits in sorted(outcomes.items()):
            months = sorted({c.month for c in hits if c.month})
            built = _pattern_action(family, party, hits[0].outcome, [c.subject_type for c in hits])
            if built is None:
                continue
            if len(hits) < min_cases or len(months) < min_months or len(hits) * 2 <= total:
                report.below_threshold.append({"family": family, "counterparty": str(party), "outcome": outcome_key,
                                               "hits": len(hits), "total": total, "months": months})
                continue
            pattern, action, scope = built
            signature = f"{family}|{party}|{outcome_key}"
            exceptions = [c.example() for c in group if c.outcome_key != outcome_key]
            examples = [c.example() for c in sorted(hits, key=lambda c: (c.day or date.min), reverse=True)[:3]]
            party_name = counterparty_name(conn, party) or ""
            question = _question(family, party_name, hits[0].outcome)
            sources = sorted({c.source for c in hits})
            existing = _existing(conn, company_id, signature)
            if existing and (existing["method"] != "code" or existing["stage"] != "candidate"):
                report.skipped_decided += 1          # a person decided (accepted, corrected or said "no")
                continue
            if existing and existing["evidence_count"] == len(hits) and len(existing["exceptions"] or []) == len(
                    exceptions):
                report.unchanged += 1
                continue
            if dry_run:
                (report.refreshed if existing else report.created).append(f"{question} ({len(hits)} z {total})")
                continue
            evidence = {"signature": signature, "family": family, "hits": len(hits), "total": total,
                        "counterparties": 1, "months": months, "sources": sources,
                        "decisions": [str(c.decision_id) for c in hits if c.decision_id][:200],
                        "note": "z decyzji człowieka i twardych identyfikatorów - nigdy z samego automatu"}
            if existing:
                rule_id, rule_key = existing["id"], existing["rule_key"]
                conn.execute(
                    "UPDATE l3_rule SET evidence_count = %s, exceptions = %s, evidence = %s, question = %s, scope = %s "
                    "WHERE id = %s",
                    (len(hits), Jsonb(exceptions), Jsonb(evidence), question, Jsonb(scope), rule_id),
                )
                report.refreshed.append(f"{rule_key}: {question} ({len(hits)} z {total})")
            else:
                rule_key = _next_key(conn, company_id)
                rule_id = conn.execute(
                    """
                    INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, evidence_count,
                                         exceptions, question, scope, source, method, confidence, evidence, created_by)
                    VALUES (%s, %s, 1, 'candidate', %s, %s, %s, %s, %s, %s, %s, 'code', %s, %s, %s) RETURNING id
                    """,
                    (company_id, rule_key, Jsonb(pattern), Jsonb(action), len(hits), Jsonb(exceptions), question,
                     Jsonb(scope), SOURCE, round(len(hits) / total, 4), Jsonb(evidence), created_by),
                ).fetchone()[0]
                report.created.append(f"{rule_key}: {question} ({len(hits)} z {total})")
            proposal = {"rule_key": rule_key, "question": question, "hits": len(hits), "total": total,
                        "counterparties": 1, "counterparty_names": [party_name], "months": months,
                        "examples": examples, "exceptions": exceptions, "pattern": pattern, "action": action,
                        "scope": scope, "family": family}
            facts = [f"{len(hits)} z {total} decyzji, u 1 kontrahenta ({party_name})",
                     f"miesiące: {', '.join(months)}",
                     "źródło: " + " i ".join({"human": "Twoje decyzje",
                                              "hard_identifier": "twarde identyfikatory w tytule"}[s] for s in sources),
                     f"wyjątki: {len(exceptions)}" if exceptions else "bez wyjątków",
                     "nie obejmie nowych kontrahentów"]
            badge = "high" if not exceptions and len(hits) >= 10 else "medium"
            _card(conn, company_id, rule_id, proposal,
                  {"facts": facts, "badge": badge, "precedents": len(hits), "hard": False, "margin": None,
                   "tags": ["rule_candidate", family]}, len(hits) / total, created_by)
    return report
