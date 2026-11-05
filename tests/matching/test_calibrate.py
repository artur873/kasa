"""Calibration on the labeled control set: 0 false automatic matches, as many automata as possible."""

from __future__ import annotations

from kasa.core import control_sets as cs
from kasa.core.matching.calibrate import calibrate, labels_of
from kasa.core.matching.engine import load_params
from kasa.reference import ground_truth as reference

from .helpers import tx_id


def test_calibration_chooses_zero_false_automata_and_the_engine_reads_it(mconn, odm):
    set_id = cs.find_set(mconn, "ground-truth-transaction")
    report = calibrate(mconn, odm.id, set_id)
    chosen = report.chosen
    assert not report.auto_off and chosen.false_autos == 0 and chosen.autos >= 45
    assert chosen.queue_rank1_agree >= 0.9 * chosen.queue
    assert chosen.params["auto_threshold"] >= 0.995 and chosen.params["margin_min"] >= 0.2
    rows = mconn.execute("SELECT count(*), count(*) FILTER (WHERE chosen) FROM calibration_result WHERE run_id = %s",
                         (report.run_id,)).fetchone()
    assert rows == (len(report.points), 1)
    assert load_params(mconn, odm.id) == chosen.params


def test_a_label_against_a_hard_candidate_lowers_the_automata_never_allows_a_false_one(mconn, odm):
    truth = reference.load(reference.default_path("odm"))
    expected = reference.expected_labels(mconn, odm.id, truth, "transaction")
    orange = tx_id(mconn, "202610232074877344")
    expected[("transaction", orange)] = {"status": "no_document", "subtype": "fee", "note": "celowo błędna etykieta"}
    info = cs.import_labels(mconn, odm, kind="transaction", expected=expected, name="zbiór z błędem")
    assert labels_of(mconn, info.id)[orange]["status"] == "no_document"
    report = calibrate(mconn, odm.id, info.id)
    assert report.chosen.false_autos == 0
    assert report.auto_off or report.chosen.autos < 45
    assert any(p.false_autos for p in report.points)
