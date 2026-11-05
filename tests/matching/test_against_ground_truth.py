"""The whole October month against the reference truth (gate 4 of matching, docs/FAZY.md).

Flow as in production: every channel imported, title semantics read, control set imported from the
reference file (``control-set import-ground-truth``), calibration on it, one matching run. Then:

* 0 false automatic decisions against the labels - documents AND amounts (``ground_truth.json``);
* every transfer with ``expected_route = auto`` closed by the automation, none with ``queue``;
* for ``queue``: the rank-1 suggestion says what the label says in >= 90% of the transfers;
* truth check 2 (coverage of matches) green.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from kasa.core import control_sets as cs
from kasa.core.checks import check_match_coverage
from kasa.core.matching.calibrate import SUBTYPE_EQUIVALENT, calibrate, labels_of
from kasa.core.matching.run import run_matching
from kasa.reference.ground_truth import SUBTYPE_BY_CATEGORY

from .conftest import OCTOBER

TRUTH = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "odm" / "ground_truth.json")
                   .read_text(encoding="utf-8"))


def _minor(text: str) -> int:
    return int((Decimal(text) * 100).to_integral_value())


def _expected_items(tx: dict) -> set[tuple[str, int]]:
    out = set()
    for d in tx["matched_documents"]:
        key = d["doc_id"] if d["doc_type"] in ("ksef", "bank_tx", "pm_sheet") else d.get("invoice_number")
        out.add((key, _minor(d["amount_pln"])))
    return out


def _actual(conn, company_id) -> tuple[dict, dict]:
    statuses = {r[0]: r[1:] for r in conn.execute(
        """
        SELECT t.entry_reference, s.final_status::text, s.subtype, s.method::text FROM l3_transaction_status s
          JOIN l1_bank_transaction t ON t.id = s.transaction_id WHERE s.company_id = %s AND s.status = 'active'
        """,
        (company_id,),
    ).fetchall()}
    items: dict[str, set] = {}
    for ref, key, amount in conn.execute(
        """
        SELECT t.entry_reference,
               coalesce(d.ksef_number, d.number_raw, r.entry_reference,
                        regexp_replace(f.external_ref, '^.*/|\\.[a-z]+$', '', 'g') || ':R' || lpad((ir.row_no - 1)::text, 2, '0')),
               m.amount_minor
          FROM l3_match m JOIN l1_bank_transaction t ON t.id = m.transaction_id
          LEFT JOIN l1_document d ON d.id = m.document_id
          LEFT JOIN l1_bank_transaction r ON r.id = m.related_transaction_id
          LEFT JOIN l1_internal_record ir ON ir.id = m.internal_record_id
          LEFT JOIN l0_raw f ON f.id = ir.file_raw_id
         WHERE m.company_id = %s AND m.status = 'active'
        """,
        (company_id,),
    ).fetchall():
        items.setdefault(ref, set()).add((key, amount))
    return statuses, items


def _subtype(value):
    return SUBTYPE_EQUIVALENT.get(value, value)


def _rank1_agrees(proposal: dict, label: dict) -> bool:
    if proposal["final_status"] != label["status"]:
        return False
    if label["status"] == "matched":
        targets = set()
        for item in proposal["items"]:
            for key, kind in (("document_id", "document"), ("internal_record_id", "internal_record"),
                              ("related_transaction_id", "transaction")):
                if item[key]:
                    targets.add((kind, item[key]))
        return targets == {(kind, str(ref)) for kind, ref in label["_targets"]}
    if label["status"] == "no_document":
        return _subtype(proposal["subtype"]) == _subtype(label.get("subtype"))
    return True


def test_full_month_against_the_reference_truth(mconn, odm):
    set_id = cs.find_set(mconn, "ground-truth-transaction")
    calibration = calibrate(mconn, odm.id, set_id)
    assert calibration.chosen.false_autos == 0 and not calibration.auto_off
    report = run_matching(mconn, odm, period=OCTOBER)
    assert not report.auto_errors
    statuses, items = _actual(mconn, odm.id)
    truth = {t["entry_reference"]: t for t in TRUTH["transactions"]}

    # 1. 0 false automatic decisions: status, documents and amounts as in the reference file
    automatic = {ref for ref, (_status, _subtype_, method) in statuses.items() if method == "rule"}
    false = {}
    for ref in automatic:
        tx = truth[ref]
        status, subtype, _ = statuses[ref]
        if status != tx["expected_status"]:
            false[ref] = (status, tx["expected_status"])
        elif status == "matched" and items.get(ref, set()) != _expected_items(tx):
            false[ref] = (items.get(ref), _expected_items(tx))
        elif status == "no_document" and _subtype(subtype) != _subtype(
                SUBTYPE_BY_CATEGORY.get(tx.get("no_document_category") or "", "other")):
            false[ref] = (subtype, tx.get("no_document_category"))
    assert not false, false

    # 2. every "auto" of the reference closed by the automation, no "queue" closed by it
    expected_auto = {ref for ref, t in truth.items() if t["expected_route"] == "auto"}
    expected_queue = {ref for ref, t in truth.items() if t["expected_route"] == "queue"}
    missing = {ref: truth[ref]["scenario_key"] for ref in expected_auto - automatic}
    assert not missing, f"powinny być automatem: {missing}"
    assert not automatic & expected_queue, automatic & expected_queue
    assert len(automatic) == 48 and report.auto == 45      # 45 decisions, 3 of them close a VAT leg as well

    # 3. queue: the rank-1 suggestion agrees with the label in >= 90%
    labels = {mconn.execute("SELECT entry_reference FROM l1_bank_transaction WHERE id = %s",
                            (subject,)).fetchone()[0]: label for subject, label in labels_of(mconn, set_id).items()}
    rank1 = dict(mconn.execute(
        """
        SELECT t.entry_reference, g.proposal FROM l2_suggestion g JOIN l1_bank_transaction t ON t.id = g.subject_id
         WHERE g.company_id = %s AND g.kind = 'match' AND g.status = 'open' AND g.rank = 1
        """,
        (odm.id,),
    ).fetchall())
    assert expected_queue <= set(rank1)
    disagree = {ref: (rank1[ref]["final_status"], labels[ref]["status"]) for ref in expected_queue
                if not _rank1_agrees(rank1[ref], labels[ref])}
    assert len(expected_queue) - len(disagree) >= 0.9 * len(expected_queue), disagree
    assert not disagree                                      # on these fixtures: every one of them

    # 4. truth check 2
    coverage = check_match_coverage(mconn, odm.id, *OCTOBER)
    assert coverage.ok, coverage.detail
    assert coverage.detail["transactions"] == 33 and coverage.detail["documents"] == 32
