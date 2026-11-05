"""Every trap of fixtures/odm/SCENARIUSZ.md through the matching engine (briefing, step 4, the table of traps)."""

from __future__ import annotations

from datetime import date, timedelta

from kasa.core.checks import check_match_coverage
from kasa.core.matching import auto
from kasa.core.matching.engine import evaluate
from kasa.core.matching.run import run_matching

from .conftest import OCTOBER
from .helpers import doc_id, only, result_for, tx_id


def _status(conn, entry_reference):
    return conn.execute(
        """
        SELECT s.final_status::text, s.subtype, s.method::text, s.awaiting_until FROM l3_transaction_status s
          JOIN l1_bank_transaction t ON t.id = s.transaction_id
         WHERE t.entry_reference = %s AND s.status = 'active'
        """,
        (entry_reference,),
    ).fetchone()


def _matches(conn, entry_reference):
    return conn.execute(
        """
        SELECT coalesce(d.number_raw, r.entry_reference, 'R' || ir.row_no), m.amount_minor, m.document_amount_minor,
               m.difference_kind::text, m.difference_minor, m.group_id, m.method::text
          FROM l3_match m JOIN l1_bank_transaction t ON t.id = m.transaction_id
          LEFT JOIN l1_document d ON d.id = m.document_id
          LEFT JOIN l1_bank_transaction r ON r.id = m.related_transaction_id
          LEFT JOIN l1_internal_record ir ON ir.id = m.internal_record_id
         WHERE t.entry_reference = %s AND m.status = 'active' ORDER BY m.amount_minor DESC
        """,
        (entry_reference,),
    ).fetchall()


def test_split_payment_is_one_occurrence_matched_to_the_gross(mconn, odm):
    main, vat = "202610234002647079", "202610232774376085"
    result = result_for(mconn, odm.id, main)
    best = result.best
    assert [t.entry_reference for t in result.group] == [main, vat] and result.amount_minor == 6888000
    assert best.hard and best.rule_key == "builtin:match_mpp_block" and "mpp_block" in best.tags
    assert [i.amount_minor for i in best.items] == [6888000]
    # the VAT leg is never a subject of its own
    assert vat not in {r.tx.entry_reference for r in evaluate(mconn, odm.id).results}
    run_matching(mconn, odm, period=OCTOBER)
    legs = {ref: _matches(mconn, ref) for ref in (main, vat)}
    assert [(m[0], m[1]) for m in legs[main]] == [("FV 3/10/2026", 5600000)]
    assert [(m[0], m[1]) for m in legs[vat]] == [("FV 3/10/2026", 1288000)]
    assert legs[main][0][5] == legs[vat][0][5]                       # one group
    assert _status(mconn, main)[:3] == _status(mconn, vat)[:3] == ("matched", None, "rule")
    decisions = mconn.execute(
        "SELECT count(*) FROM l3_decision_log WHERE subject_id IN (%s, %s)",
        (tx_id(mconn, main), tx_id(mconn, vat))).fetchone()[0]
    assert decisions == 1                                             # one decision closes both legs
    # outgoing split payment of Scena Pro: 25 000 + 5 750 = SP/2026/10/087
    assert sum(m[1] for ref in ("202610143186203462", "202610141002142076") for m in _matches(mconn, ref)) == 3075000


def test_one_transfer_for_three_invoices_is_one_group(mconn, odm):
    ref = "202610289276403991"
    best = result_for(mconn, odm.id, ref).best
    assert best.hard and len(best.items) == 3 and sum(i.amount_minor for i in best.items) == 1623600
    run_matching(mconn, odm, period=OCTOBER)
    rows = _matches(mconn, ref)
    assert sorted((r[0], r[1]) for r in rows) == [
        ("SP/2026/10/081", 492000), ("SP/2026/10/093", 492000), ("SP/2026/10/101", 639600)]
    assert len({r[5] for r in rows}) == 1


def _accept_rank1(conn, entry_reference, decided_by="artur"):
    from kasa.core.matching.decide import apply_match

    suggestion = conn.execute(
        "SELECT id FROM l2_suggestion WHERE subject_id = %s AND status = 'open' AND rank = 1",
        (tx_id(conn, entry_reference),)).fetchone()[0]
    return apply_match(conn, suggestion, decided_by=decided_by)


def test_correction_is_a_negative_row_of_the_group(mconn, odm):
    ref = "ML2610205610950"
    best = result_for(mconn, odm.id, ref).best
    assert "correction_chain" in best.tags
    assert [(i.label, i.amount_minor, i.difference_kind) for i in best.items] == [
        ("FV 2/10/2026", 1000000, None), ("KOR 1/10/2026", -200000, "correction")]
    # Nordis is known to the bank by name and IBAN, the invoice by NIP: the same name is not hard evidence
    assert not best.hard and "counterparty_name_only" in best.tags
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, ref) is None and _matches(mconn, ref) == []
    _accept_rank1(mconn, ref)                                      # the owner, in the queue
    assert sorted((r[0], r[1], r[3]) for r in _matches(mconn, ref)) == [
        ("FV 2/10/2026", 1000000, None), ("KOR 1/10/2026", -200000, "correction")]
    assert check_match_coverage(mconn, odm.id, *OCTOBER, record=False).ok


def test_a_persons_match_links_the_iban_and_then_the_invoice_number_is_hard(mconn, odm):
    first, later = "ML2610205610950", "ML2610301580065"            # Nordis: correction chain, then FV Wrocław
    before = result_for(mconn, odm.id, later).best
    assert not before.hard and "counterparty_name_only" in before.tags
    run_matching(mconn, odm, period=OCTOBER)
    decision = _accept_rank1(mconn, first)
    linked = mconn.execute("SELECT evidence -> 'iban_linked' FROM l3_decision_log WHERE id = %s",
                           (decision,)).fetchone()[0]
    iban = mconn.execute("SELECT counterparty_iban_raw FROM l1_bank_transaction WHERE entry_reference = %s",
                         (first,)).fetchone()[0].replace(" ", "")
    assert linked["iban"] == iban and linked["nip"] == "5867422344"
    ibans = mconn.execute("SELECT ibans FROM l1_counterparty WHERE nip = '5867422344' AND superseded_by IS NULL"
                          ).fetchone()[0]
    assert iban in ibans
    # from now on the same IBAN agrees by IBAN: the next invoice number is hard evidence and automatic
    after = result_for(mconn, odm.id, later).best
    assert after.hard and after.rule_key == "builtin:match_invoice_number_same_counterparty"
    assert "counterparty_strong" in after.tags
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, later)[:3] == ("matched", None, "rule")


def test_revolut_with_the_client_in_the_title_goes_to_the_queue(mconn, odm):
    result = result_for(mconn, odm.id, "202610233423350565")
    best = result.best
    assert best.items[0].document_id == doc_id(mconn, "FV 4/10/2026", "sales")
    assert not best.hard and "counterparty_title_name" in best.tags and "single_amount_any" in best.tags
    assert [c.kind for c in result.candidates if c.is_match] == ["document"]   # one proposal to click
    assert not auto.eligibility(best, {"auto_threshold": 0.995, "margin_min": 0.2}, {})[0]


def test_card_saas_has_the_named_difference_fx(mconn, odm):
    expected = {"202610166224040945": ("5384029117", 1625), "202610021670611934": ("INV-2610-0437", 1005),
                "202610221759883865": ("8F2C41A0-0007", 637)}
    for ref, (number, diff) in expected.items():
        best = result_for(mconn, odm.id, ref).best
        item = best.items[0]
        assert (item.label, item.difference_kind, item.difference_minor) == (number, "fx", diff), ref
        assert not best.hard and any("różnica „kurs”" in f for f in best.facts)
    # the receipt for fuel: PLN, a card, the same amount
    assert result_for(mconn, odm.id, "202610264228948107").best.items[0].label == "028417"


def test_euro_transfer_closes_the_invoice_in_euro_with_the_difference_kurs(mconn, odm):
    ref = "202610280050568790"
    best = result_for(mconn, odm.id, ref).best
    # Helios is known only by name on both sides (the invoice has no NIP of the buyer): not hard
    assert not best.hard and best.items[0].label == "FV 5/10/2026" and "counterparty_name_only" in best.tags
    run_matching(mconn, odm, period=OCTOBER)
    decision = _accept_rank1(mconn, ref)
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in _matches(mconn, ref)] == [
        ("FV 5/10/2026", 5273750, 1250000, "fx", -34750)]
    # the owner's match ties Helios' IBAN to the counterparty with its foreign tax id (VAT-UE)
    linked = mconn.execute("SELECT evidence -> 'iban_linked' FROM l3_decision_log WHERE id = %s",
                           (decision,)).fetchone()[0]
    assert linked["foreign_tax_id"] == "DE686148513" and linked["nip"] is None


def test_blik_for_a_hostess_proposes_the_pm_sheet_row_never_automatic(mconn, odm):
    result = result_for(mconn, odm.id, "202610053264356611")
    best = result.best
    assert best.kind == "internal_record" and best.items[0].label.endswith(":R01")
    assert {"person", "phone", "amount_exact", "record_date_in_title"} <= set(best.tags)
    assert not best.hard and any("D2" in b for b in best.blockers)
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, "202610053264356611") is None             # waits for the owner
    # a BLIK without any row is "to clarify", not a random row
    assert result_for(mconn, odm.id, "202610131625662595").best.final_status == "to_clarify"


def test_zus_tax_and_fees_are_no_document_hypotheses_closed_by_the_builtin_rule(mconn, odm):
    expected = {"202610157109429897": "zus", "202610208183657650": "tax", "202610266071538229": "tax",
                "ML2610196097579": "fee", "202610302699472862": "fee", "ML2610309084734": "interest"}
    for ref, subtype in expected.items():
        best = result_for(mconn, odm.id, ref).best
        assert (best.final_status, best.subtype, best.hard, best.rule_key) == (
            "no_document", subtype, True, "builtin:no_document_zus_tax_fee"), ref
    run_matching(mconn, odm, period=OCTOBER)
    for ref, subtype in expected.items():
        assert _status(mconn, ref)[:3] == ("no_document", subtype, "rule")


def test_advance_waits_180_days_and_a_card_30_days(mconn, odm):
    ref = "202610076358867832"                         # outgoing advance, no invoice yet
    best = result_for(mconn, odm.id, ref).best
    assert (best.final_status, best.subtype, best.awaiting_until) == (
        "no_document", "advance", date(2026, 10, 7) + timedelta(days=180))
    run_matching(mconn, odm, period=OCTOBER)
    suggestion = mconn.execute(
        "SELECT id FROM l2_suggestion WHERE subject_id = %s AND status = 'open' AND rank = 1",
        (tx_id(mconn, ref),)).fetchone()[0]
    from kasa.core.matching.decide import apply_match

    apply_match(mconn, suggestion, decided_by="artur")
    assert _status(mconn, ref) == ("no_document", "advance", "human", date(2027, 4, 5))
    # an incoming advance with the advance invoice already there is matched to it
    best = result_for(mconn, odm.id, "202610017411739420").best
    assert best.items[0].document_id == doc_id(mconn, "FV 1/10/2026", "sales") and "advance_invoice" in best.tags
    # a card payment without an invoice: card_awaiting, 30 days
    card = next(c for c in result_for(mconn, odm.id, "202610166224040945").considered if c.kind == "no_document")
    assert (card.subtype, card.awaiting_until) == ("card_awaiting", date(2026, 10, 16) + timedelta(days=30))


def test_overpayment_is_a_possible_duplicate_and_the_refund_links_to_it(mconn, odm):
    first, second, refund = "202610136613771164", "202610143818524718", "202610219260141951"
    results = {r.tx.entry_reference: r for r in evaluate(mconn, odm.id).results}
    # the first payment names the invoice; Orbita is known by name only - the owner closes it
    assert results[first].best.items[0].label == "FV 5/09/2026" and not results[first].best.hard
    run_matching(mconn, odm, period=OCTOBER)
    _accept_rank1(mconn, first)
    results = {r.tx.entry_reference: r for r in evaluate(mconn, odm.id).results}
    dup = results[second].best
    assert (dup.kind, dup.subtype, dup.items[0].related_transaction_id) == ("related", "duplicate", tx_id(mconn, refund))
    assert (dup.items[0].difference_kind, dup.items[0].difference_minor) == ("overpayment", 307500)
    invoice = next(c for c in results[second].considered if c.kind == "document")
    assert "possible_duplicate" in invoice.tags and not invoice.hard
    back = results[refund].best
    assert (back.subtype, back.items[0].related_transaction_id) == ("refund", tx_id(mconn, second))
    # never automatic, whatever the parameters
    relaxed = {"auto_threshold": 0.0, "margin_min": 0.0}
    assert not auto.eligibility(dup, relaxed, {})[0] and not auto.eligibility(back, relaxed, {})[0]
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, first)[:3] == ("matched", None, "human")
    assert _status(mconn, second) is None and _status(mconn, refund) is None


def test_tie_of_two_invoices_of_4500_is_never_automatic(mconn, odm):
    result = result_for(mconn, odm.id, "202610277149922608")
    first, a, b = result.candidates
    assert first.final_status == "to_clarify" and first.tie and "tie" in first.tags
    assert {a.items[0].label, b.items[0].label} == {"FV 14/2026", "FV 15/2026"}
    assert abs(a.score - b.score) < 0.02 and a.margin == 0.0
    run_matching(mconn, odm, period=OCTOBER)
    assert _status(mconn, "202610277149922608") is None


def test_invoice_number_with_a_lower_amount_is_a_partial_payment(mconn, odm):
    best = result_for(mconn, odm.id, "ML2610084922077").best
    item = best.items[0]
    assert (item.label, item.amount_minor, item.difference_kind, item.difference_minor) == (
        "FV 4/09/2026", 500000, "underpayment", -422500)
    assert "invoice_number" in best.tags and not best.hard


def test_own_transfers_are_closed_by_the_builtin_rule_with_the_pair_named(mconn, odm):
    refs = ["202610065634798198", "ML2610068440316", "ML2610299971452", "202610309011341667",
            "202610286656245067", "202610288152441704"]
    for ref in refs:
        best = result_for(mconn, odm.id, ref).best
        assert (best.final_status, best.hard, best.rule_key) == ("own_transfer", True, "builtin:own_transfer")
        assert "own_pair" in best.tags
    run_matching(mconn, odm, period=OCTOBER)
    assert all(_status(mconn, ref)[:3] == ("own_transfer", None, "rule") for ref in refs)


def test_same_number_at_another_nip_is_never_the_match(mconn, odm):
    # FV 7/10/2026 is our sale to ACME and the rent invoice of Biuro Centrum: direction decides
    rent = result_for(mconn, odm.id, "202610092920619715").best
    assert rent.items[0].document_id == doc_id(mconn, "FV 7/10/2026", "purchase") and rent.hard
    # FV 12/09/2026: Studio Kreska in KSeF and Biuro Centrum in JPK - the NIP decides
    kreska = result_for(mconn, odm.id, "ML2610095382734")
    other = next(c for c in kreska.candidates[1:] if c.kind == "document")
    assert kreska.best.hard and "counterparty_conflict" in other.tags and not other.hard


def test_sensitive_title_and_pending_holds(mconn, odm):
    best = result_for(mconn, odm.id, "202610096232801350").best
    assert best.final_status == "no_document" and not best.hard and "sensitive" in best.tags
    subjects = {r.tx.id for r in evaluate(mconn, odm.id).results}
    pending = {row[0] for row in mconn.execute(
        "SELECT id FROM l1_bank_transaction WHERE company_id = %s AND status = 'pending'", (odm.id,)).fetchall()}
    assert pending and not subjects & pending


def test_zawadzki_one_cost_many_proofs(mconn, odm):
    best = result_for(mconn, odm.id, "202610160785476080").best
    assert best.hard and best.items[0].label == "4/2026"
    assert [e["label"] for e in best.linked_evidence] == ["arkusz_pm_2026-10:R08"]
    assert all(c.kind != "internal_record" for c in result_for(mconn, odm.id, "202610160785476080").candidates)


def test_every_suggestion_has_the_contract_format(mconn, odm):
    run_matching(mconn, odm, period=OCTOBER)
    rows = mconn.execute(
        "SELECT subject_id, rank, proposal, evidence, score FROM l2_suggestion WHERE company_id = %s AND kind = 'match'",
        (odm.id,)).fetchall()
    assert rows
    per_subject: dict = {}
    for subject, rank, proposal, evidence, score in rows:
        per_subject.setdefault(subject, []).append(rank)
        assert set(proposal) == {"items", "final_status", "subtype", "awaiting_until"}
        for item in proposal["items"]:
            assert set(item) == {"document_id", "internal_record_id", "related_transaction_id", "amount_minor",
                                 "difference_kind", "difference_minor"}
            assert sum(item[k] is not None for k in ("document_id", "internal_record_id",
                                                     "related_transaction_id")) == 1
        assert {"facts", "badge", "precedents", "hard", "margin", "tags"} <= set(evidence)
        assert evidence["badge"] in ("high", "medium", "low") and 0 <= float(score) <= 1
        if evidence["badge"] == "high":
            assert evidence["hard"] and float(score) >= 0.95
        assert all(isinstance(f, str) and f for f in evidence["facts"])
    assert all(sorted(ranks) == list(range(1, len(ranks) + 1)) and len(ranks) <= 3 for ranks in per_subject.values())


def test_no_candidate_gives_no_document_first(mconn, odm):
    from .helpers import add_tx, party

    who = party(mconn, odm.id, "Firma Bez Faktur sp. z o.o.", None, "PL27114020040000300201355387")
    tx = add_tx(mconn, odm.id, -123456, "przelew", counterparty_id=who, name="Firma Bez Faktur sp. z o.o.",
                iban="PL27114020040000300201355387")
    best = only(mconn, odm.id, tx).best
    assert best.final_status == "no_document" and not best.hard
