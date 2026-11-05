-- 0003_l1_facts: shelf L1, facts written by parsers (code) and models.
--
-- Facts are versioned: a new parser or prompt version inserts a new row and
-- points the old one at it through superseded_by. The old row stays.
-- Every table carries the provenance block (company_id ... created_by).
-- Rules never write facts (method <> 'rule').

CREATE TYPE document_kind AS ENUM (
    'vat', 'correction', 'advance', 'settlement', 'proforma', 'internal', 'receipt', 'foreign'
);
CREATE TYPE document_direction AS ENUM ('sales', 'purchase');
CREATE TYPE dedup_key AS ENUM (
    'created',          -- the observation created the document
    'ksef_number',      -- linked by KSeF number
    'file_hash',        -- linked by file digest
    'nip_kind_number',  -- linked by (NIP, kind, normalized number) with matching date and amount
    'human'             -- linked by a person in the review queue
);
CREATE TYPE bank_account_kind AS ENUM ('current', 'vat', 'savings');
CREATE TYPE bank_op_type AS ENUM (
    'transfer', 'instant', 'card', 'blik', 'fee', 'tax', 'zus', 'own', 'vat', 'other'
);
CREATE TYPE bank_tx_status AS ENUM ('booked', 'pending');

-- ---------------------------------------------------------------------------
-- Documents (invoices and other documents), one row per document version.
-- Amounts are minor units of the document currency; PLN is derived via fx_rate.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_document (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id                  uuid NOT NULL REFERENCES company (id),
    direction                   document_direction NOT NULL,

    -- identity
    ksef_number                 text,
    number_raw                  text NOT NULL,
    number_normalized           text NOT NULL
                                CHECK (number_normalized <> '' AND number_normalized !~ '[[:space:][:punct:]]'),
    kind                        document_kind NOT NULL,
    file_hash                   text CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    issuer_software             text,

    -- dates
    issue_date                  date NOT NULL,
    sale_date                   date,
    service_period_start        date,
    service_period_end          date,
    ksef_accepted_at            timestamptz,
    due_date                    date,       -- optional in FA(3); NULL does not mean "payable now"

    -- parties
    seller_nip                  text CHECK (seller_nip ~ '^[0-9]{10}$'),
    seller_foreign_tax_id       text,       -- VAT-UE / foreign tax id when there is no Polish NIP
    seller_name                 text,
    buyer_nip                   text CHECK (buyer_nip ~ '^[0-9]{10}$'),
    buyer_foreign_tax_id        text,
    buyer_name                  text,
    payment_iban                text,
    third_parties               jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(third_parties) = 'array'),

    -- amounts
    currency                    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    net_minor                   bigint,
    vat_minor                   bigint,
    gross_minor                 bigint NOT NULL,
    fx_rate                     numeric(18,10) CHECK (fx_rate > 0),
    fx_rate_date                date,
    fx_rate_source              text,       -- 'document' or e.g. 'nbp_a'
    gross_pln_minor             bigint,
    vat_summary                 jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(vat_summary) = 'array'),

    -- relations
    corrects_document_id        uuid,
    corrects_ref                jsonb,      -- raw reference as printed: number, KSeF number, issue date
    settles_advance_document_id uuid,
    advance_refs                jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(advance_refs) = 'array'),
    order_number                text,
    contract_ref                text,

    -- flags
    mpp_required                boolean NOT NULL DEFAULT false,
    cash_method                 boolean NOT NULL DEFAULT false,
    reverse_charge              boolean NOT NULL DEFAULT false,

    -- versioning
    superseded_by               uuid,

    -- provenance
    source                      text NOT NULL CHECK (btrim(source) <> ''),
    method                      provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence                  numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence                    jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version              integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version              text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    created_by                  text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_document_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_document_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_document_corrects_fk FOREIGN KEY (corrects_document_id, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l1_document_advance_fk FOREIGN KEY (settles_advance_document_id, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l1_document_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l1_document_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    CONSTRAINT l1_document_not_self_correcting CHECK (corrects_document_id IS DISTINCT FROM id),
    CONSTRAINT l1_document_service_period_check
        CHECK (service_period_start IS NULL OR service_period_end IS NULL OR service_period_start <= service_period_end),
    CONSTRAINT l1_document_correction_ref_check
        CHECK (kind <> 'correction' OR corrects_document_id IS NOT NULL OR corrects_ref IS NOT NULL),
    CONSTRAINT l1_document_pln_fx_check
        CHECK (currency <> 'PLN' OR (fx_rate IS NULL AND (gross_pln_minor IS NULL OR gross_pln_minor = gross_minor))),
    -- The KSeF number is the strongest identity: one current document per number.
    CONSTRAINT l1_document_ksef_current_excl
        EXCLUDE USING btree (company_id WITH =, ksef_number WITH =)
        WHERE (ksef_number IS NOT NULL AND superseded_by IS NULL)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX l1_document_number_idx ON l1_document (company_id, number_normalized);
CREATE INDEX l1_document_ksef_idx ON l1_document (ksef_number) WHERE ksef_number IS NOT NULL;
CREATE INDEX l1_document_seller_idx ON l1_document (company_id, seller_nip, kind, number_normalized);
CREATE INDEX l1_document_buyer_idx ON l1_document (company_id, buyer_nip);
CREATE INDEX l1_document_issue_date_idx ON l1_document (company_id, issue_date);
CREATE INDEX l1_document_file_hash_idx ON l1_document (file_hash) WHERE file_hash IS NOT NULL;
CREATE INDEX l1_document_current_idx ON l1_document (company_id, direction, issue_date) WHERE superseded_by IS NULL;

-- ---------------------------------------------------------------------------
-- Document lines. A new document version gets its own lines.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_document_line (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        uuid NOT NULL REFERENCES company (id),
    document_id       uuid NOT NULL,
    line_no           integer NOT NULL CHECK (line_no > 0),
    name              text NOT NULL,
    quantity          numeric(18,6),
    unit              text,
    unit_price        numeric(20,8),    -- exact price as printed (may have more than 2 decimals)
    unit_price_minor  bigint,
    net_minor         bigint,
    vat_rate          text,             -- '23', '8', '5', '0', 'zw', 'np', 'oo', ...
    vat_minor         bigint,
    gross_minor       bigint,
    before_json       jsonb,            -- correction: line state before
    after_json        jsonb,            -- correction: line state after

    -- provenance
    source            text NOT NULL CHECK (btrim(source) <> ''),
    method            provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence        numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version    integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version    text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_document_line_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_document_line_no_key UNIQUE (document_id, line_no),
    CONSTRAINT l1_document_line_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_document_line_document_fk FOREIGN KEY (document_id, company_id)
        REFERENCES l1_document (id, company_id)
);

CREATE INDEX l1_document_line_company_idx ON l1_document_line (company_id);

-- ---------------------------------------------------------------------------
-- Observations: one sighting of a document in one raw payload (KSeF XML,
-- PDF, JPK row...). A document has many observations - this is deduplication.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_document_observation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    document_id     uuid NOT NULL,
    raw_id          uuid NOT NULL,
    locator         text,               -- position inside the payload (JPK element, sheet row); NULL = whole payload
    linked_by       dedup_key NOT NULL,
    observed        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(observed) = 'object'),

    -- provenance
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_document_observation_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_document_observation_key UNIQUE NULLS NOT DISTINCT (raw_id, locator, document_id),
    CONSTRAINT l1_document_observation_document_fk FOREIGN KEY (document_id, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l1_document_observation_raw_fk FOREIGN KEY (raw_id, company_id)
        REFERENCES l0_raw (id, company_id)
);

CREATE INDEX l1_document_observation_document_idx ON l1_document_observation (document_id);
CREATE INDEX l1_document_observation_company_idx ON l1_document_observation (company_id);

-- ---------------------------------------------------------------------------
-- Bank accounts of the company (including the VAT account for split payment).
-- ---------------------------------------------------------------------------
CREATE TABLE l1_bank_account (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id         uuid NOT NULL REFERENCES company (id),
    iban               text NOT NULL CHECK (iban ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$'),
    currency           char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    bank               text,
    kind               bank_account_kind NOT NULL,
    name               text,
    parent_account_id  uuid,            -- VAT account -> its current account

    -- provenance
    source             text NOT NULL CHECK (btrim(source) <> ''),
    method             provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence         numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence           jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version     integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version     text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    created_by         text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_bank_account_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_bank_account_iban_key UNIQUE (company_id, iban, currency),
    CONSTRAINT l1_bank_account_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_bank_account_parent_fk FOREIGN KEY (parent_account_id, company_id)
        REFERENCES l1_bank_account (id, company_id),
    CONSTRAINT l1_bank_account_vat_pln_check CHECK (kind <> 'vat' OR currency = 'PLN')
);

-- ---------------------------------------------------------------------------
-- Bank transactions. Pending card holds live here too (status = 'pending')
-- and never enter the balance; a booked row supersedes the pending one.
-- fingerprint = account + date + amount + title (+ ordinal of identical
-- operations on that day), used when the bank gives no entry_reference.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_bank_transaction (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id              uuid NOT NULL REFERENCES company (id),
    account_id              uuid NOT NULL,
    entry_reference         text,
    fingerprint             text NOT NULL CHECK (btrim(fingerprint) <> ''),
    status                  bank_tx_status NOT NULL,
    booking_date            date,
    value_date              date,
    amount_minor            bigint NOT NULL,        -- signed: inflow > 0, outflow < 0
    currency                char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    original_amount_minor   bigint,                 -- card/FX operations: amount in the original currency
    original_currency       char(3) CHECK (original_currency ~ '^[A-Z]{3}$'),
    fx_rate                 numeric(18,10) CHECK (fx_rate > 0),
    balance_after_minor     bigint,
    counterparty_name_raw   text,
    counterparty_iban_raw   text,
    title_raw               text,
    extracted               jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(extracted) = 'object'),
    op_type                 bank_op_type NOT NULL DEFAULT 'other',
    superseded_by           uuid,

    -- provenance
    source                  text NOT NULL CHECK (btrim(source) <> ''),
    method                  provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence              numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence                jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version          integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version          text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    created_by              text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_bank_transaction_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_bank_transaction_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_bank_transaction_account_fk FOREIGN KEY (account_id, company_id)
        REFERENCES l1_bank_account (id, company_id),
    CONSTRAINT l1_bank_transaction_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_bank_transaction (id, company_id),
    CONSTRAINT l1_bank_transaction_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    CONSTRAINT l1_bank_transaction_booked_date_check CHECK (status <> 'booked' OR booking_date IS NOT NULL),
    CONSTRAINT l1_bank_transaction_original_amount_check
        CHECK ((original_amount_minor IS NULL) = (original_currency IS NULL)),
    -- Re-fetching the same history never duplicates a current transaction.
    CONSTRAINT l1_bank_transaction_entry_ref_current_excl
        EXCLUDE USING btree (account_id WITH =, entry_reference WITH =)
        WHERE (entry_reference IS NOT NULL AND superseded_by IS NULL)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT l1_bank_transaction_fingerprint_current_excl
        EXCLUDE USING btree (account_id WITH =, fingerprint WITH =)
        WHERE (superseded_by IS NULL)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX l1_bank_transaction_company_booking_idx ON l1_bank_transaction (company_id, booking_date);
CREATE INDEX l1_bank_transaction_account_booking_idx ON l1_bank_transaction (account_id, booking_date);
CREATE INDEX l1_bank_transaction_fingerprint_idx ON l1_bank_transaction (fingerprint);
CREATE INDEX l1_bank_transaction_entry_ref_idx ON l1_bank_transaction (entry_reference) WHERE entry_reference IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Exchange rates. company_id NULL = shared reference data (e.g. NBP table A).
-- rate = PLN for 1 unit of currency (NBP rates quoted per 100 are divided).
-- ---------------------------------------------------------------------------
CREATE TABLE l1_fx_rate (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid REFERENCES company (id),
    rate_date       date NOT NULL,
    currency        char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$' AND currency <> 'PLN'),
    rate            numeric(18,10) NOT NULL CHECK (rate > 0),
    table_ref       text,               -- e.g. NBP table number

    -- provenance (company_id above)
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_fx_rate_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_fx_rate_key UNIQUE NULLS NOT DISTINCT (company_id, source, currency, rate_date)
);

CREATE INDEX l1_fx_rate_lookup_idx ON l1_fx_rate (currency, rate_date);

-- ---------------------------------------------------------------------------
-- Model reading of a transfer title: intent and entities, written once per
-- prompt version, from pseudonymized text only.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_title_semantics (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    transaction_id  uuid NOT NULL,
    intent          text,
    entities        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(entities) = 'object'),

    -- provenance
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL CHECK (method = 'model'),
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l1_title_semantics_prompt_key UNIQUE (transaction_id, prompt_version),
    CONSTRAINT l1_title_semantics_transaction_fk FOREIGN KEY (transaction_id, company_id)
        REFERENCES l1_bank_transaction (id, company_id)
);

CREATE INDEX l1_title_semantics_company_idx ON l1_title_semantics (company_id);

-- ---------------------------------------------------------------------------
-- Rows of internal files (e.g. a project manager sheet, a rent roll), parsed
-- with an import mapping declared by the company extension.
-- ---------------------------------------------------------------------------
CREATE TABLE l1_internal_record (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        uuid NOT NULL REFERENCES company (id),
    file_raw_id       uuid NOT NULL,
    mapping_key       text NOT NULL CHECK (btrim(mapping_key) <> ''),
    sheet             text,
    row_no            integer NOT NULL CHECK (row_no > 0),
    fields            jsonb NOT NULL CHECK (jsonb_typeof(fields) = 'object'),
    person_pseudonym  text,
    record_date       date,
    amount_minor      bigint,
    currency          char(3) NOT NULL DEFAULT 'PLN' CHECK (currency ~ '^[A-Z]{3}$'),
    cost_object_ref   text,             -- cost object named in the file, as written
    settlement_type   text,
    superseded_by     uuid,

    -- provenance
    source            text NOT NULL CHECK (btrim(source) <> ''),
    method            provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence        numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version    integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version    text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_internal_record_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_internal_record_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_internal_record_raw_fk FOREIGN KEY (file_raw_id, company_id)
        REFERENCES l0_raw (id, company_id),
    CONSTRAINT l1_internal_record_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_internal_record (id, company_id),
    CONSTRAINT l1_internal_record_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    -- Importing the same file twice never duplicates current rows.
    CONSTRAINT l1_internal_record_row_current_excl
        EXCLUDE USING btree (file_raw_id WITH =, (coalesce(sheet, '')) WITH =, row_no WITH =)
        WHERE (superseded_by IS NULL)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX l1_internal_record_company_date_idx ON l1_internal_record (company_id, record_date);
CREATE INDEX l1_internal_record_person_idx ON l1_internal_record (company_id, person_pseudonym)
    WHERE person_pseudonym IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Nothing is deleted from L1.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'l1_document', 'l1_document_line', 'l1_document_observation', 'l1_bank_account',
        'l1_bank_transaction', 'l1_fx_rate', 'l1_title_semantics', 'l1_internal_record'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete()',
            t || '_no_delete', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate()',
            t || '_no_truncate', t);
    END LOOP;
END;
$$;
