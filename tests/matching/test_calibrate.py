"""Calibration on the labeled control set: cross-validation, 0 false automatic matches in every test fold."""

from __future__ import annotations

from datetime import date

from kasa.core import control_sets as cs
from kasa.core.matching.calibrate import calibrate, labels_needed, labels_of, wilson_upper
from kasa.core.matching.engine import load_params
from kasa.reference import ground_truth as reference

from .helpers import add_doc, add_tx, ksef_number, party, tx_id


def test_calibration_chooses_zero_false_automata_and_the_engine_reads_it(mconn, odm):
    set_id = cs.find_set(mconn, "ground-truth-transaction")
    report = calibrate(mconn, odm.id, set_id)
    chosen = report.chosen
    assert not report.auto_off and chosen.false_autos == 0 and chosen.autos >= 41
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


def test_calibration_result_carries_the_cross_validation_and_the_wilson_bound(mconn, odm):
    report = calibrate(mconn, odm.id, cs.find_set(mconn, "ground-truth-transaction"))
    metrics = mconn.execute("SELECT metrics FROM calibration_result WHERE run_id = %s AND chosen",
                            (report.run_id,)).fetchone()[0]
    assert report.basis == "counterparty" and len(report.folds) == 5            # one month: by counterparty
    assert sum(len(f.subject_ids) for f in report.folds) == len(labels_of(mconn, report.control_set_id))
    assert metrics["labels"] == 80 and metrics["false_auto"] == 0 and metrics["labels_total"] == 83
    # 0 errors on 80 labels: the error is only known to be below z²/(n+z²) = 4.58% - not trustworthy yet
    assert abs(metrics["upper_bound_error_wilson95"] - wilson_upper(0, 80)) < 1e-6
    assert 0.045 < metrics["upper_bound_error_wilson95"] < 0.047 and metrics["trustworthy"] is False
    assert labels_needed() == 765 and wilson_upper(0, 765) <= 0.005 < wilson_upper(0, 764)
    assert len(metrics["folds"]) == 5 and all(f["false_auto"] == 0 for f in metrics["folds"])
    assert sum(f["autos"] for f in metrics["folds"]) == metrics["autos"] and metrics["whole"]["false_auto"] == 0
    assert metrics["nested"]["false_auto"] == 0
    assert "UWAGA: kalibracja jeszcze niewiarygodna" in report.summary()
    # deterministic: the same folds again
    again = calibrate(mconn, odm.id, report.control_set_id, record=False)
    assert [f.subject_ids for f in again.folds] == [f.subject_ids for f in report.folds]
    assert all(p.params["auto_threshold"] >= 0.995 for p in report.points)


def test_a_point_with_zero_false_on_the_whole_set_but_one_in_a_fold_is_rejected(mconn, odm):
    """Supplier S is paid by A (FV number, IBAN): A claims the invoice in the run of the month, so B - another
    party naming the invoice by its KSeF number - only sees an overpayment there. In B's fold (a run without
    A) B is hard evidence for the invoice and the automation would close it against its label: 0 false on the
    whole set, 1 false in a fold - the point must not be chosen."""
    nip, iban = "5260001246", "PL27114020040000300201355387"
    supplier = party(mconn, odm.id, "Dostawca Testowy sp. z o.o.", nip, iban)
    number = ksef_number(nip)
    invoice = add_doc(mconn, odm.id, "Q/1/2026", 100000, counterparty_id=supplier, nip=nip, payment_iban=iban,
                      ksef_number=number)
    a = add_tx(mconn, odm.id, -100000, "Q/1/2026", counterparty_id=supplier, name="Dostawca Testowy sp. z o.o.",
               iban=iban, day=date(2026, 10, 21))
    other = party(mconn, odm.id, "Inna Firma Handlowa sp. z o.o.", None, "PL61109010140000071219812874")
    b = add_tx(mconn, odm.id, -100000, f"oplata {number}", counterparty_id=other,
               name="Inna Firma Handlowa sp. z o.o.", iban="PL61109010140000071219812874", day=date(2026, 10, 22))
    info = cs.import_labels(mconn, odm, kind="transaction", name="zbiór foldów", expected={
        ("transaction", a): {"status": "matched", "documents": [{"type": "document", "id": str(invoice)}]},
        ("transaction", b): {"status": "no_document", "subtype": "other"}})
    report = calibrate(mconn, odm.id, info.id)
    assert len(report.folds) == 2 and {frozenset(f.subject_ids) for f in report.folds} == {
        frozenset({a}), frozenset({b})}
    trap = [p for p in report.points if p.whole_false_autos == 0 and p.false_autos > 0]
    assert trap, "some point is false only in a fold"
    assert all(not p.admissible for p in trap)
    # the choice on the whole set alone (0 false, most automata) would have admitted such a point
    best_whole = max(p.whole_autos for p in report.points if p.whole_false_autos == 0)
    assert any(p.whole_autos == best_whole for p in trap)
    chosen = report.chosen
    assert chosen not in trap and chosen.admissible and all(f.false_autos == 0 for f in chosen.folds)
    assert not report.auto_off and chosen.false_autos == 0
