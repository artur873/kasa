-- 0005_l3_decisions: shelf L3, decisions that are in force.
--
-- Only a human (method = 'human') or automation acting on a rule in stage
-- 'auto' (method = 'rule') may decide. The database enforces it:
--   * a row whose method is 'human' may be updated only by a write that is
--     itself 'human' (UPDATE allowed when NEW.method = 'human' or OLD.method <> 'human');
--   * DELETE and TRUNCATE are always forbidden: revoking is a new decision;
--   * a 'rule' write must point at an l3_rule row of the same company in stage 'auto';
--   * the decision log is append-only.
-- Exceptions to "human or rule": cost object and rule candidates proposed by
-- code (status/stage 'candidate') and the system cost object UNASSIGNED.

CREATE TYPE rule_stage AS ENUM ('candidate', 'hint', 'shadow', 'auto', 'suspended', 'dormant');
CREATE TYPE decision_kind AS ENUM (
    'match', 'allocation', 'category', 'transaction_status', 'cost_object', 'rule', 'revoke'
);
CREATE TYPE decision_status AS ENUM ('active', 'revoked');
CREATE TYPE cost_object_status AS ENUM ('candidate', 'approved', 'rejected', 'merged');
CREATE TYPE match_difference_kind AS ENUM ('fee', 'fx', 'underpayment', 'overpayment', 'tolerance');
CREATE TYPE final_transaction_status AS ENUM ('matched', 'no_document', 'own_transfer', 'to_clarify');

-- ---------------------------------------------------------------------------
-- Learned rules, versioned (a change of pattern or action = new version row).
-- ---------------------------------------------------------------------------
CREATE TABLE l3_rule (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    rule_key        text NOT NULL CHECK (btrim(rule_key) <> ''),
    version         integer NOT NULL CHECK (version > 0),
    stage           rule_stage NOT NULL,
    pattern         jsonb NOT NULL CHECK (jsonb_typeof(pattern) = 'object'),
    action          jsonb NOT NULL CHECK (jsonb_typeof(action) = 'object'),
    evidence_count  integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    exceptions      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(exceptions) = 'array'),
    decision_id     uuid,               -- last human decision about this rule (FK below)
    updated_at      timestamptz,
    updated_by      text,

    -- provenance
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL,
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l3_rule_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l3_rule_key_version_key UNIQUE (company_id, rule_key, version),
    CONSTRAINT l3_rule_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l3_rule_id_version_key UNIQUE (id, version),
    -- Mining may propose a candidate; every later stage is set by a human.
    CONSTRAINT l3_rule_method_check CHECK (method = 'human' OR (method = 'code' AND stage = 'candidate'))
);

CREATE INDEX l3_rule_company_stage_idx ON l3_rule (company_id, stage);

-- ---------------------------------------------------------------------------
-- Decision log: every decision (human click or auto rule), append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE l3_decision_log (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid NOT NULL REFERENCES company (id),
    kind                 decision_kind NOT NULL,
    subject_type         subject_type NOT NULL,
    subject_id           uuid NOT NULL,
    payload              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    rule_id              uuid,
    rule_version         integer,
    decided_by           text NOT NULL CHECK (btrim(decided_by) <> ''),
    decided_at           timestamptz NOT NULL DEFAULT now(),
    reverts_decision_id  uuid,
    suggestion_id        uuid,              -- the L2 suggestion this decision answers, if any
    reason               text,

    -- provenance
    source               text NOT NULL CHECK (btrim(source) <> ''),
    method               provenance_method NOT NULL CHECK (method IN ('human', 'rule')),
    confidence           numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version       text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l3_decision_log_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l3_decision_log_rule_pair_check CHECK ((rule_id IS NULL) = (rule_version IS NULL)),
    CONSTRAINT l3_decision_log_rule_required_check CHECK (method <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT l3_decision_log_revoke_check CHECK ((kind = 'revoke') = (reverts_decision_id IS NOT NULL)),
    CONSTRAINT l3_decision_log_not_self_revert CHECK (reverts_decision_id IS DISTINCT FROM id),
    CONSTRAINT l3_decision_log_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l3_decision_log_rule_company_fk FOREIGN KEY (rule_id, company_id) REFERENCES l3_rule (id, company_id),
    CONSTRAINT l3_decision_log_reverts_fk FOREIGN KEY (reverts_decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_decision_log_suggestion_fk FOREIGN KEY (suggestion_id, company_id)
        REFERENCES l2_suggestion (id, company_id)
);

CREATE INDEX l3_decision_log_company_time_idx ON l3_decision_log (company_id, decided_at);
CREATE INDEX l3_decision_log_subject_idx ON l3_decision_log (subject_type, subject_id);
CREATE INDEX l3_decision_log_reverts_idx ON l3_decision_log (reverts_decision_id) WHERE reverts_decision_id IS NOT NULL;
CREATE INDEX l3_decision_log_rule_idx ON l3_decision_log (rule_id) WHERE rule_id IS NOT NULL;

ALTER TABLE l3_rule
    ADD CONSTRAINT l3_rule_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id);

ALTER TABLE l2_suggestion
    ADD CONSTRAINT l2_suggestion_resolved_by_fk FOREIGN KEY (resolved_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id);

-- ---------------------------------------------------------------------------
-- Cost objects. The kind comes from the company extension; the core only
-- knows the reserved kind 'unassigned' of the system row UNASSIGNED.
-- ---------------------------------------------------------------------------
CREATE TABLE l3_cost_object (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES company (id),
    kind            text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    name            text NOT NULL CHECK (btrim(name) <> ''),
    attrs           jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(attrs) = 'object'),
    status          cost_object_status NOT NULL,
    merged_into     uuid,
    system_code     text CHECK (system_code IN ('UNASSIGNED')),
    decision_id     uuid,
    updated_at      timestamptz,
    updated_by      text,

    -- provenance
    source          text NOT NULL CHECK (btrim(source) <> ''),
    method          provenance_method NOT NULL,
    confidence      numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version  integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l3_cost_object_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l3_cost_object_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l3_cost_object_merged_fk FOREIGN KEY (merged_into, company_id)
        REFERENCES l3_cost_object (id, company_id),
    CONSTRAINT l3_cost_object_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_cost_object_merged_check CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
    CONSTRAINT l3_cost_object_not_self_merged CHECK (merged_into IS DISTINCT FROM id),
    CONSTRAINT l3_cost_object_system_kind_check CHECK ((kind = 'unassigned') = (system_code IS NOT NULL)),
    CONSTRAINT l3_cost_object_system_status_check CHECK (system_code IS NULL OR status = 'approved'),
    -- A cost object is never approved, merged or split by automation.
    CONSTRAINT l3_cost_object_method_check CHECK (
        method = 'human'
        OR (method IN ('code', 'model') AND status = 'candidate' AND system_code IS NULL)
        OR (method = 'code' AND system_code IS NOT NULL)
    )
);

CREATE UNIQUE INDEX l3_cost_object_system_code_key ON l3_cost_object (company_id, system_code)
    WHERE system_code IS NOT NULL;
CREATE INDEX l3_cost_object_company_kind_idx ON l3_cost_object (company_id, kind, status);

-- ---------------------------------------------------------------------------
-- Matches: transaction <-> document (or internal record). Several rows with
-- the same group_id form one settlement (3 invoices in one transfer, MPP legs).
-- ---------------------------------------------------------------------------
CREATE TABLE l3_match (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id             uuid NOT NULL REFERENCES company (id),
    transaction_id         uuid NOT NULL,
    document_id            uuid,
    internal_record_id     uuid,
    amount_minor           bigint NOT NULL,     -- part of the transaction, transaction currency
    document_amount_minor  bigint,              -- part of the document, document currency (when it differs)
    difference_kind        match_difference_kind,
    difference_minor       bigint,
    group_id               uuid NOT NULL DEFAULT gen_random_uuid(),
    status                 decision_status NOT NULL DEFAULT 'active',
    decision_id            uuid NOT NULL,
    revoked_by_decision    uuid,
    rule_id                uuid,
    rule_version           integer,
    updated_at             timestamptz,
    updated_by             text,

    -- provenance
    source                 text NOT NULL CHECK (btrim(source) <> ''),
    method                 provenance_method NOT NULL CHECK (method IN ('human', 'rule')),
    confidence             numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence               jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version         integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version         text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l3_match_target_check CHECK (num_nonnulls(document_id, internal_record_id) = 1),
    CONSTRAINT l3_match_difference_check CHECK ((difference_kind IS NULL) = (difference_minor IS NULL)),
    CONSTRAINT l3_match_revoked_check CHECK ((status = 'revoked') = (revoked_by_decision IS NOT NULL)),
    CONSTRAINT l3_match_rule_pair_check CHECK ((rule_id IS NULL) = (rule_version IS NULL)),
    CONSTRAINT l3_match_rule_required_check CHECK (method <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT l3_match_transaction_fk FOREIGN KEY (transaction_id, company_id)
        REFERENCES l1_bank_transaction (id, company_id),
    CONSTRAINT l3_match_document_fk FOREIGN KEY (document_id, company_id)
        REFERENCES l1_document (id, company_id),
    CONSTRAINT l3_match_internal_record_fk FOREIGN KEY (internal_record_id, company_id)
        REFERENCES l1_internal_record (id, company_id),
    CONSTRAINT l3_match_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_match_revoked_by_fk FOREIGN KEY (revoked_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_match_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l3_match_rule_company_fk FOREIGN KEY (rule_id, company_id) REFERENCES l3_rule (id, company_id)
);

CREATE UNIQUE INDEX l3_match_active_document_key ON l3_match (transaction_id, document_id)
    WHERE status = 'active' AND document_id IS NOT NULL;
CREATE UNIQUE INDEX l3_match_active_internal_key ON l3_match (transaction_id, internal_record_id)
    WHERE status = 'active' AND internal_record_id IS NOT NULL;
CREATE INDEX l3_match_company_idx ON l3_match (company_id, status);
CREATE INDEX l3_match_document_idx ON l3_match (document_id) WHERE document_id IS NOT NULL;
CREATE INDEX l3_match_group_idx ON l3_match (group_id);

-- ---------------------------------------------------------------------------
-- Allocations of a transaction, document, line or internal record to a cost
-- object, whole or in shares (truth check: shares of a subject sum to 100%).
-- ---------------------------------------------------------------------------
CREATE TABLE l3_allocation (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid NOT NULL REFERENCES company (id),
    subject_type         subject_type NOT NULL
                         CHECK (subject_type IN ('transaction', 'document', 'line', 'internal_record')),
    subject_id           uuid NOT NULL,
    cost_object_id       uuid NOT NULL,
    share                numeric(7,6) NOT NULL CHECK (share > 0 AND share <= 1),
    amount_minor         bigint,
    status               decision_status NOT NULL DEFAULT 'active',
    decision_id          uuid NOT NULL,
    revoked_by_decision  uuid,
    rule_id              uuid,
    rule_version         integer,
    updated_at           timestamptz,
    updated_by           text,

    -- provenance
    source               text NOT NULL CHECK (btrim(source) <> ''),
    method               provenance_method NOT NULL CHECK (method IN ('human', 'rule')),
    confidence           numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version       text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l3_allocation_revoked_check CHECK ((status = 'revoked') = (revoked_by_decision IS NOT NULL)),
    CONSTRAINT l3_allocation_rule_pair_check CHECK ((rule_id IS NULL) = (rule_version IS NULL)),
    CONSTRAINT l3_allocation_rule_required_check CHECK (method <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT l3_allocation_cost_object_fk FOREIGN KEY (cost_object_id, company_id)
        REFERENCES l3_cost_object (id, company_id),
    CONSTRAINT l3_allocation_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_allocation_revoked_by_fk FOREIGN KEY (revoked_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_allocation_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l3_allocation_rule_company_fk FOREIGN KEY (rule_id, company_id) REFERENCES l3_rule (id, company_id)
);

CREATE UNIQUE INDEX l3_allocation_active_key ON l3_allocation (subject_type, subject_id, cost_object_id)
    WHERE status = 'active';
CREATE INDEX l3_allocation_company_idx ON l3_allocation (company_id, status);
CREATE INDEX l3_allocation_cost_object_idx ON l3_allocation (cost_object_id);

-- ---------------------------------------------------------------------------
-- Category decisions (category names come from the company extension).
-- ---------------------------------------------------------------------------
CREATE TABLE l3_category_decision (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid NOT NULL REFERENCES company (id),
    subject_type         subject_type NOT NULL
                         CHECK (subject_type IN ('transaction', 'document', 'line', 'internal_record')),
    subject_id           uuid NOT NULL,
    category             text NOT NULL CHECK (btrim(category) <> ''),
    status               decision_status NOT NULL DEFAULT 'active',
    decision_id          uuid NOT NULL,
    revoked_by_decision  uuid,
    rule_id              uuid,
    rule_version         integer,
    updated_at           timestamptz,
    updated_by           text,

    -- provenance
    source               text NOT NULL CHECK (btrim(source) <> ''),
    method               provenance_method NOT NULL CHECK (method IN ('human', 'rule')),
    confidence           numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version       text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l3_category_decision_revoked_check CHECK ((status = 'revoked') = (revoked_by_decision IS NOT NULL)),
    CONSTRAINT l3_category_decision_rule_pair_check CHECK ((rule_id IS NULL) = (rule_version IS NULL)),
    CONSTRAINT l3_category_decision_rule_required_check CHECK (method <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT l3_category_decision_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_category_decision_revoked_by_fk FOREIGN KEY (revoked_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_category_decision_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l3_category_decision_rule_company_fk FOREIGN KEY (rule_id, company_id)
        REFERENCES l3_rule (id, company_id)
);

CREATE UNIQUE INDEX l3_category_decision_active_key ON l3_category_decision (subject_type, subject_id)
    WHERE status = 'active';
CREATE INDEX l3_category_decision_company_idx ON l3_category_decision (company_id, category);

-- ---------------------------------------------------------------------------
-- Final status of each booked transaction (exactly one active per transaction).
-- ---------------------------------------------------------------------------
CREATE TABLE l3_transaction_status (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid NOT NULL REFERENCES company (id),
    transaction_id       uuid NOT NULL,
    final_status         final_transaction_status NOT NULL,
    subtype              text,              -- e.g. zus, tax, fee, advance, deposit, card_awaiting_invoice
    status               decision_status NOT NULL DEFAULT 'active',
    decision_id          uuid NOT NULL,
    revoked_by_decision  uuid,
    rule_id              uuid,
    rule_version         integer,
    updated_at           timestamptz,
    updated_by           text,

    -- provenance
    source               text NOT NULL CHECK (btrim(source) <> ''),
    method               provenance_method NOT NULL CHECK (method IN ('human', 'rule')),
    confidence           numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version       text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l3_transaction_status_revoked_check CHECK ((status = 'revoked') = (revoked_by_decision IS NOT NULL)),
    CONSTRAINT l3_transaction_status_rule_pair_check CHECK ((rule_id IS NULL) = (rule_version IS NULL)),
    CONSTRAINT l3_transaction_status_rule_required_check CHECK (method <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT l3_transaction_status_transaction_fk FOREIGN KEY (transaction_id, company_id)
        REFERENCES l1_bank_transaction (id, company_id),
    CONSTRAINT l3_transaction_status_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_transaction_status_revoked_by_fk FOREIGN KEY (revoked_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l3_transaction_status_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l3_transaction_status_rule_company_fk FOREIGN KEY (rule_id, company_id)
        REFERENCES l3_rule (id, company_id)
);

CREATE UNIQUE INDEX l3_transaction_status_active_key ON l3_transaction_status (transaction_id)
    WHERE status = 'active';
CREATE INDEX l3_transaction_status_company_idx ON l3_transaction_status (company_id, final_status);

-- ===========================================================================
-- Guards
-- ===========================================================================

-- Main L3 guard: human decisions are protected, nothing is deleted, and
-- automation writes only on behalf of a rule in stage 'auto'.
CREATE FUNCTION kasa_l3_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_row       jsonb;
    v_rule_id   uuid;
    v_rule_ver  integer;
    v_rule      record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Usuwanie z % jest zabronione (id %)', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'KS002',
                  HINT = 'Cofnięcie decyzji to nowa decyzja typu revoke, nie kasowanie.';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.method = 'human' AND NEW.method <> 'human' THEN
            RAISE EXCEPTION 'Decyzję człowieka w % (id %) może zmienić tylko człowiek; próba zapisu metodą %',
                    TG_TABLE_NAME, OLD.id, NEW.method
                USING ERRCODE = 'KS003';
        END IF;
        IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
            RAISE EXCEPTION 'Nie można przenieść wiersza % (id %) do innej firmy', TG_TABLE_NAME, OLD.id
                USING ERRCODE = 'KS006';
        END IF;
        -- Generic for tables with or without updated_at.
        NEW := jsonb_populate_record(NEW, jsonb_build_object('updated_at', now()));
    END IF;

    IF NEW.method = 'rule' THEN
        v_row := to_jsonb(NEW);
        v_rule_id := (v_row ->> 'rule_id')::uuid;
        v_rule_ver := (v_row ->> 'rule_version')::integer;
        IF v_rule_id IS NULL THEN
            RAISE EXCEPTION 'Automat (method=rule) w % musi wskazać regułę w etapie auto (brak rule_id)', TG_TABLE_NAME
                USING ERRCODE = 'KS004';
        END IF;
        -- FOR SHARE: a concurrent suspension of the rule waits for this write.
        SELECT r.stage, r.version, r.company_id INTO v_rule
          FROM l3_rule r WHERE r.id = v_rule_id FOR SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Automat w %: reguła % nie istnieje', TG_TABLE_NAME, v_rule_id
                USING ERRCODE = 'KS004';
        END IF;
        IF v_rule.stage <> 'auto' THEN
            RAISE EXCEPTION 'Automat w %: reguła % jest w etapie %, a pisać może tylko reguła w etapie auto',
                    TG_TABLE_NAME, v_rule_id, v_rule.stage
                USING ERRCODE = 'KS004';
        END IF;
        IF v_rule.company_id <> NEW.company_id THEN
            RAISE EXCEPTION 'Automat w %: reguła % należy do innej firmy', TG_TABLE_NAME, v_rule_id
                USING ERRCODE = 'KS004';
        END IF;
        IF v_rule_ver IS DISTINCT FROM v_rule.version THEN
            RAISE EXCEPTION 'Automat w %: wersja reguły % to %, a nie %', TG_TABLE_NAME, v_rule_id, v_rule.version, v_rule_ver
                USING ERRCODE = 'KS004';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

-- Polymorphic subject must exist in the same company.
CREATE FUNCTION kasa_subject_exists(p_type subject_type, p_id uuid, p_company uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT CASE p_type
        WHEN 'transaction' THEN EXISTS (SELECT 1 FROM l1_bank_transaction WHERE id = p_id AND company_id = p_company)
        WHEN 'document' THEN EXISTS (SELECT 1 FROM l1_document WHERE id = p_id AND company_id = p_company)
        WHEN 'line' THEN EXISTS (SELECT 1 FROM l1_document_line WHERE id = p_id AND company_id = p_company)
        WHEN 'internal_record' THEN EXISTS (SELECT 1 FROM l1_internal_record WHERE id = p_id AND company_id = p_company)
        WHEN 'cost_object' THEN EXISTS (SELECT 1 FROM l3_cost_object WHERE id = p_id AND company_id = p_company)
        WHEN 'rule' THEN EXISTS (SELECT 1 FROM l3_rule WHERE id = p_id AND company_id = p_company)
        ELSE false
    END;
$$;

CREATE FUNCTION kasa_subject_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.subject_type IS NOT NULL
       AND NOT kasa_subject_exists(NEW.subject_type, NEW.subject_id, NEW.company_id) THEN
        RAISE EXCEPTION 'Podmiot % o id % nie istnieje w firmie % (tabela %)',
                NEW.subject_type, NEW.subject_id, NEW.company_id, TG_TABLE_NAME
            USING ERRCODE = 'KS005';
    END IF;
    RETURN NEW;
END;
$$;

-- An active allocation may point only at an approved cost object.
CREATE FUNCTION kasa_allocation_target_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_status cost_object_status;
BEGIN
    IF NEW.status = 'active' THEN
        SELECT co.status INTO v_status
          FROM l3_cost_object co
         WHERE co.id = NEW.cost_object_id AND co.company_id = NEW.company_id;
        IF v_status IS DISTINCT FROM 'approved' THEN
            RAISE EXCEPTION 'Alokacja do obiektu kosztowego % o statusie % jest niedozwolona (wymagany approved)',
                    NEW.cost_object_id, coalesce(v_status::text, 'brak')
                USING ERRCODE = 'KS007';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- Every company has exactly one system cost object UNASSIGNED.
CREATE FUNCTION kasa_company_unassigned() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO l3_cost_object (company_id, kind, name, status, system_code, source, method, evidence, created_by)
    VALUES (NEW.id, 'unassigned', 'UNASSIGNED', 'approved', 'UNASSIGNED', 'kasa.core', 'code',
            '{"note": "koszt ogólny - decyzja, nie brak decyzji"}'::jsonb, 'system')
    ON CONFLICT (company_id, system_code) WHERE system_code IS NOT NULL DO NOTHING;
    RETURN NULL;
END;
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'l3_rule', 'l3_decision_log', 'l3_cost_object', 'l3_match',
        'l3_allocation', 'l3_category_decision', 'l3_transaction_status'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION kasa_l3_guard()',
            t || '_guard', t);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate()',
            t || '_no_truncate', t);
    END LOOP;

    FOREACH t IN ARRAY ARRAY['l2_suggestion', 'l3_decision_log', 'l3_allocation', 'l3_category_decision'] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard()',
            t || '_subject_guard', t);
    END LOOP;
END;
$$;

CREATE TRIGGER l3_decision_log_append_only
    BEFORE UPDATE OR DELETE ON l3_decision_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();

CREATE TRIGGER l3_allocation_target_guard
    BEFORE INSERT OR UPDATE ON l3_allocation
    FOR EACH ROW EXECUTE FUNCTION kasa_allocation_target_guard();

CREATE TRIGGER company_unassigned
    AFTER INSERT ON company
    FOR EACH ROW EXECUTE FUNCTION kasa_company_unassigned();

-- Companies created before this migration get their UNASSIGNED too.
INSERT INTO l3_cost_object (company_id, kind, name, status, system_code, source, method, evidence, created_by)
SELECT c.id, 'unassigned', 'UNASSIGNED', 'approved', 'UNASSIGNED', 'kasa.core', 'code',
       '{"note": "koszt ogólny - decyzja, nie brak decyzji"}'::jsonb, 'system'
  FROM company c
ON CONFLICT (company_id, system_code) WHERE system_code IS NOT NULL DO NOTHING;
