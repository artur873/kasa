-- 0004_l2_suggestions: shelf L2, proposals made by code, rules and models.
-- A suggestion never pretends to be a fact or a decision; it only changes status.

CREATE TYPE suggestion_kind AS ENUM (
    'match', 'allocation', 'category', 'cost_object_candidate', 'rule_candidate', 'counterparty'
);
CREATE TYPE suggestion_status AS ENUM ('open', 'accepted', 'rejected', 'expired');

CREATE TABLE l2_suggestion (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id            uuid NOT NULL REFERENCES company (id),
    kind                  suggestion_kind NOT NULL,
    subject_type          subject_type,
    subject_id            uuid,
    proposal              jsonb NOT NULL CHECK (jsonb_typeof(proposal) = 'object'),
    score                 numeric,          -- raw score of the proposer; calibrated probability goes to confidence
    rank                  integer CHECK (rank > 0),
    status                suggestion_status NOT NULL DEFAULT 'open',
    expires_at            timestamptz,
    resolved_at           timestamptz,
    resolved_by_decision  uuid,             -- FK to l3_decision_log, added in 0005

    -- provenance
    source                text NOT NULL CHECK (btrim(source) <> ''),
    method                provenance_method NOT NULL CHECK (method <> 'human'),
    confidence            numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version        integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version        text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l2_suggestion_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l2_suggestion_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l2_suggestion_subject_pair_check CHECK ((subject_type IS NULL) = (subject_id IS NULL)),
    CONSTRAINT l2_suggestion_subject_type_check
        CHECK (subject_type IS NULL OR subject_type IN ('transaction', 'document', 'line', 'internal_record')),
    CONSTRAINT l2_suggestion_subject_required_check
        CHECK (subject_type IS NOT NULL OR kind IN ('cost_object_candidate', 'rule_candidate')),
    CONSTRAINT l2_suggestion_resolved_check CHECK ((status = 'open') = (resolved_at IS NULL))
);

CREATE INDEX l2_suggestion_queue_idx ON l2_suggestion (company_id, status, kind);
CREATE INDEX l2_suggestion_subject_idx ON l2_suggestion (subject_type, subject_id) WHERE subject_id IS NOT NULL;

CREATE TRIGGER l2_suggestion_no_delete
    BEFORE DELETE ON l2_suggestion
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();

CREATE TRIGGER l2_suggestion_no_truncate
    BEFORE TRUNCATE ON l2_suggestion
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
