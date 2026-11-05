"""Phase 4B: cost object candidates discovered from the data (briefing, step 5)."""

from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kasa.core.cost_objects_discovery.decide import (
    approve_candidate,
    mark_distinct,
    merge_candidates,
    reject_candidate,
    split_candidate,
)
from kasa.core.cost_objects_discovery.discover import discover, pair_verdicts
from kasa.core.cost_objects_discovery.pairs import LEVELS, load_model
from kasa.core.extension import CandidateRecord, get_extension

from .helpers import ground_truth


def _candidates(conn, company):
    return conn.execute(
        "SELECT id, name, attrs, status::text, method::text FROM l3_cost_object "
        "WHERE company_id = %s AND system_code IS NULL ORDER BY name", (company.id,)
    ).fetchall()


def _members(conn, cost_object_id):
    return conn.execute(
        "SELECT id, record_type::text, record_id, is_anchor, status::text FROM l2_cost_object_member "
        "WHERE candidate_id = %s ORDER BY is_anchor DESC, link_strength DESC, record_id", (cost_object_id,)
    ).fetchall()


def _counts(conn, company):
    return conn.execute(
        """
        SELECT (SELECT count(*) FROM l3_cost_object WHERE company_id = %(c)s),
               (SELECT count(*) FROM l2_cost_object_member WHERE company_id = %(c)s),
               (SELECT count(*) FROM l2_suggestion WHERE company_id = %(c)s AND kind = 'cost_object_candidate'),
               (SELECT count(*) FROM l2_suggestion WHERE company_id = %(c)s AND kind = 'cost_object_candidate'
                  AND status = 'open')
        """,
        {"c": company.id},
    ).fetchone()


def _line_of(conn, number: str):
    return conn.execute(
        "SELECT l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id "
        "WHERE d.number_raw = %s AND d.superseded_by IS NULL ORDER BY l.line_no LIMIT 1", (number,)
    ).fetchone()[0]


def test_candidates_cover_the_events_of_the_ground_truth(lconn, odm):
    report = discover(lconn, odm, created_by="test")
    rows = _candidates(lconn, odm)
    assert len(report.created) >= 5
    assert all(r[3] == "candidate" and r[4] == "code" for r in rows)
    by_name = {r[1]: r for r in rows}
    matched = {}
    for event in ground_truth()["events"]:
        row = by_name.get(event["name"])
        assert row is not None, f"brak kandydata dla {event['id']} {event['name']}: {sorted(by_name)}"
        attrs = row[2]
        assert attrs["client"] == event["client"]
        assert (attrs["date_start"], attrs["date_end"]) == (event["date_from"], event["date_to"])
        assert attrs["city"] == event["city"]
        matched[event["id"]] = row[0]
    # nothing else looks like an event in October
    assert len(rows) == 5, [r[1] for r in rows]


def test_card_has_the_format_of_the_contract(lconn, odm):
    discover(lconn, odm, created_by="test")
    rows = lconn.execute(
        """
        SELECT s.subject_type::text, s.subject_id, s.proposal, s.evidence, s.method::text, s.rank, co.status::text
          FROM l2_suggestion s JOIN l3_cost_object co ON co.id = s.subject_id
         WHERE s.company_id = %s AND s.kind = 'cost_object_candidate' AND s.status = 'open'
        """,
        (odm.id,),
    ).fetchall()
    assert len(rows) == 5
    for subject_type, _, proposal, evidence, method, rank, status in rows:
        assert (subject_type, method, rank, status) == ("cost_object", "code", 1, "candidate")
        assert {"name", "attrs", "anchor", "members", "weakest_link", "revenue_minor", "cost_minor"} <= proposal.keys()
        assert proposal["anchor"]["record_type"] in ("document_line", "internal_record")
        assert all(0 <= m["link_strength"] <= 1 for m in proposal["members"])
        assert proposal["weakest_link"] == min(m["link_strength"] for m in proposal["members"])
        assert {m["record_type"] for m in proposal["members"]} - {proposal["anchor"]["record_type"]}, \
            "kotwica + co najmniej jeden rekord innego typu"
        assert {"facts", "badge", "hard", "tags"} <= evidence.keys()
    gala = next(r for r in rows if r[2]["name"] == "Gala ACME 2026")[2]
    # revenue of the members: FV 3/10 (ROZ = the whole order 80 000), FV 7/10 (10 000), the advance = 0
    assert gala["revenue_minor"] == 9_000_000
    # the bill of the MC and its PM sheet row are one cost (counted once on the card)
    assert gala["cost_minor"] == 6_015_640


def test_collective_invoice_is_never_an_anchor_nor_a_member(lconn, odm):
    report = discover(lconn, odm, created_by="test")
    collective = _line_of(lconn, "FV 10/10/2026")         # "Obsługa eventów - październik 2026", three roadshows
    design = _line_of(lconn, "FV 12/09/2026")             # one design for Gdańsk, Wrocław and Łódź
    members = {r[0] for r in lconn.execute("SELECT record_id FROM l2_cost_object_member WHERE company_id = %s",
                                           (odm.id,)).fetchall()}
    assert collective not in members and design not in members
    skipped = {s["record_id"] for s in report.skipped_collective}
    assert {str(collective), str(design)} <= skipped


def test_two_offers_of_the_same_client_with_other_names_and_dates_do_not_merge(lconn, odm):
    discover(lconn, odm, created_by="test")
    nordis = [r for r in _candidates(lconn, odm) if r[2]["client"].startswith("Nordis")]
    assert sorted(r[2]["city"] for r in nordis) == ["Gdańsk", "Wrocław", "Łódź"]
    assert len({(r[2]["date_start"], r[2]["date_end"]) for r in nordis}) == 3
    # no record sits in two candidates
    shared = lconn.execute(
        "SELECT record_id, count(*) FROM l2_cost_object_member WHERE company_id = %s GROUP BY 1 HAVING count(*) > 1",
        (odm.id,)).fetchall()
    assert shared == []


def test_discovery_is_idempotent(lconn, odm):
    first = discover(lconn, odm, created_by="test")
    before = _counts(lconn, odm)
    second = discover(lconn, odm, created_by="test")
    assert first.created and not second.created and not second.updated
    assert second.unchanged == len(first.created)
    assert _counts(lconn, odm) == before


def test_only_a_person_approves_a_candidate(lconn, odm):
    discover(lconn, odm, created_by="test")
    candidate = _candidates(lconn, odm)[0][0]
    with pytest.raises(PermissionError):
        approve_candidate(lconn, candidate, decided_by="kasa", method="code")
    with pytest.raises(psycopg.errors.CheckViolation):
        with lconn.transaction():
            lconn.execute("UPDATE l3_cost_object SET status = 'approved' WHERE id = %s", (candidate,))
    decision = approve_candidate(lconn, candidate, decided_by="artur")
    row = lconn.execute("SELECT status::text, method::text, decision_id FROM l3_cost_object WHERE id = %s",
                        (candidate,)).fetchone()
    assert row == ("approved", "human", decision)
    log = lconn.execute("SELECT kind::text, method::text, payload ->> 'action', decided_by FROM l3_decision_log "
                        "WHERE id = %s", (decision,)).fetchone()
    assert log == ("cost_object", "human", "approve", "artur")
    assert {r[4] for r in _members(lconn, candidate)} == {"accepted"}
    # a second run of discovery leaves the approved object and its members alone
    discover(lconn, odm, created_by="test")
    assert lconn.execute("SELECT status::text FROM l3_cost_object WHERE id = %s", (candidate,)).fetchone() == (
        "approved",)


def test_learning_from_decisions_changes_the_weights(lconn, odm):
    discover(lconn, odm, created_by="test")
    priors = load_model(lconn, odm.id)
    assert priors.version == 0 and priors.company_id is None
    gala, gdansk = (next(r[0] for r in _candidates(lconn, odm) if r[1] == name)
                    for name in ("Gala ACME 2026", "Roadshow Nordis Gdańsk"))
    approve_candidate(lconn, gala, decided_by="artur")
    after_approve = load_model(lconn, odm.id)
    assert after_approve.version == 1 and after_approve.company_id == odm.id
    assert after_approve.trained_on_decisions == 1
    changed_m = [k for k in after_approve.m if abs(after_approve.m[k] - priors.m[k]) > 1e-6]
    assert changed_m, "zatwierdzenie (same) zmienia m"
    assert all(abs(after_approve.u[k] - priors.u[k]) < 1e-6 for k in after_approve.u)
    mark_distinct(lconn, gala, gdansk, decided_by="artur")
    after_distinct = load_model(lconn, odm.id)
    assert after_distinct.version == 2
    assert any(abs(after_distinct.u[k] - priors.u[k]) > 1e-6 for k in after_distinct.u), "różne zmienia u"
    rows = lconn.execute("SELECT DISTINCT version FROM pair_model_weights WHERE company_id = %s ORDER BY 1",
                         (odm.id,)).fetchall()
    assert rows == [(1,), (2,)]
    # every level of every feature is a row of the table, nothing in the code
    assert {(f, lv) for f, levels in LEVELS.items() for lv in levels} == set(after_distinct.m)


def test_split_and_definitely_different_keep_records_apart_for_good(lconn, odm):
    discover(lconn, odm, created_by="test")
    gala = next(r[0] for r in _candidates(lconn, odm) if r[1] == "Gala ACME 2026")
    members = _members(lconn, gala)
    sales = [m[0] for m in members if m[1] == "document_line"]
    rest = [m[0] for m in members if m[1] != "document_line"]
    result = split_candidate(lconn, gala, [sales, rest], decided_by="artur")
    assert len(result.new_ids) == 2
    assert lconn.execute("SELECT status::text FROM l3_cost_object WHERE id = %s", (gala,)).fetchone() == ("rejected",)
    a, b = sorted(result.new_ids)
    assert pair_verdicts(lconn, odm.id)[(a, b)] == "different"
    new_rows = lconn.execute("SELECT status::text, method::text FROM l3_cost_object WHERE id = ANY(%s)",
                             (result.new_ids,)).fetchall()
    assert new_rows == [("candidate", "human")] * 2
    before = _counts(lconn, odm)
    again = discover(lconn, odm, created_by="test")
    assert not again.created, "podzielony kandydat nie wraca, części się nie sklejają"
    assert _counts(lconn, odm)[:2] == before[:2]
    for new_id in result.new_ids:
        assert lconn.execute("SELECT status::text FROM l3_cost_object WHERE id = %s", (new_id,)).fetchone() == (
            "candidate",)
    # "definitely different" between two other candidates is recorded and survives discovery
    wro, lodz = (next(r[0] for r in _candidates(lconn, odm) if r[1] == name)
                 for name in ("Roadshow Nordis Wrocław", "Roadshow Nordis Łódź"))
    mark_distinct(lconn, wro, lodz, decided_by="artur")
    discover(lconn, odm, created_by="test")
    assert pair_verdicts(lconn, odm.id)[tuple(sorted((wro, lodz)))] == "different"
    assert lconn.execute("SELECT count(*) FROM l3_cost_object WHERE id = ANY(%s) AND status = 'candidate'",
                         ([wro, lodz],)).fetchone() == (2,)


def test_definitely_different_blocks_the_grouping_of_new_records(lconn, odm):
    """A new sheet row that fits two objects a person keeps apart never glues them together."""
    discover(lconn, odm, created_by="test")
    gala = next(r[0] for r in _candidates(lconn, odm) if r[1] == "Gala ACME 2026")
    members = _members(lconn, gala)
    rows = [m[0] for m in members if m[1] == "internal_record"]
    others = [m[0] for m in members if m[1] != "internal_record"]
    result = split_candidate(lconn, gala, [others, rows], decided_by="artur")
    raw = lconn.execute("SELECT file_raw_id FROM l1_internal_record WHERE company_id = %s LIMIT 1",
                        (odm.id,)).fetchone()[0]
    lconn.execute(
        """
        INSERT INTO l1_internal_record (company_id, file_raw_id, mapping_key, row_no, fields, record_date,
                                        amount_minor, cost_object_ref, settlement_type, source, method, created_by)
        VALUES (%s, %s, 'pm_sheet', 99, '{"Event": "Gala ACME"}', DATE '2026-10-09', 30000, 'Gala ACME',
                'umowa zlecenie', 'test', 'code', 'test')
        """,
        (odm.id, raw),
    )
    report = discover(lconn, odm, created_by="test")
    assert not report.created
    statuses = lconn.execute("SELECT status::text FROM l3_cost_object WHERE id = ANY(%s)",
                             (result.new_ids,)).fetchall()
    assert statuses == [("candidate",), ("candidate",)]


def test_reject_is_final_and_merge_moves_members(lconn, odm):
    discover(lconn, odm, created_by="test")
    rows = {r[1]: r[0] for r in _candidates(lconn, odm)}
    reject_candidate(lconn, rows["Helios Partner Summit 2026"], decided_by="artur", reason="test")
    report = discover(lconn, odm, created_by="test")
    assert not report.created and report.skipped_rejected == 5     # FV 5/10 line + 4 PM sheet rows
    wro, lodz = rows["Roadshow Nordis Wrocław"], rows["Roadshow Nordis Łódź"]
    moved = len([m for m in _members(lconn, lodz) if m[4] == "open"])
    merge_candidates(lconn, [wro, lodz], decided_by="artur")
    assert lconn.execute("SELECT status::text, merged_into FROM l3_cost_object WHERE id = %s", (lodz,)).fetchone() == (
        "merged", wro)
    assert pair_verdicts(lconn, odm.id)[tuple(sorted((wro, lodz)))] == "same"
    target_members = _members(lconn, wro)
    assert len([m for m in target_members if m[4] == "open"]) >= moved
    assert sum(1 for m in target_members if m[3] and m[4] == "open") == 1, "jedna kotwica"


def test_rentease_derives_room_by_month():
    rentease = get_extension("rentease")
    company = uuid.uuid4()
    roll = CandidateRecord(record_type="internal_record", record_id=uuid.uuid4(), company_id=company,
                           record_date=date(2026, 11, 1), amount_minor=230000, text="P3 | 2026-11",
                           attrs={"group": 0, "is_anchor": True, "mapping_key": "rent_roll", "cost_object_ref": "P3",
                                  "month": "2026-11"})
    rent = CandidateRecord(record_type="transaction", record_id=uuid.uuid4(), company_id=company,
                           record_date=date(2026, 11, 3), amount_minor=230000, text="czynsz P3 listopad",
                           attrs={"group": 0})
    assert rentease.is_anchor_eligible(roll) is True
    assert rentease.is_anchor_eligible(rent) is False
    [candidate] = rentease.derive_cost_object_candidates([roll, rent])
    assert candidate.kind == "room_month"
    assert candidate.attrs == {"room": "P3", "month": "2026-11"}
    assert candidate.name == "P3 / 2026-11"
    assert candidate.anchor == ("internal_record", roll.record_id)


def test_grouping_never_joins_groups_a_person_keeps_apart(lconn, odm):
    """Cannot-link: two anchors that the pair model would merge stay apart when a person said so."""
    from kasa.core.cost_objects_discovery.grouping import Group, cluster_anchors
    from kasa.core.cost_objects_discovery.pairs import PairScorer
    from kasa.core.cost_objects_discovery.records import load_records

    records = load_records(lconn, odm.id)
    rows = [r for r in records if r.record_type == "internal_record" and r.facts.get("cost_object_ref") == "Gala ACME"]
    scorer = PairScorer(load_model(lconn, odm.id), records)
    assert scorer.probability(rows[0], rows[1]) > 0.9
    merged = cluster_anchors(rows[:2], [], scorer, lambda a, b: False)
    assert len(merged) == 1
    kept_apart = cluster_anchors(rows[:2], [], scorer, lambda a, b: True)
    assert len(kept_apart) == 2
    # two existing objects are never merged by the code, whatever the model says
    first = Group(anchors=[rows[0]], existing_id=uuid.uuid4())
    second = Group(anchors=[rows[1]], existing_id=uuid.uuid4())
    assert len(cluster_anchors([], [first, second], scorer, lambda a, b: False)) == 2


def test_rentease_candidates_come_from_the_same_core(lconn):
    """The same core grouping gives room x month for Rentease (no change in the core)."""
    import hashlib

    from kasa.core.companies import ensure_company

    rentease, _ = ensure_company(lconn, get_extension("rentease"))
    payload = b"pokoj;najemca;miesiac;czynsz\nP3;A;11.2026;2300\nP4;B;11.2026;2500\n"
    raw = lconn.execute(
        "INSERT INTO l0_raw (company_id, channel, sha256, content_type, payload) VALUES (%s, 'file', %s, 'text/csv', %s) "
        "RETURNING id", (rentease.id, hashlib.sha256(payload).hexdigest(), payload)).fetchone()[0]
    account = lconn.execute(
        "INSERT INTO l1_bank_account (company_id, iban, currency, kind, source, method, created_by) "
        "VALUES (%s, 'PL61109010140000071219812874', 'PLN', 'current', 'test', 'code', 'test') RETURNING id",
        (rentease.id,)).fetchone()[0]
    for row_no, (room, person, amount) in enumerate([("P3", "P-aaaa", 230000), ("P4", "P-bbbb", 250000)], start=2):
        lconn.execute(
            """
            INSERT INTO l1_internal_record (company_id, file_raw_id, mapping_key, row_no, fields, person_pseudonym,
                                            amount_minor, cost_object_ref, evidence, source, method, created_by)
            VALUES (%s, %s, 'rent_roll', %s, %s, %s, %s, %s, %s, 'test', 'code', 'test')
            """,
            (rentease.id, raw, row_no, Jsonb({"Pokój": room, "Miesiąc": "11.2026"}), person, amount, room,
             Jsonb({"column_map": {"month": "Miesiąc", "cost_object_ref": "Pokój"}})))
        lconn.execute(
            """
            INSERT INTO l1_bank_transaction (company_id, account_id, fingerprint, status, booking_date, amount_minor,
                                             currency, title_raw, title_pseudonymized, counterparty_pseudonym,
                                             op_type, source, method, created_by)
            VALUES (%s, %s, %s, 'booked', DATE '2026-11-03', %s, 'PLN', %s, %s, %s, 'transfer', 'test', 'code', 'test')
            """,
            (rentease.id, account, f"fp-{room}", amount, f"Czynsz {room} listopad", f"Czynsz {room} listopad", person))
    report = discover(lconn, rentease, created_by="test")
    rows = lconn.execute("SELECT kind, name, attrs FROM l3_cost_object WHERE company_id = %s AND system_code IS NULL "
                         "ORDER BY name", (rentease.id,)).fetchall()
    assert len(report.created) == 2
    assert rows == [("room_month", "P3 / 2026-11", {"room": "P3", "month": "2026-11"}),
                    ("room_month", "P4 / 2026-11", {"room": "P4", "month": "2026-11"})]
