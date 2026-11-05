"""Signals that put a cost (or revenue) into a cost object - the table of step 5 of the briefing.

| strength | signal                                                        | automation?                         |
| -------- | ------------------------------------------------------------- | ----------------------------------- |
| A        | explicit assignment in an internal file (``cost_object_ref``) | no by default (D2), extension flag  |
| A        | explicit identifier: a unique token of the name in the line,  | yes, when nothing contradicts it    |
|          | the title or the mail; a mature counterparty rule             |                                     |
| B        | offer <-> invoice (an anchor with the same amount), supplier  | suggestion; two objects = queue     |
|          | + service period inside the window of one object, correction  |                                     |
|          | inheriting the split of the corrected invoice, the cascade of |                                     |
|          | a collective invoice                                          |                                     |
| C        | shared participants (people of the internal files)            | hint only                           |
| D        | the client alone, time proximity alone                        | narrowing only, never rank 1        |
| -        | no signal                                                     | UNASSIGNED (a proposal, rank 1)     |

Cascade of a collective line (one line for several objects): the description names the objects
-> the amount equals one offer -> in proportion to offer values (some offers missing = a partial
proposal, the rest to fill in) -> in proportion to person-days of the internal files -> equally,
but only when the person accepted such a rule for the client -> UNASSIGNED.
"""

from __future__ import annotations

import importlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg

from kasa.core.allocation.amounts import DocumentFacts, InternalRecordFacts, distribute, to_pln
from kasa.core.allocation.objects import (
    ObjectProfile,
    named_objects,
    resolve,
    resolve_reference,
    unassigned,
)
from kasa.core.allocation.subjects import Subject
from kasa.core.cost_objects_discovery.text import token_set, window_days, window_gap_days
from kasa.core.extension import Extension

CONTRADICTION_DAYS = 3
SHORT_SERVICE_DAYS = 14
COLLECTIVE_MIN_DAYS = 7
D9_OP_TYPES = frozenset({"zus", "tax"})
D9_INTENTS = frozenset({"zus", "tax"})

SCORES = {
    "explicit": 0.99, "rule_auto": 0.99, "internal_file": 0.96, "inherited": 0.9, "window_city": 0.9,
    "window": 0.85, "offer_match": 0.85, "contradiction": 0.8, "offer_value": 0.8, "line_description": 0.75,
    "person_days": 0.72, "equal_rule": 0.7, "rule_hint": 0.8, "partial": 0.6, "person_days_alt": 0.65,
    "unassigned_none": 0.7, "unassigned_hint": 0.55, "unassigned_alt": 0.1, "shared_people": 0.45,
    "client_only": 0.2, "near": 0.2, "tie": 0.5,
}


@dataclass(frozen=True)
class AllocationSignal:
    """One share of one proposal (``option``) for a subject."""

    strength: str | None                   # A | B | C | D | None (no signal: UNASSIGNED)
    cost_object_id: UUID
    share: Fraction
    fact_pl: str
    cascade_step: str | None = None
    option: int = 0
    score: float = 0.0
    tags: tuple[str, ...] = ()
    hard: bool = False
    partial: bool = False
    auto: bool = False
    rule: tuple[UUID, int, str] | None = None
    contradiction: bool = False


@dataclass
class Proposal:
    shares: list[tuple[UUID, Fraction]]
    strength: str | None
    cascade_step: str | None
    score: float
    facts: list[str]
    tags: list[str]
    hard: bool = False
    partial: bool = False
    auto: bool = False
    rule: tuple[UUID, int, str] | None = None
    contradiction: bool = False
    badge_override: str | None = None

    @property
    def object_ids(self) -> frozenset[UUID]:
        return frozenset(o for o, _ in self.shares)


@dataclass
class AllocationContext:
    company_id: UUID
    extension: Extension
    profiles: dict[UUID, ObjectProfile]
    documents: dict[UUID, DocumentFacts]
    records: dict[UUID, InternalRecordFacts]
    allocations: dict[tuple[str, UUID], list[tuple[UUID, Decimal]]] = field(default_factory=dict)
    person_days: dict[UUID, Fraction] = field(default_factory=dict)
    offers: dict[UUID, list[tuple[UUID, int, str]]] = field(default_factory=dict)
    precedents: dict[tuple[UUID, UUID], int] = field(default_factory=dict)
    rule_hits: Any = None                  # callable(subject) -> list of hits, or None

    @property
    def unassigned_id(self) -> UUID:
        return unassigned(self.profiles).id

    def targets(self) -> list[ObjectProfile]:
        return [p for p in self.profiles.values() if p.is_target]

    def name(self, cost_object_id: UUID) -> str:
        profile = self.profiles.get(cost_object_id)
        return profile.name if profile else str(cost_object_id)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _rules_module():
    try:
        return importlib.import_module("kasa.core.rules.apply")
    except ImportError:
        return None


def rule_hits_for(conn: psycopg.Connection, company_id: UUID):
    """Hits of learned rules (4C, ``kasa.core.rules.apply``); without the module this layer is skipped."""
    module = _rules_module()
    applicable = getattr(module, "applicable_rules", None) if module else None
    if applicable is None:
        return None

    def hits(subject: Subject) -> list[Any]:
        facts = {
            "counterparty_id": subject.counterparty_id, "counterparty_kind": subject.counterparty_kind,
            "op_type": subject.facts.get("op_type"), "intent": subject.facts.get("intent"),
            "title": subject.text, "amount_minor": subject.amount_minor, "direction": subject.direction,
            "subject_type": subject.subject_type,
        }
        try:
            found = applicable(conn, company_id, subject.subject_type, facts) or []
        except Exception:  # noqa: BLE001 - a rule layer in progress never breaks allocation
            return []
        return [h for h in found if (getattr(h, "action", None) or {}).get("kind") == "allocation"]

    return hits


def build_context(conn: psycopg.Connection, company_id: UUID, extension: Extension, profiles, documents,
                  records) -> AllocationContext:
    ctx = AllocationContext(company_id=company_id, extension=extension, profiles=profiles, documents=documents,
                            records=records)
    for subject_type, subject_id, cost_object_id, share in conn.execute(
        """
        SELECT subject_type::text, subject_id, cost_object_id, share FROM l3_allocation
         WHERE company_id = %s AND status = 'active'
        """,
        (company_id,),
    ).fetchall():
        ctx.allocations.setdefault((subject_type, subject_id), []).append((cost_object_id, share))
    # person-days: rows of internal files of a person-day settlement type, per cost object
    kinds = {k.casefold() for k in extension.allocation.person_day_settlement_types}
    member_of: dict[UUID, UUID] = {}
    for profile in profiles.values():
        if profile.is_target:
            for (record_type, record_id), _ in profile.members.items():
                if record_type == "internal_record":
                    member_of.setdefault(record_id, profile.id)
    for record in records.values():
        if kinds and (record.settlement_type or "").casefold() not in kinds:
            continue
        if not kinds and not record.person_pseudonym:
            continue
        allocated = ctx.allocations.get(("internal_record", record.id))
        if allocated:
            for cost_object_id, share in allocated:
                target = resolve(profiles, cost_object_id)
                if target and target.is_target:
                    ctx.person_days[target.id] = ctx.person_days.get(target.id, Fraction(0)) + Fraction(share)
            continue
        target_id = member_of.get(record.id)
        if target_id is None:
            found = resolve_reference(record.cost_object_ref, profiles)
            target_id = found[0].id if len(found) == 1 else None
        if target_id:
            ctx.person_days[target_id] = ctx.person_days.get(target_id, Fraction(0)) + 1
    # offers: proforma sales documents naming exactly one object
    for doc in documents.values():
        if doc.kind != "proforma" or doc.direction != "sales":
            continue
        tokens = token_set(" ".join(ln.name or "" for ln in doc.lines))
        named = named_objects(tokens, profiles)
        if len(named) == 1:
            value = to_pln(doc.net_minor if doc.net_minor is not None else doc.gross_minor, doc.currency, doc.fx_rate)
            ctx.offers.setdefault(named[0][0].id, []).append((doc.id, value, doc.number_raw))
    # precedents: the person's allocations of the same counterparty to the same object
    for counterparty_id, cost_object_id, count in conn.execute(
        """
        SELECT coalesce(d.counterparty_id, dl.counterparty_id, t.counterparty_id), a.cost_object_id, count(*)
          FROM l3_allocation a
          LEFT JOIN l1_document d ON a.subject_type = 'document' AND d.id = a.subject_id
          LEFT JOIN l1_document_line l ON a.subject_type = 'line' AND l.id = a.subject_id
          LEFT JOIN l1_document dl ON dl.id = l.document_id
          LEFT JOIN l1_bank_transaction t ON a.subject_type = 'transaction' AND t.id = a.subject_id
         WHERE a.company_id = %s AND a.status = 'active' AND a.method = 'human'
         GROUP BY 1, 2
        """,
        (company_id,),
    ).fetchall():
        if counterparty_id:
            ctx.precedents[(counterparty_id, cost_object_id)] = int(count)
    ctx.rule_hits = rule_hits_for(conn, company_id)
    return ctx


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _fmt_window(window) -> str:
    if not window:
        return "?"
    a, b = window
    return f"{a:%d.%m.%Y}" if a == b else f"{a:%d.%m}-{b:%d.%m.%Y}"


def contradictions(subject: Subject, profile: ObjectProfile) -> list[str]:
    """What speaks against an explicit identifier (dates, client, city written on the subject)."""
    found = []
    if profile.window:
        start, end = profile.window
        for w in subject.text_windows:
            if w[1] < start - timedelta(days=CONTRADICTION_DAYS) or w[0] > end + timedelta(days=CONTRADICTION_DAYS):
                found.append(f"data {_fmt_window(w)} poza oknem {_fmt_window(profile.window)}")
                break
    if subject.direction == "sales" and subject.client_tokens and profile.client_tokens \
            and not subject.client_tokens & profile.client_tokens:
        found.append(f"inny klient niż {profile.attrs.get('client')}")
    if subject.city_from_text and subject.city_key and profile.city_key and subject.city_key != profile.city_key:
        found.append(f"inne miasto niż {profile.attrs.get('city')}")
    return found


def _equal(ids: list[UUID]) -> list[tuple[UUID, Fraction]]:
    return [(i, Fraction(1, len(ids))) for i in ids]


def _proportional(weights: dict[UUID, Fraction | int]) -> list[tuple[UUID, Fraction]]:
    total = sum(Fraction(w) for w in weights.values())
    return [(i, Fraction(w) / total) for i, w in weights.items()]


class _Options:
    def __init__(self):
        self.signals: list[AllocationSignal] = []
        self.next = 0

    def add(self, shares: list[tuple[UUID, Fraction]], strength: str | None, fact: str, *, score: float,
            step: str | None = None, tags: tuple[str, ...] = (), hard: bool = False, partial: bool = False,
            auto: bool = False, rule=None, contradiction: bool = False) -> None:
        option = self.next
        self.next += 1
        for cost_object_id, share in shares:
            self.signals.append(AllocationSignal(
                strength=strength, cost_object_id=cost_object_id, share=share, fact_pl=fact, cascade_step=step,
                option=option, score=score, tags=tags, hard=hard, partial=partial, auto=auto, rule=rule,
                contradiction=contradiction))


def _cascade(subject: Subject, objects: list[ObjectProfile], ctx: AllocationContext, opts: _Options, *,
             enumerated: bool) -> None:
    ids = sorted((o.id for o in objects), key=lambda i: ctx.name(i))
    names = ", ".join(ctx.name(i) for i in ids)
    base = (f"pozycja wymienia {len(ids)} obiekty: {names}" if enumerated
            else f"pozycja zbiorcza: {len(ids)} obiekty klienta w okresie {_fmt_window(subject.window)} ({names})")
    if enumerated:
        opts.add(_equal(ids), "B", base + "; podział równy - tylko Twoją decyzją", score=SCORES["line_description"],
                 step="line_description", tags=("collective", "line_description"))
    amount = abs(subject.amount_minor or 0)
    # offer <-> line: the amount equals the offer of exactly one object
    exact = [i for i in ids for _, value, _ in ctx.offers.get(i, []) if amount and value == amount]
    if len(set(exact)) == 1:
        opts.add([(exact[0], Fraction(1))], "B", base + f"; kwota = oferta {ctx.name(exact[0])}",
                 score=SCORES["offer_match"], step="offer_match", tags=("offer_match",))
        return
    with_offer = {i: sum(v for _, v, _ in ctx.offers.get(i, [])) for i in ids if ctx.offers.get(i)}
    if with_offer and amount:
        if len(with_offer) == len(ids):
            opts.add(_proportional(with_offer), "B", base + "; proporcjonalnie do wartości ofert",
                     score=SCORES["offer_value"], step="offer_value", tags=("offer_value",))
        else:
            shares = [(i, Fraction(v, amount)) for i, v in with_offer.items()]
            if sum(s for _, s in shares) <= 1:
                missing = ", ".join(ctx.name(i) for i in ids if i not in with_offer)
                opts.add(shares, "B", base + f"; propozycja częściowa: brak oferty dla {missing} - reszta do "
                         "uzupełnienia", score=SCORES["partial"], step="offer_value", tags=("offer_value", "partial"),
                         partial=True)
                return
    days = {i: ctx.person_days.get(i, Fraction(0)) for i in ids}
    if all(d > 0 for d in days.values()):
        detail = ", ".join(f"{ctx.name(i)} {float(d):g}" for i, d in days.items())
        opts.add(_proportional(days), "B", base + f"; osobodni z plików wewnętrznych: {detail}",
                 score=SCORES["person_days_alt" if enumerated else "person_days"], step="person_days",
                 tags=("person_days",))
        if not enumerated:
            return
    for hit in _equal_split_rules(subject, ctx):
        opts.add(_equal(ids), "B", base + f"; podział równy wg reguły {hit.rule_key}@{hit.version}",
                 score=SCORES["equal_rule"], step="equal", tags=(f"rule:{hit.rule_key}@{hit.version}",),
                 rule=(hit.rule_id, hit.version, hit.rule_key))
        return


def _equal_split_rules(subject: Subject, ctx: AllocationContext) -> list[Any]:
    if ctx.rule_hits is None:
        return []
    return [h for h in ctx.rule_hits(subject) if (h.action or {}).get("split") == "equal"]


def _inherited(subject: Subject, ctx: AllocationContext) -> list[tuple[UUID, Fraction]] | None:
    doc = subject.document
    if doc is None or doc.kind != "correction" or not doc.corrects_document_id:
        return None
    parent = ctx.documents.get(doc.corrects_document_id)
    if parent is None:
        return None
    weights: dict[UUID, Fraction] = defaultdict(Fraction)
    keys = [("line", ln.id, ln.memorial_minor or ln.cash_weight_minor or 1) for ln in parent.lines] or [
        ("document", parent.id, 1)]
    covered = 0
    for subject_type, subject_id, weight in keys:
        allocated = ctx.allocations.get((subject_type, subject_id))
        if not allocated:
            continue
        covered += 1
        for cost_object_id, share in allocated:
            target = resolve(ctx.profiles, cost_object_id)
            if target:
                weights[target.id] += Fraction(share) * Fraction(abs(weight))
    if not weights or covered != len(keys):
        return None
    return _proportional(dict(weights))


def signals_for(subject: Subject, ctx: AllocationContext) -> list[AllocationSignal]:
    """Every signal for one subject, grouped into options (proposals)."""
    opts = _Options()
    profiles = ctx.profiles
    explicit_done = False

    # learned rules of the counterparty (4C)
    if ctx.rule_hits is not None:
        for hit in ctx.rule_hits(subject):
            action = hit.action or {}
            target = resolve(profiles, UUID(str(action["cost_object_id"]))) if action.get("cost_object_id") else None
            if target is None or not (target.is_target or target.system_code):
                continue
            share = Fraction(str(action.get("share", 1)))
            auto = hit.stage == "auto"
            opts.add([(target.id, share)], "A" if auto else "B",
                     f"reguła {hit.rule_key}@{hit.version}: {hit.evidence_count} zgodnych decyzji",
                     score=SCORES["rule_auto" if auto else "rule_hint"], tags=(f"rule:{hit.rule_key}@{hit.version}",),
                     hard=auto, auto=auto, rule=(hit.rule_id, hit.version, hit.rule_key))
            explicit_done = explicit_done or auto

    # A: explicit assignment in an internal file
    if subject.subject_type == "internal_record" and subject.record and subject.record.cost_object_ref:
        found = resolve_reference(subject.record.cost_object_ref, profiles)
        hard = ctx.extension.allocation.internal_record_is_hard
        if len(found) == 1:
            opts.add([(found[0].id, Fraction(1))], "A",
                     f"plik wewnętrzny wskazuje „{subject.record.cost_object_ref}” = {found[0].name}"
                     + ("" if hard else " (arkusz nie jest twardym dowodem, D2)"),
                     score=SCORES["internal_file"], tags=("internal_file_assignment", "cost_object_ref"),
                     hard=hard, auto=hard)
            explicit_done = True
        elif len(found) > 1:
            for profile in found:
                opts.add([(profile.id, Fraction(1))], "B",
                         f"plik wewnętrzny: „{subject.record.cost_object_ref}” pasuje do kilku obiektów",
                         score=SCORES["tie"], tags=("internal_file_assignment", "tie"))
            explicit_done = True

    # A: explicit identifier in the line / title / text, or several objects named (cascade)
    named = [] if explicit_done else named_objects(subject.tokens, profiles)
    if len(named) == 1:
        profile, _hits, unique_hits = named[0]
        against = contradictions(subject, profile)
        where = {"line": "pozycji", "document": "dokumencie", "transaction": "tytule",
                 "internal_record": "wierszu"}.get(subject.subject_type, "tekście")
        if not against:
            opts.add([(profile.id, Fraction(1))], "A",
                     f"nazwa obiektu w {where}: {', '.join(sorted(unique_hits))} (unikalny token) - {profile.name}",
                     score=SCORES["explicit"], tags=("explicit_identifier", *(f"unique_token:{t}" for t in
                                                                               sorted(unique_hits))),
                     hard=True, auto=True)
        else:
            opts.add([(profile.id, Fraction(1))], "B",
                     f"nazwa obiektu w {where} ({profile.name}), ale sprzeczność: {'; '.join(against)}",
                     score=SCORES["contradiction"], tags=("explicit_identifier", "contradiction"), contradiction=True)
        explicit_done = True
    elif len(named) > 1:
        _cascade(subject, [p for p, _, _ in named], ctx, opts, enumerated=True)
        explicit_done = True

    # correction: inherits the split of the corrected invoice unless its description names the object
    inherited = _inherited(subject, ctx)
    if inherited:
        if not explicit_done:
            opts.add(inherited, "B", "korekta dziedziczy podział faktury pierwotnej", score=SCORES["inherited"],
                     step="inherited", tags=("correction_inherits",), hard=True)
            explicit_done = True

    targets = ctx.targets()
    # collective line: a long window of the client that holds several objects
    if not explicit_done and subject.window and window_days(subject.window) >= COLLECTIVE_MIN_DAYS \
            and subject.client_tokens:
        inside = [p for p in targets if p.window and p.client_tokens & subject.client_tokens
                  and p.window[0] >= subject.window[0] and p.window[1] <= subject.window[1]]
        if len(inside) >= 2:
            _cascade(subject, inside, ctx, opts, enumerated=False)
            explicit_done = True

    if not explicit_done:
        # B: offer <-> invoice - the amount of an anchor (offer, earlier sales line) of one object
        if subject.direction == "sales" and subject.amount_minor:
            same = [p for p in targets for _, v, _ in ctx.offers.get(p.id, []) if v == abs(subject.amount_minor)]
            if len(set(p.id for p in same)) == 1:
                opts.add([(same[0].id, Fraction(1))], "B", f"kwota = oferta {same[0].name}",
                         score=SCORES["offer_match"], step="offer_match", tags=("offer_match",))
                explicit_done = True

    # a booking date says when the money moved, not when the service was: never a window signal
    service_window = subject.window if subject.window_source in ("text", "document", "record") else None
    if not explicit_done and service_window and window_days(service_window) <= SHORT_SERVICE_DAYS:
        # B: supplier + service period inside the window of one object
        def inside(tolerance: int) -> list[ObjectProfile]:
            out = []
            for p in targets:
                if not p.window:
                    continue
                if subject.window[0] < p.window[0] - timedelta(days=tolerance) or \
                        subject.window[1] > p.window[1] + timedelta(days=tolerance):
                    continue
                if subject.direction == "sales" and not (subject.client_tokens & p.client_tokens):
                    continue
                if subject.city_key and p.city_key and subject.city_key != p.city_key:
                    continue
                out.append(p)
            return out

        found = inside(0) or inside(1)
        weak = subject.subject_type == "transaction" and not (
            len(found) == 1 and subject.city_key and subject.city_key == found[0].city_key)
        if weak and found:
            # a date in a title may be the date of anything (a payment, a duplicate): a hint, not a signal B
            if len(found) == 1:
                opts.add([(found[0].id, Fraction(1))], "C",
                         f"data w tytule {_fmt_window(subject.window)} w oknie obiektu {found[0].name} "
                         "(tylko podpowiedź)",
                         score=SCORES["shared_people"], tags=("title_date",))
            found = []
        if len(found) == 1:
            p = found[0]
            city = bool(subject.city_key and subject.city_key == p.city_key)
            what = "okres usługi" if subject.window_source in ("text", "document") and window_days(
                subject.window) > 1 else "data"
            opts.add([(p.id, Fraction(1))], "B",
                     f"{what} {_fmt_window(subject.window)} w oknie jednego obiektu ({p.name})"
                     + (" i to samo miasto" if city else ""),
                     score=SCORES["window_city" if city else "window"], tags=("service_period",))
            explicit_done = True
        elif len(found) > 1:
            for p in found:
                opts.add([(p.id, Fraction(1))], "B",
                         f"okres {_fmt_window(subject.window)} w oknie {len(found)} obiektów - wybierz",
                         score=SCORES["tie"], tags=("service_period", "tie"))
            explicit_done = True

    # C: shared participants
    if subject.persons:
        sharing = [p for p in targets if p.persons & subject.persons]
        if len(sharing) == 1:
            opts.add([(sharing[0].id, Fraction(1))], "C", f"ta sama osoba w pliku wewnętrznym {sharing[0].name}",
                     score=SCORES["shared_people"], tags=("shared_people",))
    # D: the client alone / time proximity alone (narrowing only)
    if not explicit_done:
        narrowed: list[ObjectProfile] = []
        if subject.direction == "sales" and subject.client_tokens:
            narrowed = [p for p in targets if p.client_tokens & subject.client_tokens]
        elif service_window and window_days(service_window) <= SHORT_SERVICE_DAYS:
            narrowed = [p for p in targets if p.window and window_gap_days(p.window, service_window) <= 3]
        if 0 < len(narrowed) <= 3:
            for p in narrowed:
                why = "ten sam klient" if subject.direction == "sales" else "bliskość w czasie"
                opts.add([(p.id, Fraction(1))], "D", f"tylko zawężenie: {why} ({p.name})",
                         score=SCORES["client_only" if subject.direction == "sales" else "near"], tags=("narrowing",))
    return opts.signals


# ---------------------------------------------------------------------------
# Proposals (max. 3, rank 1 = best; D never rank 1, UNASSIGNED when nothing stronger)
# ---------------------------------------------------------------------------


def _d9(subject: Subject) -> bool:
    return subject.facts.get("op_type") in D9_OP_TYPES or subject.facts.get("intent") in D9_INTENTS


def proposals_for(subject: Subject, ctx: AllocationContext) -> list[Proposal]:
    signals = signals_for(subject, ctx)
    grouped: dict[int, list[AllocationSignal]] = defaultdict(list)
    for s in signals:
        grouped[s.option].append(s)
    options = []
    for group in grouped.values():
        head = group[0]
        options.append(Proposal(
            shares=[(s.cost_object_id, s.share) for s in group], strength=head.strength, cascade_step=head.cascade_step,
            score=head.score, facts=[head.fact_pl], tags=list(head.tags), hard=head.hard, partial=head.partial,
            auto=head.auto, rule=head.rule, contradiction=head.contradiction,
            badge_override="high" if "internal_file_assignment" in head.tags and head.strength == "A" else None,
        ))
    # identical splits: keep the stronger one
    unique: dict[tuple, Proposal] = {}
    for p in sorted(options, key=lambda p: -p.score):
        key = tuple(sorted((str(o), s) for o, s in p.shares))
        unique.setdefault(key, p)
    options = list(unique.values())
    strong = sorted([p for p in options if p.strength in ("A", "B")], key=lambda p: -p.score)
    hints = sorted([p for p in options if p.strength in ("C", "D")], key=lambda p: -p.score)
    u_id = ctx.unassigned_id
    d9 = _d9(subject)
    u_fact = ("ZUS i PIT od umów cywilnoprawnych w v1 → UNASSIGNED (D9)" if d9
              else "brak sygnału - koszt ogólny (UNASSIGNED to decyzja, nie brak decyzji)")
    ranked: list[Proposal]
    if strong:
        ranked = strong[:3]
        if len(ranked) < 3 and not any(p.object_ids == {u_id} for p in ranked):
            ranked.append(Proposal([(u_id, Fraction(1))], None, None, SCORES["unassigned_alt"],
                                   ["koszt ogólny, jeśli żaden obiekt"], ["unassigned"]))
        ranked += hints[: 3 - len(ranked)]
    else:
        u_score = SCORES["unassigned_hint" if hints else "unassigned_none"]
        tags = ["unassigned", *(["d9"] if d9 else [])]
        ranked = [Proposal([(u_id, Fraction(1))], None, None, u_score, [u_fact], tags)]
        ranked += hints[:2]
    return ranked[:3]


def badge(proposal: Proposal) -> str:
    if proposal.badge_override:
        return proposal.badge_override
    if proposal.score >= 0.95 and proposal.hard:
        return "high"
    if proposal.score >= 0.7:
        return "medium"
    return "low"


def share_rows(proposal: Proposal, amount_minor: int, ctx: AllocationContext) -> list[dict[str, Any]]:
    """Shares as stored in the proposal: 6 decimals summing to 1 (unless partial), exact amounts."""
    fractions = [s for _, s in proposal.shares]
    if proposal.partial:
        micro = [int((s * 1_000_000).numerator // (s * 1_000_000).denominator) for s in fractions]
        amounts = [int(Fraction(amount_minor) * s) for s in fractions]
    else:
        micro = distribute(1_000_000, fractions)
        amounts = distribute(int(amount_minor), fractions)
    rows = []
    for (cost_object_id, share), m, amount in zip(proposal.shares, micro, amounts, strict=True):
        rows.append({"cost_object_id": str(cost_object_id), "cost_object_name": ctx.name(cost_object_id),
                     "share": m / 1_000_000, "share_exact": str(share), "amount_minor": int(amount)})
    return rows


def precedent_count(subject: Subject, proposal: Proposal, ctx: AllocationContext) -> int:
    if not subject.counterparty_id or len(proposal.shares) != 1:
        return 0
    return ctx.precedents.get((subject.counterparty_id, proposal.shares[0][0]), 0)


def describe(proposals: list[Proposal]) -> Counter:
    return Counter(p.strength or "-" for p in proposals)
