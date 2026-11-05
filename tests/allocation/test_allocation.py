"""Phase 4B: allocation signals A-D, the cascade of a collective invoice, automation, decisions."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kasa.core.allocation.auto import BUILTIN_RULE
from kasa.core.allocation.decide import (
    AllocationError,
    allocate_manually,
    apply_allocation,
)
from kasa.core.allocation.run import run_allocation
from kasa.core.checks import check_allocation_shares
from kasa.core.extension import AllocationSpec, get_extension
from kasa.core.normalize import normalize_invoice_number

from .helpers import approve_all, event_map, ground_truth


@pytest.fixture
def approved(lconn, odm):
    approve_all(lconn, odm)
    return event_map(lconn, odm)


def _line(conn, number: str, line_no: int = 1):
    return conn.execute(
        "SELECT l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id "
        "WHERE d.number_raw = %s AND d.superseded_by IS NULL AND l.line_no = %s ORDER BY d.direction LIMIT 1",
        (number, line_no),
    ).fetchone()[0]


def _tx(conn, entry_reference: str):
    return conn.execute("SELECT id FROM l1_bank_transaction WHERE entry_reference = %s AND superseded_by IS NULL",
                        (entry_reference,)).fetchone()[0]


def _row(conn, sheet_row: int):
    return conn.execute("SELECT id FROM l1_internal_record WHERE row_no = %s AND superseded_by IS NULL",
                        (sheet_row,)).fetchone()[0]


def _suggestions(conn, subject_id, status: str = "open"):
    return conn.execute(
        "SELECT id, rank, proposal, evidence, score FROM l2_suggestion WHERE kind = 'allocation' AND subject_id = %s "
        "AND status = %s ORDER BY rank", (subject_id, status),
    ).fetchall()


def _shares(proposal) -> dict:
    return {s["cost_object_name"]: Fraction(s["share_exact"]) for s in proposal["shares"]}


def _active(conn, subject_id):
    return conn.execute(
        "SELECT cost_object_id, share, method::text, signal_strength, cascade_step, rule_id, decision_id "
        "FROM l3_allocation WHERE subject_id = %s AND status = 'active' ORDER BY share DESC", (subject_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_signals_a_b_c_d_on_the_fixtures(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    # A: the name of the event in the line (unique token), no contradiction
    [(_, _rank, proposal, evidence, _score)] = _suggestions(lconn, _line(lconn, "2026/10/0193"))[:1]
    assert proposal["signal_strength"] == "A" and evidence["badge"] == "high" and evidence["hard"]
    assert _shares(proposal) == {"Gala ACME 2026": 1}
    assert "explicit_identifier" in evidence["tags"] and evidence["auto_eligible"]
    # B: supplier + service period inside the window of one event (hotel in Kraków 08-10.10, no name)
    for line_no in (1, 2):
        top = _suggestions(lconn, _line(lconn, "FV/2026/10/0412", line_no))[0]
        assert top[2]["signal_strength"] == "B" and _shares(top[2]) == {"Gala ACME 2026": 1}
        assert top[3]["badge"] == "medium" and not top[3]["auto_eligible"]
    hall = _suggestions(lconn, _line(lconn, "2026/10/27"))[0]                      # hall in Wrocław, 17.10
    assert hall[2]["signal_strength"] == "B" and _shares(hall[2]) == {"Roadshow Nordis Wrocław": 1}
    fuel = _suggestions(lconn, _line(lconn, "028417"))[0]                          # receipt: Łódź, 24.10
    assert _shares(fuel[2]) == {"Roadshow Nordis Łódź": 1} and not fuel[3]["auto_eligible"]
    # A from the PM sheet: high badge, but not hard evidence (D2) - never automatic
    row = _suggestions(lconn, _row(lconn, 2))[0]
    assert row[2]["signal_strength"] == "A" and row[3]["badge"] == "high"
    assert row[3]["hard"] is False and row[3]["auto_eligible"] is False
    assert _shares(row[2]) == {"Roadshow Nordis Gdańsk": 1}
    # C: a person of the internal files (the PM who bought the flowers is also on the payroll)
    salary = _suggestions(lconn, _tx(lconn, "202610097692101583"))
    assert salary[0][2]["shares"][0]["cost_object_name"] == "UNASSIGNED"
    assert [s[2]["signal_strength"] for s in salary[1:]] == ["C"]
    # D: narrowing only - never rank 1, never a high badge
    d_rows = lconn.execute(
        "SELECT rank, evidence ->> 'badge' FROM l2_suggestion WHERE company_id = %s AND kind = 'allocation' "
        "AND proposal ->> 'signal_strength' = 'D'", (odm.id,)).fetchall()
    assert d_rows, "na danych testowych jest sygnał D (sprzedaż wrześniowa do klienta eventów)"
    assert all(rank > 1 and badge != "high" for rank, badge in d_rows)


def test_no_signal_is_unassigned_rank_one(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    # the purchase line of the office rent (the same number as a sale to ACME)
    rent = lconn.execute(
        "SELECT l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id WHERE d.number_raw = "
        "'FV 7/10/2026' AND d.direction = 'purchase' AND d.superseded_by IS NULL").fetchone()[0]
    for subject in (rent, _line(lconn, "5384029117"), _line(lconn, "INV-2610-0437"), _line(lconn, "8F2C41A0-0007")):
        top = _suggestions(lconn, subject)[0]
        assert top[1] == 1 and _shares(top[2]) == {"UNASSIGNED": 1}, top[2]
        assert top[2]["signal_strength"] is None and not top[3]["auto_eligible"]
    zus = _suggestions(lconn, _tx(lconn, "202610157109429897"))[0]
    assert _shares(zus[2]) == {"UNASSIGNED": 1}
    assert any("D9" in f for f in zus[3]["facts"]) and "d9" in zus[3]["tags"]


def test_collective_invoice_split_by_person_days_as_in_ground_truth(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    truth = next(i for i in ground_truth()["invoices"] if i["invoice_number"] == "FV 10/10/2026")
    expected = {s["event_name"]: Fraction(s["share_exact"]) for s in truth["suggested_allocation"]}
    top = _suggestions(lconn, _line(lconn, "FV 10/10/2026"))
    assert top[0][1] == 1 and _shares(top[0][2]) == expected           # 20 / 30 / 50
    assert top[0][2]["cascade_step"] == "person_days" and top[0][2]["signal_strength"] == "B"
    assert not top[0][3]["partial"] and not top[0][3]["auto_eligible"]
    assert sum(s["share"] for s in top[0][2]["shares"]) == pytest.approx(1.0, abs=1e-9)
    assert sum(s["amount_minor"] for s in top[0][2]["shares"]) == 5_000_000
    # the design for three roadshows: the description names them -> equal split, only by a person
    design = _suggestions(lconn, _line(lconn, "FV 12/09/2026"))
    truth = next(i for i in ground_truth()["invoices"] if i["invoice_number"] == "FV 12/09/2026")
    assert _shares(design[0][2]) == {s["event_name"]: Fraction(s["share_exact"]) for s in truth["suggested_allocation"]}
    assert design[0][2]["cascade_step"] == "line_description"
    assert design[1][2]["cascade_step"] == "person_days"


def _proforma(conn, company_id, number: str, name: str, net: int) -> None:
    buyer = conn.execute("SELECT counterparty_id, buyer_nip, buyer_name FROM l1_document WHERE number_raw = "
                         "'FV 10/10/2026'").fetchone()
    doc = conn.execute(
        """
        INSERT INTO l1_document (company_id, direction, number_raw, number_normalized, kind, issue_date, buyer_nip,
                                 buyer_name, counterparty_id, currency, net_minor, vat_minor, gross_minor, source,
                                 method, created_by)
        VALUES (%s, 'sales', %s, %s, 'proforma', DATE '2026-09-20', %s, %s, %s, 'PLN', %s, 0, %s,
                'test', 'code', 'test')
        RETURNING id
        """,
        (company_id, number, normalize_invoice_number(number), buyer[1], buyer[2], buyer[0], net, net),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO l1_document_line (company_id, document_id, line_no, name, net_minor, gross_minor, source, method, "
        "created_by) VALUES (%s, %s, 1, %s, %s, %s, 'test', 'code', 'test')",
        (company_id, doc, name, net, net),
    )


def test_partial_proposal_when_an_offer_is_missing(lconn, odm, approved):
    """Offers for two of three events: 20/30 and the rest to fill in - nothing is split "by eye"."""
    _proforma(lconn, odm.id, "PRO 1/2026", "Oferta - Roadshow Nordis Gdańsk 03.10.2026", 1_000_000)
    _proforma(lconn, odm.id, "PRO 2/2026", "Oferta - Roadshow Nordis Wrocław 17.10.2026", 1_500_000)
    run_allocation(lconn, odm, auto=False)
    subject = _line(lconn, "FV 10/10/2026")
    top = _suggestions(lconn, subject)[0]
    assert top[2]["cascade_step"] == "offer_value" and top[3]["partial"] is True
    assert _shares(top[2]) == {"Roadshow Nordis Gdańsk": Fraction(1, 5), "Roadshow Nordis Wrocław": Fraction(3, 10)}
    assert sum(s["share"] for s in top[2]["shares"]) == pytest.approx(0.5)
    assert any("reszta do uzupełnienia" in f for f in top[3]["facts"])
    with pytest.raises(AllocationError, match="częściowa"):
        apply_allocation(lconn, top[0], decided_by="artur")
    complete = [*top[2]["shares"], {"cost_object_id": str(approved["EV-04"]), "share_exact": "1/2"}]
    apply_allocation(lconn, top[0], decided_by="artur", shares=complete)
    shares = {r[0]: r[1] for r in _active(lconn, subject)}
    assert shares == {approved["EV-02"]: Decimal("0.200000"), approved["EV-03"]: Decimal("0.300000"),
                      approved["EV-04"]: Decimal("0.500000")}


def test_correction_inherits_the_split_unless_its_description_names_the_event(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    collective = _line(lconn, "FV 10/10/2026")
    apply_allocation(lconn, _suggestions(lconn, collective)[0][0], decided_by="artur")
    parent = lconn.execute("SELECT id, counterparty_id, buyer_nip FROM l1_document WHERE number_raw = 'FV 10/10/2026'"
                           ).fetchone()
    doc = lconn.execute(
        """
        INSERT INTO l1_document (company_id, direction, number_raw, number_normalized, kind, issue_date, sale_date,
                                 buyer_nip, counterparty_id, currency, net_minor, vat_minor, gross_minor,
                                 corrects_document_id, source, method, created_by)
        VALUES (%s, 'sales', 'KOR 2/10/2026', 'KOR2102026', 'correction', DATE '2026-10-31', DATE '2026-10-31',
                %s, %s, 'PLN', -500000, -115000, -615000, %s, 'test', 'code', 'test') RETURNING id
        """,
        (odm.id, parent[2], parent[1], parent[0]),
    ).fetchone()[0]
    lconn.execute("INSERT INTO l1_document_line (company_id, document_id, line_no, name, net_minor, gross_minor, "
                  "line_kind, source, method, created_by) VALUES (%s, %s, 1, 'Rabat za terminową płatność', -500000, "
                  "-615000, 'correction', 'test', 'code', 'test')", (odm.id, doc))
    run_allocation(lconn, odm, auto=False)
    line = lconn.execute("SELECT id FROM l1_document_line WHERE document_id = %s", (doc,)).fetchone()[0]
    top = _suggestions(lconn, line)[0]
    assert top[2]["cascade_step"] == "inherited"
    assert _shares(top[2]) == {"Roadshow Nordis Gdańsk": Fraction(1, 5), "Roadshow Nordis Wrocław": Fraction(3, 10),
                               "Roadshow Nordis Łódź": Fraction(1, 2)}
    assert sum(s["amount_minor"] for s in top[2]["shares"]) == -500_000
    # KOR 1/10/2026 names Roadshow Nordis Gdańsk in its line: the description wins (signal A)
    kor = lconn.execute("SELECT l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id "
                        "WHERE d.number_raw = 'KOR 1/10/2026'").fetchone()[0]
    kor_top = _suggestions(lconn, kor)[0]
    assert kor_top[2]["signal_strength"] == "A" and _shares(kor_top[2]) == {"Roadshow Nordis Gdańsk": 1}


# ---------------------------------------------------------------------------
# automation
# ---------------------------------------------------------------------------


def test_automation_only_for_signal_a_and_never_for_the_pm_sheet(lconn, odm, approved):
    report = run_allocation(lconn, odm)
    assert report.auto["applied"] > 0 and not report.auto["failed"]
    rule = lconn.execute("SELECT id, version, stage::text, method::text, created_by FROM l3_rule "
                         "WHERE company_id = %s AND rule_key = %s", (odm.id, BUILTIN_RULE)).fetchone()
    assert rule[2:] == ("auto", "human", "briefing-2026-11-03")
    rows = lconn.execute(
        "SELECT subject_type::text, signal_strength, rule_id, rule_version, count(*) FROM l3_allocation "
        "WHERE company_id = %s AND status = 'active' AND method = 'rule' GROUP BY 1, 2, 3, 4", (odm.id,)).fetchall()
    assert {r[1] for r in rows} == {"A"}
    assert {(r[2], r[3]) for r in rows} == {(rule[0], rule[1])}
    assert "internal_record" not in {r[0] for r in rows}, "arkusz PM nie jest twardym dowodem (D2)"
    log = lconn.execute(
        "SELECT count(*) FROM l3_decision_log WHERE company_id = %s AND kind = 'allocation' AND method = 'rule' "
        "AND rule_id = %s", (odm.id, rule[0])).fetchone()[0]
    assert log == sum(r[4] for r in rows)        # one decision per subject (one object each)
    # B, cascade and UNASSIGNED stay in the queue
    for number in ("FV/2026/10/0412", "FV 10/10/2026", "FV 12/09/2026", "2026/10/27", "028417"):
        assert _active(lconn, _line(lconn, number)) == [], number
    # nothing automatic is against the ground truth (expected_event = id of the event for signal A)
    by_number = {i["invoice_number"]: i for i in ground_truth()["invoices"]}
    autos = lconn.execute(
        "SELECT d.number_raw, d.direction::text, a.cost_object_id FROM l3_allocation a "
        "JOIN l1_document_line l ON l.id = a.subject_id JOIN l1_document d ON d.id = l.document_id "
        "WHERE a.company_id = %s AND a.method = 'rule' AND a.status = 'active'", (odm.id,)).fetchall()
    for number, direction, cost_object_id in autos:
        truth = by_number.get(number)
        if truth and truth["direction"] == direction:
            assert truth["expected_event"] != "queue", number
            assert approved[truth["expected_event"]] == cost_object_id, number
    # a second run changes nothing
    again = run_allocation(lconn, odm)
    assert again.suggested == 0 and again.auto["applied"] == 0


def test_pm_sheet_becomes_automatic_only_when_the_extension_says_so(lconn, odm, approved, monkeypatch):
    extension = get_extension("odm")
    monkeypatch.setattr(extension, "allocation", AllocationSpec(internal_record_is_hard=True,
                                                                person_day_settlement_types=["umowa zlecenie"]))
    monkeypatch.setattr("kasa.core.allocation.run.get_extension", lambda slug: extension)
    run_allocation(lconn, odm)
    rows = lconn.execute("SELECT count(*) FROM l3_allocation WHERE company_id = %s AND status = 'active' "
                         "AND subject_type = 'internal_record' AND method = 'rule'", (odm.id,)).fetchone()[0]
    assert rows == 19          # 20 rows, the MC's row is evidence of his bill (one cost)


def test_automation_stops_when_the_builtin_rule_is_suspended(lconn, odm, approved):
    lconn.execute("UPDATE l3_rule SET stage = 'suspended', method = 'human', updated_by = 'artur' "
                  "WHERE company_id = %s AND rule_key = %s", (odm.id, BUILTIN_RULE))
    report = run_allocation(lconn, odm)
    assert report.auto["applied"] == 0 and report.auto["skipped_no_rule"] > 0
    assert lconn.execute("SELECT count(*) FROM l3_allocation WHERE company_id = %s AND method = 'rule'",
                         (odm.id,)).fetchone() == (0,)


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


def test_apply_allocation_writes_the_contract(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    subject = _line(lconn, "FV/2026/10/0412")
    sugs = _suggestions(lconn, subject)
    with pytest.raises(AllocationError):
        apply_allocation(lconn, sugs[0][0], decided_by="kasa", method="code")
    decision = apply_allocation(lconn, sugs[0][0], decided_by="artur", reason="hotel Gali")
    [row] = _active(lconn, subject)
    assert row[:5] == (approved["EV-01"], Decimal("1.000000"), "human", "B", None)
    assert row[6] == decision
    log = lconn.execute("SELECT kind::text, method::text, suggestion_id, payload FROM l3_decision_log WHERE id = %s",
                        (decision,)).fetchone()
    assert log[:3] == ("allocation", "human", sugs[0][0])
    assert log[3]["signal_strength"] == "B" and log[3]["shares"][0]["amount_minor"] == 1_176_000
    statuses = dict(lconn.execute("SELECT id, status::text FROM l2_suggestion WHERE subject_id = %s",
                                  (subject,)).fetchall())
    assert statuses[sugs[0][0]] == "accepted"
    assert all(statuses[s[0]] == "rejected" for s in sugs[1:])
    # a person changes their mind: the old allocation is revoked by the new decision, nothing is deleted
    new = allocate_manually(lconn, odm.id, "line", subject, [(approved["UNASSIGNED"], 1)], decided_by="artur")
    rows = lconn.execute("SELECT status::text, revoked_by_decision FROM l3_allocation WHERE subject_id = %s "
                         "ORDER BY created_at", (subject,)).fetchall()
    assert rows == [("revoked", new), ("active", None)]
    # an allocation to a candidate (not approved) is refused
    candidate = lconn.execute(
        "INSERT INTO l3_cost_object (company_id, kind, name, attrs, status, source, method, created_by) VALUES "
        "(%s, 'event', 'X', %s, 'candidate', 'test', 'code', 'test') RETURNING id",
        (odm.id, Jsonb({"name": "X", "client": "Y", "date_start": "2026-10-01", "date_end": "2026-10-01"})),
    ).fetchone()[0]
    with pytest.raises(AllocationError, match="zatwierdź"):
        allocate_manually(lconn, odm.id, "line", subject, [(candidate, 1)], decided_by="artur")


def test_automation_never_replaces_a_human_allocation(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    subject = _line(lconn, "2026/10/0193")
    allocate_manually(lconn, odm.id, "line", subject, [(approved["EV-01"], Fraction(1, 2)),
                                                        (approved["UNASSIGNED"], Fraction(1, 2))], decided_by="artur")
    run_allocation(lconn, odm)
    rows = _active(lconn, subject)
    assert {r[2] for r in rows} == {"human"} and len(rows) == 2


def test_truth_check_3_shares_sum_to_one(lconn, odm, approved):
    run_allocation(lconn, odm)
    run_allocation(lconn, odm, auto=False)
    subject = _line(lconn, "FV 12/09/2026")
    apply_allocation(lconn, _suggestions(lconn, subject)[0][0], decided_by="artur")     # 1/3 x 3
    shares = [r[1] for r in _active(lconn, subject)]
    assert sum(shares) == Decimal("1.000000") and sorted(shares) == [Decimal("0.333333")] * 2 + [Decimal("0.333334")]
    result = check_allocation_shares(lconn, odm.id)
    assert result.ok and result.detail["subjects"] > 0
    assert lconn.execute("SELECT ok FROM truth_check_result WHERE company_id = %s AND check_name = "
                         "'allocation_shares' ORDER BY run_at DESC LIMIT 1", (odm.id,)).fetchone() == (True,)
    with lconn.transaction(force_rollback=True):
        decision = lconn.execute("SELECT decision_id FROM l3_allocation WHERE subject_id = %s LIMIT 1",
                                 (subject,)).fetchone()[0]
        lconn.execute("UPDATE l3_allocation SET share = 0.2, method = 'human' WHERE subject_id = %s AND share = "
                      "0.333334", (subject,))
        broken = check_allocation_shares(lconn, odm.id, record=False)
        assert not broken.ok and broken.detail["violations"][0]["subject_id"] == str(subject)
        assert decision


def test_allocation_error_on_wrong_method(lconn, odm, approved):
    run_allocation(lconn, odm, auto=False)
    sug = _suggestions(lconn, _line(lconn, "FV/2026/10/0412"))[0][0]
    with pytest.raises(AllocationError):
        apply_allocation(lconn, sug, decided_by="kasa", method="rule")      # no rule given
    rule = lconn.execute("SELECT id FROM l3_rule WHERE company_id = %s AND rule_key = %s",
                         (odm.id, BUILTIN_RULE)).fetchone()[0]
    with pytest.raises(psycopg.Error):
        with lconn.transaction():
            apply_allocation(lconn, sug, decided_by="kasa", method="rule", rule_id=rule, rule_version=99)
