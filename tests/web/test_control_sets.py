"""Control sets: drawing, sentinels, labels (not decisions), history by the database, export, fixture labels."""

from __future__ import annotations

import contextlib
import json

import psycopg
import pytest

from kasa.core import control_sets as cs
from kasa.reference import ground_truth as reference
from tests.semantics.helpers import ground_truth


@contextlib.contextmanager
def db_error(conn: psycopg.Connection, sqlstate: str):
    with pytest.raises(psycopg.Error) as excinfo:
        with conn.transaction():
            yield
    assert excinfo.value.sqlstate == sqlstate, f"{excinfo.value.sqlstate}: {excinfo.value}"


def _items(conn, set_id) -> list[tuple]:
    return conn.execute(
        "SELECT id, order_no, subject_id, is_sentinel, sentinel_expected FROM control_set_item WHERE set_id = %s "
        "ORDER BY order_no", (set_id,)).fetchall()


def test_a_set_takes_everything_when_data_is_short_and_has_a_sentinel(conn, loaded):
    info = cs.create_control_set(conn, loaded, kind="transaction", size=600, seed=42,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "transaction"))
    assert (info.items, info.size_target, info.sentinels) == (83, 600, 1) and info.frozen_at is not None
    items = _items(conn, info.id)
    assert [i[1] for i in items] == list(range(1, 84))
    [sentinel] = [i for i in items if i[3]]
    assert 30 <= sentinel[1] <= 55 and sentinel[4]["status"] in cs.STATUSES
    # the same seed draws the same order; another seed another one
    again = cs.create_control_set(conn, loaded, kind="transaction", size=600, seed=42,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "transaction"))
    assert [i[2] for i in _items(conn, again.id)] == [i[2] for i in items] and again.name != info.name
    other = cs.create_control_set(conn, loaded, kind="transaction", size=600, seed=7,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "transaction"))
    assert [i[2] for i in _items(conn, other.id)] != [i[2] for i in items]
    small = cs.create_control_set(conn, loaded, kind="transaction", size=10, seed=1,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "transaction"))
    assert (small.items, small.sentinels) == (10, 1)
    costs = cs.create_control_set(conn, loaded, kind="cost", size=150, seed=1,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "cost"))
    available = conn.execute(
        """SELECT (SELECT count(*) FROM l1_document WHERE company_id = %(c)s AND superseded_by IS NULL
                     AND direction = 'purchase')
                + (SELECT count(*) FROM l1_internal_record WHERE company_id = %(c)s AND superseded_by IS NULL)""",
        {"c": loaded.id}).fetchone()[0]
    assert costs.items == available == 70 and costs.sentinels == 1   # purchases (KSeF, PDF, JPK) + 20 sheet rows
    assert sentinel_positions_are_spread()


def sentinel_positions_are_spread() -> bool:
    positions = cs._sentinel_positions(600, 12)
    gaps = [b - a for a, b in zip([0, *positions], positions, strict=False)]
    return len(positions) == 12 and all(40 <= g <= 50 for g in gaps)


def test_without_ground_truth_there_is_no_sentinel(conn, loaded):
    info = cs.create_control_set(conn, loaded, kind="transaction", size=5, seed=3)
    assert (info.items, info.sentinels) == (5, 0)


def test_labels_are_written_once_and_changes_go_to_history(conn, loaded):
    info = cs.create_control_set(conn, loaded, kind="transaction", size=3, seed=5)
    item_id = _items(conn, info.id)[0][0]
    shown = cs.next_item(conn, info.id)
    assert shown["id"] == item_id and shown["shown_at"] is not None
    cs.save_label(conn, info.id, item_id, {"status": "to_clarify"}, labeled_by="artur")
    first = conn.execute("SELECT label, labeled_by, duration_ms FROM control_set_item WHERE id = %s",
                         (item_id,)).fetchone()
    assert first[0] == {"status": "to_clarify", "documents": [], "other_document": False} and first[2] >= 0
    cs.save_label(conn, info.id, item_id, {"status": "no_document", "subtype": "fee"}, labeled_by="artur")
    history = conn.execute(
        "SELECT old_label, new_label, old_by FROM control_set_label_history WHERE item_id = %s", (item_id,),
    ).fetchall()
    assert len(history) == 1 and history[0][0]["status"] == "to_clarify" and history[0][1]["subtype"] == "fee"
    # the database guards the set: identity, deletion, freezing
    with db_error(conn, "KS009"):
        conn.execute("UPDATE control_set_item SET order_no = 99 WHERE id = %s", (item_id,))
    with db_error(conn, "KS009"):
        conn.execute("UPDATE control_set_item SET label = NULL, labeled_by = NULL, labeled_at = NULL WHERE id = %s",
                     (item_id,))
    with db_error(conn, "KS002"):
        conn.execute("DELETE FROM control_set_item WHERE id = %s", (item_id,))
    with db_error(conn, "KS009"):
        conn.execute(
            """INSERT INTO control_set_item (set_id, company_id, subject_type, subject_id, order_no)
               SELECT set_id, company_id, subject_type, subject_id, 50 FROM control_set_item WHERE id = %s""",
            (item_id,))
    with db_error(conn, "KS001"):
        conn.execute("DELETE FROM control_set_label_history WHERE item_id = %s", (item_id,))
    # labels are reference truth, never decisions
    assert conn.execute("SELECT count(*) FROM l3_decision_log WHERE company_id = %s", (loaded.id,)).fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM l3_transaction_status WHERE company_id = %s",
                        (loaded.id,)).fetchone() == (0,)


def test_invalid_labels_are_refused(conn, loaded):
    info = cs.create_control_set(conn, loaded, kind="transaction", size=2, seed=5)
    item_id = _items(conn, info.id)[0][0]
    for label in ({"status": "matched"}, {"status": "no_document"}, {"status": "own_transfer", "subtype": "fee"},
                  {"status": "approved"}, {"status": "matched", "documents": [{"type": "document", "id": "x"}]}):
        with pytest.raises(cs.ControlSetError):
            cs.save_label(conn, info.id, item_id, label, labeled_by="artur")
    costs = cs.create_control_set(conn, loaded, kind="cost", size=2, seed=5)
    cost_item = _items(conn, costs.id)[0][0]
    with pytest.raises(cs.ControlSetError, match="kategoria"):
        cs.save_label(conn, costs.id, cost_item, {"category": "Bitcoin", "unassigned": True}, labeled_by="artur")
    with pytest.raises(cs.ControlSetError, match="100%"):
        cs.save_label(conn, costs.id, cost_item, {"split": [{"name": "A", "share": "0.5"}]}, labeled_by="artur")
    stored = cs.save_label(conn, costs.id, cost_item, {"category": "Realizacja eventu / Logistyczne", "cost_object": "Gala ACME 2026"},
                           labeled_by="artur")
    assert stored["label"]["cost_object"] == "Gala ACME 2026"


def test_sentinel_result_is_hidden_until_the_set_is_complete(conn, loaded):
    info = cs.create_control_set(conn, loaded, kind="transaction", size=3, seed=11,
                                   expected=reference.expected_labels(conn, loaded.id, ground_truth(), "transaction"))
    items = _items(conn, info.id)
    sentinel = next(i for i in items if i[3])
    wrong = {"status": "to_clarify"} if sentinel[4]["status"] != "to_clarify" else {"status": "own_transfer"}
    cs.save_label(conn, info.id, sentinel[0], wrong, labeled_by="artur")
    status = cs.status(conn, info.id)
    assert "sentinel_results" not in status and not status["completed"]
    for item in items:
        if not item[3]:
            cs.save_label(conn, info.id, item[0], {"status": "to_clarify"}, labeled_by="artur")
    status = cs.status(conn, info.id)
    assert status["completed"] and status["sentinel_results"] == {"passed": 0, "failed": 1}
    assert status["progress"] == "3/3"


def test_ground_truth_import_and_export_round_trip(conn, loaded):
    info = reference.import_ground_truth(conn, loaded, kind="transaction", truth=ground_truth())
    assert info.items == 83 and info.completed_at is not None
    exported = cs.export_set(conn, info.id)
    truth = {t["entry_reference"]: t for t in ground_truth()["transactions"]}
    assert exported["status"]["fixture_labels"] == 83
    assert len(exported["transactions"]) == 83
    for entry in exported["transactions"]:
        expected = truth[entry["entry_reference"]]
        assert entry["expected_status"] == expected["expected_status"]
        assert entry["amount"] == expected["amount"] and entry["labeled_by"] == "fixture"
        assert {(d["doc_type"], d["doc_id"]) for d in entry["matched_documents"]} == {
            (d["doc_type"], d["doc_id"]) for d in expected["matched_documents"]}, entry["entry_reference"]
    json.dumps(exported, default=str)                                   # serializable as is
    costs = reference.import_ground_truth(conn, loaded, kind="cost", truth=ground_truth())
    exported_costs = cs.export_set(conn, costs.id)["costs"]
    kreska = next(c for c in exported_costs if c.get("number") == "FV 12/09/2026")
    assert [a["cost_object_name"] for a in kreska["final_allocation"]] == [
        "Roadshow Nordis Gdańsk", "Roadshow Nordis Wrocław", "Roadshow Nordis Łódź"]
    sheet = [c for c in exported_costs if c["doc_type"] == "internal_record"]
    assert len(sheet) == 20 and all(c["final_allocation"] for c in sheet)
