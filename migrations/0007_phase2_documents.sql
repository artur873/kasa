-- 0007_phase2_documents: documents channel of phase 2 (KSeF FA(3), JPK, CSV/XLSX
-- import wizard, PDF from e-mails read by a model, deduplication).
--
-- This migration relies only on objects from 0001-0005: it must apply whether or
-- not 0006 (bank, counterparties, pseudonymization) is present. Every change is
-- written so that it also survives 0006 having added the same thing first.
--
-- Enum values added here (ALTER TYPE ... ADD VALUE) are not used anywhere in this
-- file: PostgreSQL 12+ allows adding them inside the runner's transaction as long
-- as nothing uses the new value before commit.

-- ---------------------------------------------------------------------------
-- Enum values
-- ---------------------------------------------------------------------------

-- A bill ("rachunek") of a person or a non-VAT seller is neither a VAT invoice
-- nor a receipt; the PDF channel brings such documents (briefing, step 1).
ALTER TYPE document_kind ADD VALUE IF NOT EXISTS 'bill';

-- Deduplication that cannot decide (same NIP and number, different date or
-- amount) creates a new document and proposes "this may be a duplicate of X"
-- for the review queue (briefing, step 2: "reszta do kolejki").
ALTER TYPE suggestion_kind ADD VALUE IF NOT EXISTS 'duplicate';

-- ---------------------------------------------------------------------------
-- Company tax id: the direction of an invoice is decided by our own NIP
-- (Podmiot1 = us -> sales, Podmiot2 = us -> purchase). Filled from the KSeF
-- session context, from JPK Podmiot1 or by hand; NULL until known.
-- ---------------------------------------------------------------------------
ALTER TABLE company ADD COLUMN IF NOT EXISTS nip text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'company'::regclass AND conname = 'company_nip_format_check'
    ) THEN
        ALTER TABLE company
            ADD CONSTRAINT company_nip_format_check CHECK (nip IS NULL OR nip ~ '^[0-9]{10}$');
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Missing columns of document facts.
-- ---------------------------------------------------------------------------

-- "We store everything": source fields the parser does not map to a column
-- (FA(3) elements, extra CSV columns) are kept here instead of being dropped.
ALTER TABLE l1_document
    ADD COLUMN IF NOT EXISTS extra jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(extra) = 'object');

-- Lines: unmapped line fields (GTU, indeks, KursWaluty, P_6A ...) and what the
-- line is: a normal invoice line, an order line of an advance invoice (amounts of
-- the whole order, FA(3) ZamowienieWiersz) or a correction delta (after - before).
ALTER TABLE l1_document_line
    ADD COLUMN IF NOT EXISTS extra jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(extra) = 'object'),
    ADD COLUMN IF NOT EXISTS line_kind text NOT NULL DEFAULT 'invoice'
        CHECK (line_kind IN ('invoice', 'order', 'correction'));

-- Observations: the version of the parser that read the payload. A newer parser
-- re-reading the same payload creates a new document version (superseded_by);
-- the same version re-reading it creates nothing (idempotent import).
ALTER TABLE l1_document_observation
    ADD COLUMN IF NOT EXISTS parser_version integer CHECK (parser_version > 0);

CREATE INDEX IF NOT EXISTS l1_document_observation_raw_idx ON l1_document_observation (raw_id, locator);
CREATE INDEX IF NOT EXISTS l1_document_current_number_idx
    ON l1_document (company_id, direction, number_normalized) WHERE superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- Lineage of document versions (a derived view, not a shelf table - no provenance
-- columns, hence no l1_ prefix): every version (current or superseded) mapped
-- to the current head of its chain. Observations, relations and matches made
-- against an older version resolve to the current document through this view.
-- ---------------------------------------------------------------------------
CREATE VIEW document_lineage AS
WITH RECURSIVE chain (document_id, head_id, company_id, depth) AS (
    SELECT d.id, d.id, d.company_id, 0
      FROM l1_document d
     WHERE d.superseded_by IS NULL
    UNION ALL
    SELECT d.id, c.head_id, c.company_id, c.depth + 1
      FROM l1_document d
      JOIN chain c ON d.superseded_by = c.document_id
)
SELECT document_id, head_id, company_id, depth FROM chain;

-- ---------------------------------------------------------------------------
-- Import wizard profiles.
-- Why: the column mapping confirmed once for a kind of file (identified by its
-- headers) must be remembered, so the next such file imports without questions
-- (briefing, step 1: "mapowanie kolumn zapisane raz").
-- The extension declares what the fields mean; the profile records which header of
-- this particular export holds which field. A changed mapping is a new row and the
-- old one gets superseded_by (nothing deleted).
-- ---------------------------------------------------------------------------
CREATE TABLE import_mapping_profile (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    mapping_key     text NOT NULL CHECK (mapping_key ~ '^[a-z][a-z0-9_]*$'),
    file_signature  text NOT NULL CHECK (btrim(file_signature) <> ''),   -- normalized headers, in order
    headers         jsonb NOT NULL CHECK (jsonb_typeof(headers) = 'array'),   -- headers as written in the file
    file_format     jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(file_format) = 'object'),
    column_map      jsonb NOT NULL CHECK (jsonb_typeof(column_map) = 'object'),  -- field -> header
    superseded_by   uuid,
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT import_mapping_profile_id_company_key UNIQUE (id, company_id),
    CONSTRAINT import_mapping_profile_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES import_mapping_profile (id, company_id),
    CONSTRAINT import_mapping_profile_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id)
);

CREATE UNIQUE INDEX import_mapping_profile_current_key
    ON import_mapping_profile (company_id, mapping_key, file_signature) WHERE superseded_by IS NULL;

CREATE TRIGGER import_mapping_profile_no_delete
    BEFORE DELETE ON import_mapping_profile
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER import_mapping_profile_no_truncate
    BEFORE TRUNCATE ON import_mapping_profile
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ---------------------------------------------------------------------------
-- Model reading of a PDF.
-- Why not l1_document_observation: the paid model reading happens once per (file,
-- model, prompt version) and must be kept even when no document can be built from it,
-- whereas an observation always belongs to a document.
-- It also records what went to the model (digest of the pseudonymized text, gate
-- result) and the code check that the lines add up to the totals.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_pdf_extraction (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    raw_id          uuid NOT NULL,
    document_id     uuid,                    -- document built from this reading (NULL = none)
    model           text NOT NULL CHECK (btrim(model) <> ''),
    input_sha256    text CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),   -- text sent to the model
    pseudonymized   boolean NOT NULL,        -- the text passed the pseudonymization gate
    extracted       jsonb NOT NULL CHECK (jsonb_typeof(extracted) = 'object'),
    lines_sum_ok    boolean,                 -- code check: lines add up to the totals
    needs_review    boolean NOT NULL DEFAULT false,
    superseded_by   uuid,

    -- provenance
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL CHECK (method = 'model'),
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text NOT NULL CHECK (btrim(prompt_version) <> ''),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l1_pdf_extraction_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_pdf_extraction_once_key UNIQUE (raw_id, model, prompt_version),
    CONSTRAINT l1_pdf_extraction_raw_fk FOREIGN KEY (raw_id, company_id)
        REFERENCES l0_raw (id, company_id),
    CONSTRAINT l1_pdf_extraction_document_fk FOREIGN KEY (document_id, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l1_pdf_extraction_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_pdf_extraction (id, company_id),
    CONSTRAINT l1_pdf_extraction_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id)
);

CREATE INDEX l1_pdf_extraction_company_idx ON l1_pdf_extraction (company_id, created_at);
CREATE INDEX l1_pdf_extraction_document_idx ON l1_pdf_extraction (document_id) WHERE document_id IS NOT NULL;

CREATE TRIGGER l1_pdf_extraction_no_delete
    BEFORE DELETE ON l1_pdf_extraction
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l1_pdf_extraction_no_truncate
    BEFORE TRUNCATE ON l1_pdf_extraction
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
