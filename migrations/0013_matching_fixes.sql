-- 0013_matching_fixes: fixes of the review of phases 4A and 4B (docs/PRZEGLADY.md, "Fazy 4A i 4B").
--
--   a. l3_match.evidence_only - "one cost, many pieces of evidence" (briefing, step 5; review point 6):
--      a row of an internal file (PM sheet) that proves the same cost as the document of a match is
--      written into the SAME group (group_id) as a row with amount 0 and evidence_only = true. It is
--      evidence, not a settlement: truth check 2 (coverage) skips it, the balance of the row is
--      closed by it (the cost is paid through the document), and reports follow the group instead
--      of guessing by person, amount and date.
--
-- Independent of 0012 (reserved for phase 5): the runner applies migrations by version, and this file
-- uses only objects of 0001-0011.

ALTER TABLE l3_match ADD COLUMN evidence_only boolean NOT NULL DEFAULT false;

-- Evidence is a row of an internal file with no amount of its own (the money is on the document row
-- of the same group); it never names a difference.
ALTER TABLE l3_match
    ADD CONSTRAINT l3_match_evidence_only_check
        CHECK (NOT evidence_only OR (internal_record_id IS NOT NULL AND amount_minor = 0
                                     AND coalesce(document_amount_minor, 0) = 0 AND difference_kind IS NULL));

CREATE INDEX l3_match_evidence_group_idx ON l3_match (group_id) WHERE evidence_only;

COMMENT ON COLUMN l3_match.evidence_only IS
    'Dowód tego samego kosztu (wiersz arkusza PM przy rachunku/fakturze), nie rozliczenie: kwota 0, ta sama '
    'group_id co wiersz dokumentu. Kontrola prawdy 2 go pomija; raport liczy koszt raz - z dokumentu grupy.';
