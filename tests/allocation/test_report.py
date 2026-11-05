"""Phase 4B: company report - memorial and cash views, the control difference, one cost counted once."""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from kasa.core.allocation.decide import allocate_manually, apply_allocation
from kasa.core.allocation.run import run_allocation
from kasa.core.checks import check_allocation_shares
from kasa.core.decisions import log_decision
from kasa.core.reports.company import company_report, render
from kasa.core.reports.export import HEADER, export_csv
from tests.conftest import database_url_for, drop_database

from .helpers import (
    REPO,
    approve_all,
    click_ground_truth,
    event_map,
    ground_truth,
    minor,
    simulate_matches,
)
from .loaded_db import derived_name


def _full_month(conn, company, *, matches: str | None):
    approve_all(conn, company)
    events = event_map(conn, company)
    path = None
    if matches:
        path = simulate_matches(conn, company, use_matching_module=(matches == "4A"))
    run_allocation(conn, company)
    stats = click_ground_truth(conn, company, events)
    return events, stats, path


@pytest.mark.parametrize("matches", ["4A", "direct"])
def test_margins_per_event_equal_the_ground_truth_to_the_grosz(lconn, odm, matches):
    _, stats, path = _full_month(lconn, odm, matches=matches)
    assert path == matches
    # the person accepted the first proposal everywhere; the automation was never wrong
    assert stats.auto_wrong == [] and stats.manual == 0 and stats.other_rank == 0
    assert stats.rank1 > 50 and stats.auto_ok > 15
    report = company_report(lconn, odm.id, "2026-10")
    truth = ground_truth()
    for event in truth["events"]:
        line = report.object_line(event["name"])
        assert line is not None, event["name"]
        assert line.revenue_minor == minor(event["revenue_net_pln"]), event["id"]
        assert line.cost_minor == minor(event["cost_net_pln"]), event["id"]
        assert line.margin_minor == minor(event["margin_net_pln"]), event["id"]
        assert line.cash_minor == minor(event["cash_contribution_pln"]), event["id"]
        assert line.cost_pending_minor == 0 and line.revenue_pending_minor == 0
    expected = truth["metrics_expected"]["cash_report_2026_10"]
    assert report.cash["objects"] == minor(expected["events"])
    assert report.cash["unassigned"] == minor(expected["unassigned"])
    assert report.cash["unmatched"] == minor(expected["unmatched"])
    assert report.cash_control_minor == minor(expected["balance_change"])
    assert report.cash_difference_minor == minor(expected["control_difference"]) == 0
    assert report.control_source == "bank_balances" and report.cash["own"] == 0
    assert report.memorial["control"].revenue_minor == minor(truth["metrics_expected"]["revenue_memorial_net_2026_10"])
    assert report.memorial_difference_minor == 0
    assert check_allocation_shares(lconn, odm.id).ok
    text = render(report)
    assert "Różnica kontrolna" in text and "0,00" in text


def test_control_difference_is_zero_without_any_match(lconn, odm):
    approve_all(lconn, odm)
    before = company_report(lconn, odm.id, "2026-10")
    assert before.cash_difference_minor == 0 and before.memorial_difference_minor == 0
    assert before.cash["objects"] == 0 and before.cash["unassigned"] == 0
    assert before.cash["unmatched"] + before.cash["own"] == before.cash_control_minor
    run_allocation(lconn, odm)
    after = company_report(lconn, odm.id, "2026-10")
    assert after.cash_difference_minor == 0 and after.memorial_difference_minor == 0
    # own transfers (mBank <-> Millennium, VAT rebooking) net to zero in the month
    assert after.cash["own"] == 0
    # the memorial margins do not need the bank at all
    for event in ground_truth()["events"]:
        line = after.object_line(event["name"])
        assert line.revenue_minor + line.revenue_pending_minor == minor(event["revenue_net_pln"]), event["id"]


def test_one_cost_many_pieces_of_evidence_counts_once(lconn, odm):
    """Hostess 450 PLN: sheet row + BLIK; MC 1 800 PLN: bill PDF + sheet row + transfer - each counted once."""
    approve_all(lconn, odm)
    events = event_map(lconn, odm)
    run_allocation(lconn, odm)
    row = lconn.execute("SELECT id FROM l1_internal_record WHERE row_no = 6 AND superseded_by IS NULL").fetchone()[0]
    blik = lconn.execute("SELECT id FROM l1_bank_transaction WHERE entry_reference = %s",
                         ("202610126851950665",)).fetchone()[0]
    allocate_manually(lconn, odm.id, "internal_record", row, [(events["EV-01"], 1)], decided_by="test")
    # the BLIK title names the event (signal A), but before matching it is only a suggestion; a person accepts it
    top = lconn.execute("SELECT id, evidence FROM l2_suggestion WHERE subject_id = %s AND status = 'open' AND rank = 1",
                        (blik,)).fetchone()
    assert top[1]["auto_eligible"] is False and "awaiting_matching" in top[1]["tags"]
    apply_allocation(lconn, top[0], decided_by="test")
    before = company_report(lconn, odm.id, "2026-10").object_line("Gala ACME 2026")
    # 4A pairs the BLIK with the sheet row: the cash now follows the row, still once
    decision = log_decision(lconn, company_id=odm.id, kind="match", subject_type="transaction", subject_id=blik,
                            decided_by="test", source="test")
    lconn.execute("INSERT INTO l3_match (company_id, transaction_id, internal_record_id, amount_minor, decision_id, "
                  "source, method, created_by) VALUES (%s, %s, %s, 45000, %s, 'test', 'human', 'test')",
                  (odm.id, blik, row, decision))
    after = company_report(lconn, odm.id, "2026-10").object_line("Gala ACME 2026")
    assert after.cost_minor == before.cost_minor          # memorial: the row, once (a transfer is never a cost)
    assert after.cash_minor == before.cash_minor          # cash: the BLIK, once (match path wins over the direct one)
    # the MC: the PM row is evidence of his bill (same person, amount and date) - no suggestion, not a cost
    mc_row = lconn.execute("SELECT id FROM l1_internal_record WHERE row_no = 9 AND superseded_by IS NULL").fetchone()[0]
    assert lconn.execute("SELECT count(*) FROM l2_suggestion WHERE subject_id = %s", (mc_row,)).fetchone() == (0,)
    report = company_report(lconn, odm.id, "2026-10")
    links = {e["internal_record_id"]: e["reason"] for e in report.evidence_links}
    assert links == {str(mc_row): "same_person_amount_date"}
    # ...and the same when 4A spins all three: transfer -> bill and transfer -> row
    bill = lconn.execute("SELECT id FROM l1_document WHERE number_raw = %s AND superseded_by IS NULL",
                         ("4/2026",)).fetchone()[0]
    tx = lconn.execute("SELECT id FROM l1_bank_transaction WHERE entry_reference = '202610160785476080'").fetchone()[0]
    decision = log_decision(lconn, company_id=odm.id, kind="match", subject_type="transaction", subject_id=tx,
                            decided_by="test", source="test")
    group = uuid.uuid4()
    for column in ("document_id", "internal_record_id"):
        lconn.execute(
            sql.SQL("INSERT INTO l3_match (company_id, transaction_id, {}, amount_minor, group_id, decision_id, "
                    "source, method, created_by) VALUES (%s, %s, %s, 180000, %s, %s, 'test', 'human', 'test')").format(
                sql.Identifier(column)),
            (odm.id, tx, bill if column == "document_id" else mc_row, group, decision))
    linked = company_report(lconn, odm.id, "2026-10")
    assert {e["internal_record_id"]: e["reason"] for e in linked.evidence_links} == {str(mc_row): "l3_match"}
    gala = linked.object_line("Gala ACME 2026")
    assert gala.cost_minor == report.object_line("Gala ACME 2026").cost_minor
    assert linked.cash_difference_minor == 0


def test_export_for_the_accounting_office(lconn, odm):
    _full_month(lconn, odm, matches="direct")
    content = export_csv(lconn, odm.id, "2026-10")
    assert content.startswith("﻿")
    rows = list(csv.reader(io.StringIO(content.lstrip("﻿")), delimiter=";"))
    assert rows[0] == HEADER
    by_number = {r[2]: r for r in rows[1:] if r[0] in ("sprzedaż", "zakup")}
    collective = by_number["FV 10/10/2026"]
    assert collective[HEADER.index("alokacja")] == ("Roadshow Nordis Gdańsk: 0,200000 | Roadshow Nordis Wrocław: "
                                                    "0,300000 | Roadshow Nordis Łódź: 0,500000")
    assert collective[HEADER.index("status_alokacji")] == "człowiek"
    assert collective[HEADER.index("status_platnosci")] == "otwarta"
    assert by_number["SP/2026/10/087"][HEADER.index("status_alokacji")] == "reguła"
    assert by_number["SP/2026/10/087"][HEADER.index("status_platnosci")] == "zapłacona"
    assert by_number["FV 4/10/2026"][HEADER.index("memorialowo_pln")] == "12000,00"
    internal = [r for r in rows[1:] if r[0] == "koszt"]
    assert len(internal) == 19 and all(r[HEADER.index("kontrahent")].startswith("P-") for r in internal)


def test_cli_discover_allocate_and_report(loaded_url):
    copy = database_url_for(derived_name("alloc_cli"))
    drop_database(copy)
    source = conninfo_to_dict(loaded_url)["dbname"]
    admin = make_conninfo(loaded_url, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(conninfo_to_dict(copy)["dbname"]), sql.Identifier(source)))

    def kasa(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "kasa", "--database-url", copy, *args], cwd=REPO,
                              capture_output=True, text=True, timeout=300, check=False)

    try:
        out = kasa("cost-objects", "discover", "--company", "odm")
        assert out.returncode == 0, out.stderr
        assert "kandydaci nowi: 5" in out.stdout
        listing = kasa("cost-objects", "list", "--company", "odm", "--status", "candidate")
        assert listing.returncode == 0 and "Gala ACME 2026" in listing.stdout
        ids = [line.split()[0] for line in listing.stdout.splitlines()[1:]]
        assert len(ids) == 5
        show = kasa("cost-objects", "show", ids[0])
        assert show.returncode == 0 and "kotwica" in show.stdout
        for oid in ids:
            assert kasa("cost-objects", "approve", oid, "--by", "artur").returncode == 0
        dry = kasa("allocate", "run", "--company", "odm", "--dry-run")
        assert dry.returncode == 0 and "próba" in dry.stdout
        run = kasa("allocate", "run", "--company", "odm")
        assert run.returncode == 0 and "automat: " in run.stdout
        status = kasa("allocate", "status", "--company", "odm")
        assert status.returncode == 0 and "automat (reguła)" in status.stdout
        report = kasa("report", "company", "--company", "odm", "--month", "2026-10")
        assert report.returncode == 0, report.stdout + report.stderr
        assert "Różnica kontrolna" in report.stdout and "-27 347,45" in report.stdout
        export = kasa("report", "export", "--company", "odm", "--month", "2026-10")
        assert export.returncode == 0 and export.stdout.startswith("kierunek;rodzaj")
        check = kasa("check")
        assert check.returncode == 0, check.stdout
        assert "Kontrola prawdy 3: alokacje = 100%" in check.stdout
    finally:
        drop_database(copy)
