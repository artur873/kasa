"""Phase 5 test databases: the phase 4 pipeline (``tests/queue/pipeline.py``) after the person's clicks.

* ``clicked_database()`` - the pipeline copy after ``click_queue`` (the whole queue answered as the reference
  truth says), the truth checks of October recorded (as after every run) and the metric definitions synced;
* ``partial_database()`` - the pipeline copy where the person only approved the five cost object candidates on
  their cards (the automation of signal A ran): the rest of the costs still waits in open suggestions.

Both are built once per test session (committed); tests open rolled-back transactions on them, the agent
writes its log and proposals there (never asserted as absolute counts).
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

import psycopg

from kasa.core.allocation.run import run_allocation
from kasa.core.checks import (
    check_allocation_shares,
    check_ksef_completeness,
    check_match_coverage,
    run_truth_checks,
)
from kasa.core.companies import get_company
from kasa.core.metrics.registry import sync_definitions
from kasa.core.providers.ksef import MockKsef
from kasa.core.queue import actions, build
from tests.queue.pipeline import USER, click_queue, copy_database, derived_name, pipeline_database

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
TRUTH_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "odm" / "ground_truth.json"


@cache
def truth() -> dict[str, Any]:
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8"))


def minor(text: str) -> int:
    whole, _, cents = text.replace("-", "").partition(".")
    value = int(whole) * 100 + int((cents + "00")[:2])
    return -value if text.startswith("-") else value


def record_truth_checks(conn: psycopg.Connection, company) -> None:
    run_truth_checks(conn, company.id, *OCTOBER)
    check_match_coverage(conn, company.id, *OCTOBER)
    check_allocation_shares(conn, company.id, *OCTOBER)
    check_ksef_completeness(conn, company, MockKsef(), date(2026, 9, 1), OCTOBER[1])


@cache
def clicked_database() -> str:
    url = copy_database(pipeline_database(), derived_name("phase5"))
    with psycopg.connect(url) as conn:
        company = get_company(conn, "odm")
        click_queue(conn, company)
        record_truth_checks(conn, company)
        sync_definitions(conn)
        conn.commit()
    return url


@cache
def partial_database() -> str:
    url = copy_database(pipeline_database(), derived_name("phase5_partial"))
    with psycopg.connect(url) as conn:
        company = get_company(conn, "odm")
        for item in actions.list_items(conn, company.id):
            if item["card_kind"] == "cost_object_candidate":
                actions.answer(conn, item["id"], "approve", decided_by=USER)
        run_allocation(conn, company)
        build.build(conn, company)
        record_truth_checks(conn, company)
        sync_definitions(conn)
        conn.commit()
    return url
