-- 0009_phase4_matching: phase 4, matching transfers to invoices (briefing, step 4).
--
--   a. l3_transaction_status.awaiting_until - an advance waits 180 days for its invoice, a card
--      payment 30 days (docs/PRZEGLADY.md, phase 1 point 5).
--   b. l3_match.related_transaction_id - a third target: a refund <-> the original payment, a
--      duplicate payment <-> its refund (phase 1 point 11); difference_kind 'correction' and the
--      convention of a negative amount for a correction inside a group (phase 1 point 8).
--   c. Card holds: l1_bank_transaction.resolved_by_transaction_id (the booked operation that
--      replaced the hold) instead of superseded_by, which is reserved for versions of a fact, and
--      the status 'expired' for a hold that lapsed (phase 2 point 5).
--   d. calibration_result - thresholds chosen on a labeled control set (gate 4).
--   e. Built-in rules from the briefing (user consent = the briefing of 3.11.2026), seeded for
--      every company in stage 'auto' with method 'human'; the user may suspend each of them.
--
-- Enum values added here (ALTER TYPE ... ADD VALUE) are not used anywhere in this file:
-- PostgreSQL allows adding them inside the runner's transaction only when nothing uses them
-- before commit.

-- ===========================================================================
-- a. Awaiting a document
-- ===========================================================================

-- no_document/advance: booking date + 180 days; no_document/card_awaiting: + 30 days. After
-- that date the transfer comes back to the review queue (the missing invoice is a finding).
ALTER TABLE l3_transaction_status ADD COLUMN awaiting_until date;

COMMENT ON COLUMN l3_transaction_status.awaiting_until IS
    'Do kiedy czekamy na dokument: zaliczka = data księgowania + 180 dni, karta = + 30 dni; NULL = nie czekamy.';

-- ===========================================================================
-- b. Matches: a third target and corrections
-- ===========================================================================

ALTER TABLE l3_match
    ADD COLUMN related_transaction_id uuid,
    ADD CONSTRAINT l3_match_related_transaction_fk FOREIGN KEY (related_transaction_id, company_id)
        REFERENCES l1_bank_transaction (id, company_id),
    ADD CONSTRAINT l3_match_not_self_related CHECK (related_transaction_id IS DISTINCT FROM transaction_id);

-- Exactly one target: a document, a row of an internal file or another transaction.
ALTER TABLE l3_match DROP CONSTRAINT l3_match_target_check;
ALTER TABLE l3_match
    ADD CONSTRAINT l3_match_target_check
        CHECK (num_nonnulls(document_id, internal_record_id, related_transaction_id) = 1);

CREATE UNIQUE INDEX l3_match_active_related_key ON l3_match (transaction_id, related_transaction_id)
    WHERE status = 'active' AND related_transaction_id IS NOT NULL;
CREATE INDEX l3_match_related_idx ON l3_match (related_transaction_id) WHERE related_transaction_id IS NOT NULL;
CREATE INDEX l3_match_internal_record_idx ON l3_match (internal_record_id) WHERE internal_record_id IS NOT NULL;
CREATE INDEX l3_match_transaction_idx ON l3_match (transaction_id, status);

ALTER TYPE match_difference_kind ADD VALUE IF NOT EXISTS 'correction';

COMMENT ON COLUMN l3_match.amount_minor IS
    'Część przelewu (waluta przelewu), dodatnia; wiersz korekty w grupie ma kwotę UJEMNĄ '
    '(faktura 10 000 + korekta -2 000 = przelew 8 000, dwa wiersze jednej grupy, '
    'difference_kind = correction przy korekcie). Saldo dokumentu liczymy tylko z wierszy active: '
    'status revoked zdejmuje kwotę z salda.';
COMMENT ON COLUMN l3_match.document_amount_minor IS
    'Część dokumentu w walucie dokumentu (np. EUR przy fakturze walutowej); ujemna przy korekcie.';
COMMENT ON COLUMN l3_match.difference_minor IS
    'Nazwana różnica w walucie przelewu: kwota przelewu minus wartość dokumentu w PLN '
    '(fee, fx, underpayment, overpayment, tolerance, correction). Kwot nigdy nie poprawiamy.';
COMMENT ON COLUMN l3_match.related_transaction_id IS
    'Trzeci cel dopasowania: zwrot <-> pierwotna wpłata, duplikat wpłaty <-> jego zwrot.';

-- ===========================================================================
-- c. Card holds: resolution is a state, not a version of the fact
-- ===========================================================================

ALTER TYPE bank_tx_status ADD VALUE IF NOT EXISTS 'expired';

ALTER TABLE l1_bank_transaction
    ADD COLUMN resolved_by_transaction_id uuid,
    ADD CONSTRAINT l1_bank_transaction_resolved_by_fk FOREIGN KEY (resolved_by_transaction_id, company_id)
        REFERENCES l1_bank_transaction (id, company_id),
    ADD CONSTRAINT l1_bank_transaction_not_self_resolved CHECK (resolved_by_transaction_id IS DISTINCT FROM id),
    -- only a hold is resolved by a booking; a booked operation is never "resolved"
    ADD CONSTRAINT l1_bank_transaction_resolved_hold_check
        CHECK (resolved_by_transaction_id IS NULL OR status <> 'booked');

CREATE INDEX l1_bank_transaction_resolved_by_idx ON l1_bank_transaction (resolved_by_transaction_id)
    WHERE resolved_by_transaction_id IS NOT NULL;

COMMENT ON COLUMN l1_bank_transaction.resolved_by_transaction_id IS
    'Blokada kartowa (pending) zastąpiona zaksięgowaną operacją: id tej operacji. superseded_by '
    'zostaje wyłącznie dla wersji faktu (nowy parser), nie dla zmiany stanu blokady.';

-- Holds resolved before this migration carried the booking in superseded_by: move it.
UPDATE l1_bank_transaction p
   SET resolved_by_transaction_id = p.superseded_by, superseded_by = NULL
  FROM l1_bank_transaction b
 WHERE b.id = p.superseded_by AND p.status = 'pending' AND b.status = 'booked';

-- ===========================================================================
-- d. Calibration of the matching score
-- ===========================================================================

-- One row per evaluated parameter set of one calibration run (run_id); the row the run
-- chose has chosen = true. The engine reads the chosen parameters of the latest run.
-- Append-only: a new calibration is a new run, the old one stays as evidence.
CREATE TABLE calibration_result (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          uuid NOT NULL,
    company_id      uuid NOT NULL REFERENCES company (id),
    control_set_id  uuid NOT NULL,
    kind            text NOT NULL DEFAULT 'match' CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    params          jsonb NOT NULL CHECK (jsonb_typeof(params) = 'object'),
    metrics         jsonb NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    chosen          boolean NOT NULL DEFAULT false,
    run_at          timestamptz NOT NULL DEFAULT now(),
    run_by          text NOT NULL DEFAULT current_user,

    CONSTRAINT calibration_result_control_set_fk FOREIGN KEY (control_set_id, company_id)
        REFERENCES control_set (id, company_id)
);

CREATE UNIQUE INDEX calibration_result_one_chosen_per_run ON calibration_result (run_id) WHERE chosen;
CREATE INDEX calibration_result_lookup_idx ON calibration_result (company_id, kind, run_at DESC) WHERE chosen;

CREATE TRIGGER calibration_result_append_only
    BEFORE UPDATE OR DELETE ON calibration_result
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER calibration_result_no_truncate
    BEFORE TRUNCATE ON calibration_result
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- e. Built-in rules of the briefing (step 4), stage auto, consent = briefing of 3.11.2026
-- ===========================================================================

-- The pattern says, in the rule language of the phase 4 contract, which transfers the rule
-- covers; the matching itself is code (kasa/core/matching/engine.py), the rule is what makes
-- the automation legal (KS004: a rule write needs a rule in stage auto). method = 'human':
-- the user agreed to these rules in the briefing (created_by = 'briefing-2026-11-03').
-- The guards are unchanged: a human row may be changed only by a human (KS003), so the user
-- suspends a built-in rule like any other.
CREATE FUNCTION kasa_seed_builtin_rules(p_company_id uuid) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_count integer;
    v_quote_auto text := 'Automat wymaga naraz trzech rzeczy: skalibrowanej pewności co najmniej 99,5% (...), '
        'twardego dowodu (identyfikator, blok MPP, numer faktury przy zgodnym kontrahencie, reguła auto albo '
        'jedyna otwarta faktura kontrahenta na dokładnie tę kwotę) i wyraźnej przewagi nad drugim kandydatem. '
        'Brak któregokolwiek = kolejka.';
BEGIN
    INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, evidence_count, source, method,
                         confidence, evidence, created_by)
    SELECT p_company_id, r.rule_key, 1, 'auto', r.pattern, r.action, 0, 'briefing', 'human', 1,
           jsonb_build_object(
               'source', 'docs/BRIEFING.md, Krok 4 - jak łączymy przelew z fakturą',
               'consent', 'briefing z 3.11.2026 (zgoda użytkownika)',
               'quote', r.quote,
               'quote_auto', v_quote_auto,
               'implemented_in', 'kasa/core/matching/engine.py',
               'builtin', true),
           'briefing-2026-11-03'
      FROM (VALUES
        ('builtin:match_ksef_number',
         '{"all": [{"field": "title_regex", "op": "regex", "value": "[0-9]{10}-[0-9]{8}-[0-9A-F]{12}-[0-9A-F]{2}"}]}'::jsonb,
         '{"kind": "match_strategy", "strategy": "ksef_number", "requires": ["amount_exact"]}'::jsonb,
         'Twarde identyfikatory. Numer KSeF lub identyfikator zbiorczy w tytule (obowiązkowe w przelewach B2B '
         'od 1.01.2027) (...). Kwota zgodna → automat. Kwota niezgodna → warstwa 3, ale sygnał zostaje.'),
        ('builtin:match_mpp_block',
         '{"all": [{"field": "title_regex", "op": "regex", "value": "/VAT/[^/]+/IDC/[^/]+/INV/"}]}'::jsonb,
         '{"kind": "match_strategy", "strategy": "mpp_block", "requires": ["amount_exact", "nip_agrees"]}'::jsonb,
         'Twarde identyfikatory. (...) blok MPP z numerem i NIP-em (...). Kwota zgodna → automat. Split payment: '
         'dwie nogi = jedno zdarzenie dopasowane do faktury na kwotę brutto.'),
        ('builtin:match_invoice_number_same_counterparty',
         '{"all": [{"field": "title_regex", "op": "regex", "value": "[0-9]+[/-][0-9]+"}]}'::jsonb,
         '{"kind": "match_strategy", "strategy": "invoice_number_same_counterparty", "requires": ["amount_exact", "counterparty_agrees"]}'::jsonb,
         'Twarde identyfikatory. (...) numer faktury w tytule. Kwota zgodna → automat. (...) twardego dowodu '
         '(... numer faktury przy zgodnym kontrahencie ...).'),
        ('builtin:match_single_open_invoice_exact_amount',
         '{"all": [{"field": "counterparty_id", "op": "regex", "value": ".+"}]}'::jsonb,
         '{"kind": "match_strategy", "strategy": "single_open_invoice_exact_amount", "requires": ["amount_exact", "counterparty_by_iban_or_nip"]}'::jsonb,
         'Kontrahent, kwota, czas. Wiemy, kto przelał → bierzemy jego otwarte faktury → szukamy dokładnej kwoty '
         'w oknie od -30 do +90 dni wokół terminu. (...) twardego dowodu (... jedyna otwarta faktura kontrahenta '
         'na dokładnie tę kwotę).'),
        ('builtin:own_transfer',
         '{"all": [{"field": "op_type", "op": "eq", "value": "own"}]}'::jsonb,
         '{"kind": "final_status", "final_status": "own_transfer", "subtype": null}'::jsonb,
         'Przygotowanie (kod). (...) przelewy między własnymi kontami oznaczone jako „transfer własny”. '
         'Split payment: (...) przeksięgowanie = transfer własny.'),
        ('builtin:no_document_zus_tax_fee',
         '{"all": [{"field": "op_type", "op": "in", "value": ["zus", "tax", "fee", "interest"]}, {"field": "counterparty_kind", "op": "in", "value": ["institution", "bank", null]}]}'::jsonb,
         '{"kind": "final_status", "final_status": "no_document", "subtype": null, "subtype_by_op_type": {"zus": "zus", "tax": "tax", "fee": "fee", "interest": "interest"}}'::jsonb,
         'Brak dokumentu w ogóle: ZUS, US, prowizja (...). To osobna hipoteza z podtypami, nie „nie znaleźliśmy”.')
      ) AS r (rule_key, pattern, action, quote)
    ON CONFLICT (company_id, rule_key, version) DO NOTHING;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

CREATE FUNCTION kasa_company_builtin_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM kasa_seed_builtin_rules(NEW.id);
    RETURN NULL;
END;
$$;

-- Every new company gets the built-in rules (like its UNASSIGNED cost object).
CREATE TRIGGER company_builtin_rules
    AFTER INSERT ON company
    FOR EACH ROW EXECUTE FUNCTION kasa_company_builtin_rules();

SELECT kasa_seed_builtin_rules(c.id) FROM company c;
