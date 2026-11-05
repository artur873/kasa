"""The whole ODM pipeline on a fresh database (phase 4 gate and the queue tests).

Exactly what the user runs: init-db (companies, sensitive words), bank ingest, documents ksef-sync /
import (JPK, PM sheet) / import-pdf, semantics run, control-set import-ground-truth, match calibrate +
run, cost-objects discover, allocate run, queue build. Committed once; tests open their own
transactions on it (rolled back) or work on a copy.

``click_queue`` is the user: it presses the buttons of the queue (``kasa.core.queue.actions.answer`` ->
``accept_suggestion`` / ``decide_other``) as the reference truth says - never ``apply_*`` directly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from kasa.core import control_sets as cs
from kasa.core import migrate
from kasa.core.allocation.run import run_allocation
from kasa.core.cost_objects_discovery.discover import discover
from kasa.core.extension import get_extension
from kasa.core.matching.calibrate import calibrate
from kasa.core.matching.run import run_matching
from kasa.core.providers.llm import MockLlm
from kasa.core.queue import actions, build
from kasa.core.queue.build import normalize_answer
from kasa.core.semantics.run import run_semantics
from kasa.core.sensitive import sync_sensitive_terms
from kasa.reference import ground_truth as reference
from tests.allocation.helpers import event_map, expected_allocations
from tests.allocation.helpers import ground_truth as load_truth
from tests.conftest import database_url_for, drop_database, recreate_database
from tests.semantics.helpers import load_month, odm_company

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
USER = "artur"


def derived_name(suffix: str) -> str:
    base = conninfo_to_dict(database_url_for())["dbname"]
    stem = base.removesuffix("_test")
    return f"{stem}_{suffix}_test"


def build_pipeline_database(dbname: str) -> str:
    url = database_url_for(dbname)
    recreate_database(url)
    migrate.migrate(url)
    with psycopg.connect(url) as conn:
        company = odm_company(conn)
        sync_sensitive_terms(conn, [(company.id, get_extension("odm").sensitive_words)])
        load_month(conn, company)
        run_semantics(conn, company, "all", provider=MockLlm())
        reference.import_ground_truth(conn, company, kind="transaction")
        reference.import_ground_truth(conn, company, kind="cost")
        set_id = cs.find_set(conn, "ground-truth-transaction")
        calibrate(conn, company.id, set_id)
        run_matching(conn, company, period=OCTOBER)
        discover(conn, company)
        run_allocation(conn, company)
        build.build(conn, company)
        conn.commit()
    return url


def copy_database(template_url: str, dbname: str) -> str:
    url = database_url_for(dbname)
    drop_database(url)
    template = conninfo_to_dict(template_url)["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(dbname),
                                                                        sql.Identifier(template)))
    return url


# ---------------------------------------------------------------------------
# the user
# ---------------------------------------------------------------------------


@dataclass
class ClickLog:
    cards: Counter = field(default_factory=Counter)
    buttons: Counter = field(default_factory=Counter)
    other: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    sentinels: int = 0
    rounds: int = 0
    allocation_runs: int = 0


def _label_for_match(labels: dict, subject_id: UUID) -> dict[str, Any] | None:
    return labels.get(("transaction", subject_id))


def _proposal_shares(proposal: dict[str, Any]) -> dict[UUID, Fraction]:
    return {UUID(s["cost_object_id"]): Fraction(s.get("share_exact") or str(s["share"]))
            for s in proposal.get("shares") or []}


def _same_shares(a: dict[UUID, Fraction], b: dict[UUID, Fraction]) -> bool:
    return set(a) == set(b) and all(abs(a[k] - b[k]) < Fraction(1, 100000) for k in a)


def _press_match(conn, item, card, label, log: ClickLog) -> None:
    want = normalize_answer(label)
    for proposal in card["proposals"]:
        if normalize_answer(proposal["proposal"]) == want:
            actions.answer(conn, item["id"], "accept", decided_by=USER, suggestion_id=UUID(proposal["suggestion_id"]))
            log.buttons["accept" if proposal["rank"] == 1 else "alternative"] += 1
            return
    status = label["status"]
    extra: dict[str, Any] = {"kind": status}
    if status == "matched":
        extra["targets"] = [{{"document": "document_id", "internal_record": "internal_record_id",
                              "transaction": "related_transaction_id"}[d["type"]]: d["id"]} for d in label["documents"]]
    if status == "no_document":
        extra["subtype"] = label.get("subtype") or "other"
    actions.answer(conn, item["id"], "other", decided_by=USER, extra=extra)
    log.buttons["other"] += 1
    log.other.append(f"match {card['subject'].get('title')}: {status}")


def _press_allocation(conn, item, card, want: dict[UUID, Fraction] | None, log: ClickLog) -> None:
    if want is None:
        log.unexpected.append(f"allocation {item['subject_type']}:{item['subject_id']} bez etykiety")
        actions.answer(conn, item["id"], "defer", decided_by=USER)
        return
    for proposal in card["proposals"]:
        if _same_shares(_proposal_shares(proposal["proposal"]), want) and not proposal["partial"]:
            actions.answer(conn, item["id"], "accept", decided_by=USER, suggestion_id=UUID(proposal["suggestion_id"]))
            log.buttons["accept" if proposal["rank"] == 1 else "alternative"] += 1
            return
    shares = [{"cost_object_id": str(k), "share_exact": str(v), "share": float(v)} for k, v in want.items()]
    actions.answer(conn, item["id"], "other", decided_by=USER, extra={"kind": "allocation", "shares": shares})
    log.buttons["other"] += 1
    log.other.append(f"allocation {item['subject_type']} {card['subject'].get('title')}")


def answer_match_cards(conn: psycopg.Connection, company, count: int) -> list[UUID]:
    """The person answers the first ``count`` match cards of the queue as the reference truth says (the
    transfers she has decided herself - the only source of sentinels). Returns the decided transfers."""
    truth = load_truth()
    labels = reference.expected_labels(conn, company.id, truth, "transaction")
    log = ClickLog()
    decided: list[UUID] = []
    for item in actions.list_items(conn, company.id, include_deferred=False):
        if len(decided) >= count:
            break
        if item["card_kind"] != "match" or ("transaction", item["subject_id"]) not in labels:
            continue
        card = actions.card(conn, item["id"])
        _press_match(conn, item, card, labels[("transaction", item["subject_id"])], log)
        decided.append(item["subject_id"])
    return decided


def click_queue(conn: psycopg.Connection, company, *, max_cards: int = 600) -> ClickLog:
    """Answer the whole queue as the reference truth says (cards in the queue's own order)."""
    truth = load_truth()
    labels = reference.expected_labels(conn, company.id, truth, "transaction")
    log = ClickLog()
    expected: dict | None = None
    skipped: list[UUID] = []
    while log.rounds < 6:
        log.rounds += 1
        while sum(log.cards.values()) < max_cards:
            item = actions.next_item(conn, company.id, skip=skipped)
            if item is None:
                break
            card = actions.card(conn, item["id"])
            kind = item["card_kind"]
            log.cards[kind] += 1
            if kind in ("match", "sentinel"):
                if kind == "sentinel":
                    log.sentinels += 1
                label = _label_for_match(labels, item["subject_id"])
                if label is None:
                    log.unexpected.append(f"match bez etykiety {item['subject_id']}")
                    skipped.append(item["id"])
                    continue
                _press_match(conn, item, card, label, log)
            elif kind == "cost_object_candidate":
                actions.answer(conn, item["id"], "approve", decided_by=USER)
                log.buttons["approve"] += 1
                expected = None
            elif kind == "allocation":
                if expected is None:
                    expected = expected_allocations(conn, company, event_map(conn, company), truth)
                _press_allocation(conn, item, card, expected.get((item["subject_type"], item["subject_id"])), log)
            elif kind == "sensitive":
                actions.answer(conn, item["id"], "accept", decided_by=USER, extra={"category": "Inne / Inne / do wyjaśnienia"})
                log.buttons["category"] += 1
            else:
                log.unexpected.append(f"{kind} {item['subject_id']}")
                skipped.append(item["id"])
        # the system after the person's decisions: allocations refreshed (objects approved, statuses known)
        run_allocation(conn, company)
        log.allocation_runs += 1
        build.build(conn, company)
        expected = None
        if actions.next_item(conn, company.id, mark_shown=False, skip=skipped) is None:
            break
    return log


_BUILT: dict[str, str] = {}


def pipeline_database() -> str:
    """The pipeline database of this test session (built once, shared by tests/queue, tests/rules, gate 4)."""
    name = derived_name("pipeline")
    if name not in _BUILT:
        _BUILT[name] = build_pipeline_database(name)
    return _BUILT[name]
