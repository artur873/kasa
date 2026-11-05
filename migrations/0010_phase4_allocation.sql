-- 0010_phase4_allocation: phase 4 (part B) - cost object candidates discovered from the
-- data and allocation of lines, documents, internal rows and transactions to cost objects.
--
--   1. l2_cost_object_member: records grouped into a cost object candidate, with the
--      anchor and the strength of every link (review of phase 1, point 10).
--   2. l3_allocation.signal_strength (A-D) and cascade_step (review of phase 1, point 9).
--   3. pair_model_weights: m/u probabilities of the Fellegi-Sunter pair model, versioned.
--      Version 0 (company_id NULL) holds the priors; every retrain on human decisions writes
--      a new full version for the company. No weight lives in the code.
--   4. cost_object_pair_decision: a person's verdict about two cost objects ("different" =
--      never glued by discovery, "same" = merged by a person). Append-only.
--   5. l2_suggestion may name a cost object (cost_object_candidate) or a rule (rule_candidate).
--   6. Built-in rule builtin:allocation_explicit_identifier (stage auto) for every company:
--      the user's consent is the briefing of 3.11 (decision D3: automation only for signal A).
--
-- The core never names the kind of a cost object; the extension does (kasa/extensions/).

-- ===========================================================================
-- 1. Members of a cost object candidate
-- ===========================================================================

CREATE TYPE cost_object_member_type AS ENUM (
    'document_line',    -- l1_document_line
    'document',         -- l1_document (a document without lines that name anything)
    'transaction',      -- l1_bank_transaction
    'internal_record',  -- l1_internal_record (a row of an internal file)
    'email_case'        -- a case from the mail archive (offer with dates and place); not imported yet
);

CREATE TABLE l2_cost_object_member (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id            uuid NOT NULL REFERENCES company (id),
    candidate_id          uuid NOT NULL,
    record_type           cost_object_member_type NOT NULL,
    record_id             uuid NOT NULL,
    link_strength         numeric(5,4) NOT NULL CHECK (link_strength BETWEEN 0 AND 1),
    is_anchor             boolean NOT NULL DEFAULT false,
    status                suggestion_status NOT NULL DEFAULT 'open',
    resolved_at           timestamptz,
    resolved_by_decision  uuid,

    -- provenance ('human' = put there by a person: split or merge of candidates)
    source                text NOT NULL CHECK (btrim(source) <> ''),
    method                provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence            numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version        integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version        text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l2_cost_object_member_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l2_cost_object_member_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l2_cost_object_member_key UNIQUE (candidate_id, record_type, record_id),
    CONSTRAINT l2_cost_object_member_candidate_fk FOREIGN KEY (candidate_id, company_id)
        REFERENCES l3_cost_object (id, company_id),
    CONSTRAINT l2_cost_object_member_resolved_by_fk FOREIGN KEY (resolved_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l2_cost_object_member_resolved_check CHECK ((status = 'open') = (resolved_at IS NULL))
);

-- One anchor per candidate among its live members.
CREATE UNIQUE INDEX l2_cost_object_member_anchor_key ON l2_cost_object_member (candidate_id)
    WHERE is_anchor AND status IN ('open', 'accepted');
CREATE INDEX l2_cost_object_member_record_idx ON l2_cost_object_member (record_type, record_id);
CREATE INDEX l2_cost_object_member_company_idx ON l2_cost_object_member (company_id, status);

CREATE TRIGGER l2_cost_object_member_no_delete
    BEFORE DELETE ON l2_cost_object_member
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l2_cost_object_member_no_truncate
    BEFORE TRUNCATE ON l2_cost_object_member
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- The record of a member exists in the same company (KS005, as for polymorphic subjects).
CREATE FUNCTION kasa_cost_object_member_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_type subject_type;
BEGIN
    IF NEW.record_type = 'email_case' THEN
        RETURN NEW;
    END IF;
    v_type := CASE NEW.record_type
        WHEN 'document_line' THEN 'line'::subject_type
        ELSE NEW.record_type::text::subject_type
    END;
    IF NOT kasa_subject_exists(v_type, NEW.record_id, NEW.company_id) THEN
        RAISE EXCEPTION 'Rekord % o id % nie istnieje w firmie % (członek kandydata %)',
                NEW.record_type, NEW.record_id, NEW.company_id, NEW.candidate_id
            USING ERRCODE = 'KS005';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER l2_cost_object_member_record_guard
    BEFORE INSERT OR UPDATE ON l2_cost_object_member
    FOR EACH ROW EXECUTE FUNCTION kasa_cost_object_member_guard();

-- ===========================================================================
-- 2. Strength of the signal and step of the cascade on every allocation
-- ===========================================================================

ALTER TABLE l3_allocation
    ADD COLUMN signal_strength char(1) CHECK (signal_strength IN ('A', 'B', 'C', 'D')),
    ADD COLUMN cascade_step text CHECK (cascade_step IN (
        'line_description', 'offer_match', 'offer_value', 'person_days', 'equal', 'inherited'));

-- ===========================================================================
-- 3. Fellegi-Sunter pair model
-- ===========================================================================
-- feature/level:
--   name: exact | high | partial | none           (token overlap of names, weighted by rarity)
--   client: agree | disagree
--   dates: overlap | near | mid | far             (gap between the date windows)
--   city: agree | disagree
--   rare_token: yes | no                          (a shared rare token, not the client, not the city)
--   persons: shared | none                        (people of the internal files)
--   A missing value on either side contributes nothing (weight 0) - it has no row.
--   __prior__/match: m_prob = share of true pairs among compared pairs (lambda).
--   __threshold__/<name>: decision thresholds of grouping (merge, min_link, member, high, margin).
--   __param__/rare_df_ratio: a token is rare when it is in at most this share of records.
-- pseudo_count: weight of the row as a Dirichlet prior (equivalent number of pairs).

CREATE TABLE pair_model_weights (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id            uuid REFERENCES company (id),     -- NULL = priors shared by every company
    feature               text NOT NULL CHECK (feature ~ '^(__)?[a-z][a-z0-9_]*(__)?$'),
    level                 text NOT NULL CHECK (level ~ '^[a-z][a-z0-9_]*$'),
    m_prob                numeric(9,8) NOT NULL CHECK (m_prob > 0 AND m_prob <= 1),
    u_prob                numeric(9,8) CHECK (u_prob > 0 AND u_prob <= 1),
    pseudo_count          numeric(12,4) NOT NULL DEFAULT 0 CHECK (pseudo_count >= 0),
    version               integer NOT NULL CHECK (version >= 0),
    trained_on_decisions  integer NOT NULL DEFAULT 0 CHECK (trained_on_decisions >= 0),
    trained_on_pairs      jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(trained_on_pairs) = 'object'),
    source                text NOT NULL CHECK (btrim(source) <> ''),
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT pair_model_weights_key UNIQUE NULLS NOT DISTINCT (company_id, version, feature, level),
    CONSTRAINT pair_model_weights_prior_version_check CHECK ((company_id IS NULL) = (version = 0))
);

CREATE INDEX pair_model_weights_lookup_idx ON pair_model_weights (company_id, version DESC);

CREATE TRIGGER pair_model_weights_append_only
    BEFORE UPDATE OR DELETE ON pair_model_weights
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER pair_model_weights_no_truncate
    BEFORE TRUNCATE ON pair_model_weights
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- Priors (version 0). Reasonable starting values; the model learns from the person's decisions.
INSERT INTO pair_model_weights (company_id, feature, level, m_prob, u_prob, pseudo_count, version, source, created_by)
VALUES
    (NULL, 'name',       'exact',   0.55, 0.02, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'name',       'high',    0.30, 0.05, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'name',       'partial', 0.10, 0.18, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'name',       'none',    0.05, 0.75, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'client',     'agree',   0.85, 0.25, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'client',     'disagree', 0.15, 0.75, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'dates',      'overlap', 0.70, 0.06, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'dates',      'near',    0.15, 0.08, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'dates',      'mid',     0.10, 0.20, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'dates',      'far',     0.05, 0.66, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'city',       'agree',   0.88, 0.15, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'city',       'disagree', 0.12, 0.85, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'rare_token', 'yes',     0.75, 0.06, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'rare_token', 'no',      0.25, 0.94, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'persons',    'shared',  0.60, 0.01, 20, 0, 'prior', 'migration-0010'),
    (NULL, 'persons',    'none',    0.40, 0.99, 20, 0, 'prior', 'migration-0010'),
    (NULL, '__prior__',  'match',   0.02, 0.98, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__threshold__', 'merge',    0.90, NULL, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__threshold__', 'min_link', 0.50, NULL, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__threshold__', 'member',   0.60, NULL, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__threshold__', 'high',     0.90, NULL, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__threshold__', 'margin',   0.15, NULL, 0, 0, 'prior', 'migration-0010'),
    (NULL, '__param__',  'rare_df_ratio', 0.25, NULL, 0, 0, 'prior', 'migration-0010');

-- ===========================================================================
-- 4. A person's verdicts about pairs of cost objects
-- ===========================================================================

CREATE TYPE cost_object_pair_verdict AS ENUM ('same', 'different');

CREATE TABLE cost_object_pair_decision (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   uuid NOT NULL REFERENCES company (id),
    a_id         uuid NOT NULL,
    b_id         uuid NOT NULL,
    verdict      cost_object_pair_verdict NOT NULL,
    decision_id  uuid NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT cost_object_pair_decision_order_check CHECK (a_id < b_id),
    CONSTRAINT cost_object_pair_decision_a_fk FOREIGN KEY (a_id, company_id) REFERENCES l3_cost_object (id, company_id),
    CONSTRAINT cost_object_pair_decision_b_fk FOREIGN KEY (b_id, company_id) REFERENCES l3_cost_object (id, company_id),
    CONSTRAINT cost_object_pair_decision_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id)
);

CREATE INDEX cost_object_pair_decision_pair_idx ON cost_object_pair_decision (company_id, a_id, b_id, created_at DESC);

CREATE TRIGGER cost_object_pair_decision_append_only
    BEFORE UPDATE OR DELETE ON cost_object_pair_decision
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER cost_object_pair_decision_no_truncate
    BEFORE TRUNCATE ON cost_object_pair_decision
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- 5. Suggestions about a cost object (candidate card) or a rule (rule card)
-- ===========================================================================

ALTER TABLE l2_suggestion DROP CONSTRAINT l2_suggestion_subject_type_check;
ALTER TABLE l2_suggestion
    ADD CONSTRAINT l2_suggestion_subject_type_check
        CHECK (subject_type IS NULL
               OR subject_type IN ('transaction', 'document', 'line', 'internal_record', 'cost_object', 'rule'));

-- ===========================================================================
-- 6. Built-in rule: allocation by an explicit identifier (signal A) is automatic
-- ===========================================================================

CREATE FUNCTION kasa_seed_builtin_allocation_rule(p_company_id uuid) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO l3_rule (company_id, rule_key, version, stage, pattern, action, evidence_count,
                         source, method, confidence, evidence, created_by)
    VALUES (p_company_id, 'builtin:allocation_explicit_identifier', 1, 'auto',
            '{"all": [{"field": "signal_strength", "op": "eq", "value": "A"},
                      {"field": "tags", "op": "in", "value": ["explicit_identifier"]},
                      {"field": "contradiction", "op": "eq", "value": false}]}'::jsonb,
            '{"kind": "allocation", "strategy": "explicit_identifier"}'::jsonb,
            0, 'briefing', 'human', 1,
            '{"note": "Krok 5 briefingu i decyzja D3: automat alokacji tylko dla sygnału A (jawny identyfikator w pozycji, tytule lub mailu, bez sprzeczności); arkusz PM nie jest twardym dowodem (D2)."}'::jsonb,
            'briefing-2026-11-03')
    ON CONFLICT (company_id, rule_key, version) DO NOTHING;
$$;

CREATE FUNCTION kasa_company_builtin_allocation_rule() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM kasa_seed_builtin_allocation_rule(NEW.id);
    RETURN NULL;
END;
$$;

CREATE TRIGGER company_builtin_allocation_rule
    AFTER INSERT ON company
    FOR EACH ROW EXECUTE FUNCTION kasa_company_builtin_allocation_rule();

SELECT kasa_seed_builtin_allocation_rule(c.id) FROM company c;
