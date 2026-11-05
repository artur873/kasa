"""Weights of the additive matching score and the default thresholds.

The score of a candidate is ``raw = BASE + sum(signal weights)`` (log-odds units). It becomes a
probability with a logistic function, ``p = 1 / (1 + exp(-(raw - bias) / temperature))``;
``temperature``, ``bias`` and the auto threshold are chosen by calibration on a labeled
control set (``kasa.core.matching.calibrate``) and read from ``calibration_result``.

Hard evidence (identifier, MPP block, invoice number with an agreeing counterparty, built-in
or auto rule, the only open invoice of the counterparty for exactly this amount) is a separate
flag of the candidate, not a weight: the automation needs ALL of p >= threshold, hard and a
margin over the second candidate (briefing, step 4).
"""

from __future__ import annotations

BASE = -3.0

WEIGHTS: dict[str, float] = {
    # layer 2: hard identifiers
    "ksef_number": 7.0,
    "mpp_block": 7.0,
    "invoice_number": 5.5,
    # counterparty (by id/NIP/IBAN = strong; same name; similar name or the name in the title)
    "counterparty_strong": 1.5,
    "counterparty_name": 1.0,
    "counterparty_similar": 0.5,
    "counterparty_title_name": 0.5,
    "counterparty_conflict": -3.0,
    "counterparty_different": -1.0,
    # amount
    "amount_exact": 2.0,
    "amount_tolerance": 0.8,
    "amount_fx_tolerance": 0.8,
    "underpayment": -1.0,
    "overpayment": -2.0,
    "subset_sum": 0.5,           # on top of amount_exact: 2-6 open invoices add up to the grosz
    # layer 3: counterparty, amount, time
    "single_open_invoice": 4.0,  # strong counterparty: the only open invoice for exactly this amount
    "single_open_invoice_weak": 1.0,
    "single_amount_any": 1.0,    # unknown payer: the only open document for exactly this amount
    "window": 0.3,
    "title_names_other_invoice": -4.0,
    "proforma": -1.0,
    "advance_invoice": 1.0,      # an advance payment and an advance invoice (ZAL)
    # duplicates and refunds
    "possible_duplicate": -3.0,
    "duplicate_of_earlier": 1.0,
    "refund_intent": 1.0,
    "refund_date_in_title": 1.5,
    "refunded_later": 1.5,
    # internal records (PM sheet)
    "person": 2.5,
    "phone": 1.5,
    "person_mismatch": -4.0,
    "record_date_close": 0.7,
    "record_date_in_title": 0.7,
    "record_label_in_title": 0.3,
    # layer 4: title semantics (soft)
    "intent_agrees": 0.3,
    "intent_conflicts": -0.5,
    # no document / own transfer / to clarify
    "builtin_no_document": 9.0,
    "builtin_own_transfer": 9.0,
    "own_pair": 0.5,
    "no_document_intent": 1.0,
    "no_document_keyword": 2.0,
    "no_document_person": 0.5,
    "no_document_card": 2.0,
    "no_document_fallback": 1.0,
    "to_clarify_blik_person": 2.5,
    "awaiting_ksef": 2.0,        # the title names an invoice that is not in KSeF yet: wait a day (never auto)
    # layer 5: learned rules (4C) by stage
    "rule_hint": 1.5,
    "rule_shadow": 0.0,
    "rule_auto": 4.0,
}

# Calibrated parameters (defaults until a calibration run chooses others).
DEFAULT_PARAMS: dict[str, float] = {
    "temperature": 1.0,
    "bias": 0.0,
    "auto_threshold": 0.995,     # briefing: calibrated certainty of at least 99.5%
    "margin_min": 0.2,           # clear advantage over the second candidate
}
MIN_AUTO_THRESHOLD = 0.995       # calibration may raise the threshold, never lower it
MIN_MARGIN = 0.2
TIE_MARGIN = 0.02                # contract: two suggestions within 0.02 = a tie, never auto

# Amount tolerances (briefing, step 4): only for ONE invoice per transfer; several = to the grosz.
TOLERANCE_PCT = "0.01"           # 1% or 20 zł, whichever is larger
TOLERANCE_MIN_MINOR = 2000
FX_TOLERANCE_PCT = "0.015"       # foreign currency: 1.5% or 50 zł
FX_TOLERANCE_MIN_MINOR = 5000

# Windows (days)
WINDOW_BEFORE = 30               # -30 .. +90 days around the due date (or the issue date, flagged)
WINDOW_AFTER = 90
RECORD_WINDOW_BEFORE = 3         # internal record dated up to 3 days after the transfer
RECORD_WINDOW_AFTER = 14         # ... or up to 14 days before it
RECORD_DATE_CLOSE = 3            # "data ±3 dni"
DUPLICATE_WINDOW = 30
REFUND_WINDOW = 90
OWN_PAIR_WINDOW = 3
ADVANCE_AWAIT_DAYS = 180
CARD_AWAIT_DAYS = 30
KSEF_AWAIT_DAYS = 1              # an invoice named in the title, not in KSeF yet: until the next run

SUBSET_MIN = 2
SUBSET_MAX = 6
SUBSET_POOL = 24                 # open invoices of one counterparty considered by the subset search
SUBSET_NODE_LIMIT = 50_000

MAX_SUGGESTIONS = 3


def badge(score: float, hard: bool) -> str:
    """high = score >= 0.95 and hard evidence; medium = score >= 0.7; low = the rest (contract)."""
    if score >= 0.95 and hard:
        return "high"
    if score >= 0.7:
        return "medium"
    return "low"
