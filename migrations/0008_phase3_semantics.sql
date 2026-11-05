-- 0008_phase3_semantics: phase 3 (semantics and control sets).
--
--   1. l1_semantics: what a model (or code, when no call is needed) read from the
--      text of a transaction title, a document line, an internal record row or a
--      document (docs/PRZEGLADY.md, point 12). l1_title_semantics stays (nothing
--      is deleted); new code writes here. View title_semantics_current.
--   2. llm_call_log: every model call, also a blocked one - input only as a digest
--      and a length; llm_call_payload keeps the full pseudonymized prompt and the
--      answer for 90 days (briefing, step 9), expired only by kasa_expire_llm_payloads().
--   3. Control sets: reference labels for calibration (briefing, step 7). They are
--      NOT decisions (never in l3_*); a label is written once, a change is a row
--      in control_set_label_history (written by the database).
--   4. sensitive_term: sensitive words as data (stems with Polish inflection,
--      docs/PRZEGLADY.md, point 15), seeded from the core and the extensions.
--   + review of phase 2: l1_pdf_extraction.method may be 'code' (regex reading),
--     company.nip_evidence, pseudonym_key.basis (why a value is a person).
--
-- New error codes:
--   KS009  control set: frozen set, immutable item identity
--   KS010  llm_call_payload: only expiry by kasa_expire_llm_payloads() may change a row
--   KS011  sensitive_term: only retiring (retired_at) may change a row

-- ===========================================================================
-- 1. Model semantics, generalized
-- ===========================================================================

CREATE TABLE l1_semantics (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id           uuid NOT NULL REFERENCES company (id),
    subject_type         subject_type NOT NULL
                         CHECK (subject_type IN ('transaction', 'line', 'internal_record', 'document')),
    subject_id           uuid NOT NULL,
    purpose              text NOT NULL CHECK (purpose ~ '^[a-z][a-z0-9_]*$'),   -- title_intent, line_entities
    intent               text,
    entities             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(entities) = 'object'),
    summary              text,
    model                text NOT NULL CHECK (btrim(model) <> ''),   -- 'code' when no call was needed
    input_pseudonymized  text NOT NULL,       -- exactly the data part the model saw (after the gate)
    llm_call_id          uuid,                -- llm_call_log row of the call (NULL: no call)
    superseded_by        uuid,

    -- provenance
    source               text NOT NULL CHECK (btrim(source) <> ''),
    method               provenance_method NOT NULL CHECK (method IN ('model', 'code')),
    confidence           numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version       text NOT NULL CHECK (btrim(prompt_version) <> ''),
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l1_semantics_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_semantics_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_semantics (id, company_id),
    CONSTRAINT l1_semantics_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    CONSTRAINT l1_semantics_code_model_check CHECK ((method = 'code') = (model = 'code'))
);

-- One current reading per (subject, prompt version, model): running again costs nothing.
CREATE UNIQUE INDEX l1_semantics_current_key
    ON l1_semantics (subject_type, subject_id, purpose, prompt_version, model) WHERE superseded_by IS NULL;
CREATE INDEX l1_semantics_company_idx ON l1_semantics (company_id, subject_type, created_at);

CREATE TRIGGER l1_semantics_no_delete
    BEFORE DELETE ON l1_semantics
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l1_semantics_no_truncate
    BEFORE TRUNCATE ON l1_semantics
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
CREATE TRIGGER l1_semantics_subject_guard
    BEFORE INSERT OR UPDATE ON l1_semantics
    FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard();

-- The latest current reading of each transaction title (new table first, the
-- phase 1 table l1_title_semantics only where the new one has nothing).
CREATE VIEW title_semantics_current AS
SELECT DISTINCT ON (company_id, transaction_id)
       id, company_id, transaction_id, intent, entities, summary, model, prompt_version, method, confidence,
       created_at, origin
  FROM (
      SELECT s.id, s.company_id, s.subject_id AS transaction_id, s.intent, s.entities, s.summary, s.model,
             s.prompt_version, s.method, s.confidence, s.created_at, 'l1_semantics'::text AS origin, 0 AS rank
        FROM l1_semantics s
       WHERE s.subject_type = 'transaction' AND s.purpose = 'title_intent' AND s.superseded_by IS NULL
      UNION ALL
      SELECT t.id, t.company_id, t.transaction_id, t.intent, t.entities, NULL::text,
             coalesce(t.evidence ->> 'model', 'unknown'), t.prompt_version, t.method, t.confidence, t.created_at,
             'l1_title_semantics'::text, 1
        FROM l1_title_semantics t
  ) readings
 ORDER BY company_id, transaction_id, rank, created_at DESC, id;

-- ===========================================================================
-- 2. Model call log and payloads (retention 90 days)
-- ===========================================================================

CREATE TYPE llm_call_status AS ENUM ('ok', 'blocked', 'error');

-- One row per attempted call. The input is never stored as text here: only the
-- SHA-256 of the prompt actually sent (NULL when nothing was sent) and its length.
CREATE TABLE llm_call_log (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid NOT NULL REFERENCES company (id),
    provider         text NOT NULL CHECK (btrim(provider) <> ''),
    model            text,
    prompt_version   text,
    purpose          text NOT NULL CHECK (purpose ~ '^[a-z][a-z0-9_]*$'),   -- the "tool" of the call
    subject_type     subject_type,
    subject_id       uuid,
    leaves_machine   boolean NOT NULL,
    input_hash       text CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    input_chars      integer NOT NULL DEFAULT 0 CHECK (input_chars >= 0),
    output_hash      text CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    output_chars     integer CHECK (output_chars >= 0),
    latency_ms       integer CHECK (latency_ms >= 0),
    cost_estimate    numeric(14,6) NOT NULL DEFAULT 0 CHECK (cost_estimate >= 0),
    status           llm_call_status NOT NULL,
    blocked_reason   text CHECK (blocked_reason IN ('sensitive', 'gate_unavailable', 'leak_suspected')),
    error_kind       text,                -- exception class or 'invalid_output', never the message text
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text NOT NULL DEFAULT current_user,

    CONSTRAINT llm_call_log_id_company_key UNIQUE (id, company_id),
    CONSTRAINT llm_call_log_blocked_check CHECK ((status = 'blocked') = (blocked_reason IS NOT NULL)),
    CONSTRAINT llm_call_log_blocked_no_input CHECK (status <> 'blocked' OR input_hash IS NULL),
    CONSTRAINT llm_call_log_subject_pair_check CHECK ((subject_type IS NULL) = (subject_id IS NULL))
);

CREATE INDEX llm_call_log_company_idx ON llm_call_log (company_id, created_at);
CREATE INDEX llm_call_log_subject_idx ON llm_call_log (subject_type, subject_id) WHERE subject_id IS NOT NULL;

CREATE TRIGGER llm_call_log_append_only
    BEFORE UPDATE OR DELETE ON llm_call_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER llm_call_log_no_truncate
    BEFORE TRUNCATE ON llm_call_log
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

ALTER TABLE l1_semantics
    ADD CONSTRAINT l1_semantics_call_fk FOREIGN KEY (llm_call_id, company_id)
        REFERENCES llm_call_log (id, company_id);

-- Full prompt (after pseudonymization) and answer of a call that was sent.
-- Kept 90 days, then emptied by kasa_expire_llm_payloads(): the row, the call
-- and the digests stay, the text is gone.
CREATE TABLE llm_call_payload (
    call_id               uuid PRIMARY KEY,
    company_id            uuid NOT NULL REFERENCES company (id),
    prompt_pseudonymized  text,
    response              text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    expires_at            timestamptz NOT NULL DEFAULT now() + interval '90 days',
    expired_at            timestamptz,

    CONSTRAINT llm_call_payload_call_fk FOREIGN KEY (call_id, company_id) REFERENCES llm_call_log (id, company_id),
    CONSTRAINT llm_call_payload_expired_check
        CHECK (expired_at IS NULL OR (prompt_pseudonymized IS NULL AND response IS NULL)),
    CONSTRAINT llm_call_payload_expiry_after_creation CHECK (expires_at > created_at)
);

CREATE INDEX llm_call_payload_expiry_idx ON llm_call_payload (expires_at) WHERE expired_at IS NULL;

CREATE FUNCTION kasa_llm_payload_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND coalesce(current_setting('kasa.expire_payload', true), '') = 'on'
       AND OLD.expired_at IS NULL
       AND NEW.expired_at IS NOT NULL
       AND NEW.prompt_pseudonymized IS NULL
       AND NEW.response IS NULL
       AND (NEW.call_id, NEW.company_id, NEW.created_at, NEW.expires_at)
           IS NOT DISTINCT FROM (OLD.call_id, OLD.company_id, OLD.created_at, OLD.expires_at)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS010',
              HINT = 'Prompty i odpowiedzi modelu wygasają tylko przez SELECT kasa_expire_llm_payloads().';
END;
$$;

CREATE TRIGGER llm_call_payload_guard
    BEFORE UPDATE OR DELETE ON llm_call_payload
    FOR EACH ROW EXECUTE FUNCTION kasa_llm_payload_guard();
CREATE TRIGGER llm_call_payload_no_truncate
    BEFORE TRUNCATE ON llm_call_payload
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- Retention of prompts and answers (briefing, step 9: 90 days). Empties every
-- payload past its expires_at (as of p_now) and returns how many were emptied.
CREATE FUNCTION kasa_expire_llm_payloads(p_now timestamptz DEFAULT now()) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    v_count integer;
BEGIN
    PERFORM set_config('kasa.expire_payload', 'on', true);
    UPDATE llm_call_payload
       SET prompt_pseudonymized = NULL, response = NULL, expired_at = p_now
     WHERE expired_at IS NULL AND expires_at <= p_now;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    PERFORM set_config('kasa.expire_payload', 'off', true);
    RETURN v_count;
END;
$$;

-- ===========================================================================
-- 3. Control sets (reference labels, not decisions)
-- ===========================================================================

CREATE TYPE control_set_kind AS ENUM ('transaction', 'cost');

CREATE TABLE control_set (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    uuid NOT NULL REFERENCES company (id),
    name          text NOT NULL CHECK (btrim(name) <> ''),
    kind          control_set_kind NOT NULL,
    size_target   integer NOT NULL CHECK (size_target > 0),
    seed          bigint,
    params        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(params) = 'object'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL CHECK (btrim(created_by) <> ''),
    frozen_at     timestamptz,          -- membership closed: no item can be added
    completed_at  timestamptz,          -- every item labeled (sentinel results become visible)

    CONSTRAINT control_set_id_company_key UNIQUE (id, company_id),
    CONSTRAINT control_set_name_key UNIQUE (company_id, name),
    CONSTRAINT control_set_completed_check CHECK (completed_at IS NULL OR frozen_at IS NOT NULL)
);

CREATE TABLE control_set_item (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id             uuid NOT NULL,
    company_id         uuid NOT NULL,
    subject_type       subject_type NOT NULL
                       CHECK (subject_type IN ('transaction', 'document', 'line', 'internal_record')),
    subject_id         uuid NOT NULL,
    order_no           integer NOT NULL CHECK (order_no > 0),
    label              jsonb CHECK (label IS NULL OR jsonb_typeof(label) = 'object'),
    labeled_by         text,
    labeled_at         timestamptz,
    duration_ms        integer CHECK (duration_ms >= 0),
    shown_at           timestamptz,     -- when the card was last sent (duration = answer - shown_at)
    is_sentinel        boolean NOT NULL DEFAULT false,
    sentinel_expected  jsonb CHECK (sentinel_expected IS NULL OR jsonb_typeof(sentinel_expected) = 'object'),
    sentinel_passed    boolean,         -- set by code when a sentinel gets its label

    CONSTRAINT control_set_item_id_company_key UNIQUE (id, company_id),
    CONSTRAINT control_set_item_set_fk FOREIGN KEY (set_id, company_id) REFERENCES control_set (id, company_id),
    CONSTRAINT control_set_item_order_key UNIQUE (set_id, order_no),
    CONSTRAINT control_set_item_subject_key UNIQUE (set_id, subject_type, subject_id),
    CONSTRAINT control_set_item_label_pair_check
        CHECK ((label IS NULL) = (labeled_at IS NULL) AND (label IS NULL) = (labeled_by IS NULL)),
    CONSTRAINT control_set_item_sentinel_check CHECK (is_sentinel OR sentinel_expected IS NULL),
    CONSTRAINT control_set_item_sentinel_result_check CHECK (sentinel_passed IS NULL OR (is_sentinel AND label IS NOT NULL))
);

CREATE INDEX control_set_item_pending_idx ON control_set_item (set_id, order_no) WHERE label IS NULL;

-- Every change of a label that was already given (the first label lives on the item).
CREATE TABLE control_set_label_history (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id      uuid NOT NULL,
    company_id   uuid NOT NULL,
    old_label    jsonb NOT NULL,
    old_by       text NOT NULL,
    old_at       timestamptz NOT NULL,
    new_label    jsonb NOT NULL,
    new_by       text NOT NULL,
    changed_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT control_set_label_history_item_fk FOREIGN KEY (item_id, company_id)
        REFERENCES control_set_item (id, company_id)
);

CREATE INDEX control_set_label_history_item_idx ON control_set_label_history (item_id, changed_at);

CREATE FUNCTION kasa_control_set_item_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_frozen timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Usuwanie pozycji zbioru kontrolnego jest zabronione' USING ERRCODE = 'KS002';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT frozen_at INTO v_frozen FROM control_set WHERE id = NEW.set_id;
        IF v_frozen IS NOT NULL THEN
            RAISE EXCEPTION 'Zbiór kontrolny % jest zamrożony - nie można dodać pozycji', NEW.set_id
                USING ERRCODE = 'KS009';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.set_id, NEW.company_id, NEW.subject_type, NEW.subject_id, NEW.order_no, NEW.is_sentinel,
        NEW.sentinel_expected)
       IS DISTINCT FROM
       (OLD.id, OLD.set_id, OLD.company_id, OLD.subject_type, OLD.subject_id, OLD.order_no, OLD.is_sentinel,
        OLD.sentinel_expected) THEN
        RAISE EXCEPTION 'Pozycja zbioru kontrolnego (id %) nie może zmienić podmiotu, kolejności ani wartownika', OLD.id
            USING ERRCODE = 'KS009';
    END IF;
    IF OLD.label IS NOT NULL AND NEW.label IS NULL THEN
        RAISE EXCEPTION 'Etykiety nie można usunąć (pozycja %)', OLD.id USING ERRCODE = 'KS009';
    END IF;
    IF OLD.label IS NOT NULL AND (NEW.label, NEW.labeled_by, NEW.labeled_at)
                                 IS DISTINCT FROM (OLD.label, OLD.labeled_by, OLD.labeled_at) THEN
        -- A label is written once; every change is kept in the history (by the database).
        INSERT INTO control_set_label_history (item_id, company_id, old_label, old_by, old_at, new_label, new_by)
        VALUES (OLD.id, OLD.company_id, OLD.label, OLD.labeled_by, OLD.labeled_at, NEW.label, NEW.labeled_by);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER control_set_item_guard
    BEFORE INSERT OR UPDATE OR DELETE ON control_set_item
    FOR EACH ROW EXECUTE FUNCTION kasa_control_set_item_guard();
CREATE TRIGGER control_set_item_subject_guard
    BEFORE INSERT OR UPDATE ON control_set_item
    FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard();
CREATE TRIGGER control_set_item_no_truncate
    BEFORE TRUNCATE ON control_set_item
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

CREATE FUNCTION kasa_control_set_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Usuwanie zbioru kontrolnego jest zabronione' USING ERRCODE = 'KS002';
    END IF;
    IF (NEW.id, NEW.company_id, NEW.kind, NEW.size_target, NEW.seed, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.company_id, OLD.kind, OLD.size_target, OLD.seed, OLD.created_at)
       OR (OLD.frozen_at IS NOT NULL AND NEW.frozen_at IS DISTINCT FROM OLD.frozen_at)
       OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at) THEN
        RAISE EXCEPTION 'Zbiór kontrolny % może zmienić tylko nazwę, zamrożenie i zakończenie (raz)', OLD.id
            USING ERRCODE = 'KS009';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER control_set_guard
    BEFORE UPDATE OR DELETE ON control_set
    FOR EACH ROW EXECUTE FUNCTION kasa_control_set_guard();
CREATE TRIGGER control_set_no_truncate
    BEFORE TRUNCATE ON control_set
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

CREATE TRIGGER control_set_label_history_append_only
    BEFORE UPDATE OR DELETE ON control_set_label_history
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER control_set_label_history_no_truncate
    BEFORE TRUNCATE ON control_set_label_history
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- 4. Sensitive words (stems), seeded by `kasa init-db` from the core and the extensions
-- ===========================================================================

CREATE TYPE sensitive_term_source AS ENUM ('core', 'extension', 'human');

CREATE TABLE sensitive_term (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid REFERENCES company (id),        -- NULL = every company
    term        text NOT NULL CHECK (btrim(term) <> ''),   -- as written ("zajęcie wynagrodzenia")
    stem        text NOT NULL CHECK (stem ~ '^[a-z0-9]+( [a-z0-9]+)*$'),   -- stems, folded ("zajec wynagrodzen")
    category    text NOT NULL CHECK (category ~ '^[a-z][a-z0-9_]*$'),    -- health, union, enforcement, ...
    source      sensitive_term_source NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL CHECK (btrim(created_by) <> ''),
    retired_at  timestamptz,
    CONSTRAINT sensitive_term_stem_key UNIQUE NULLS NOT DISTINCT (company_id, stem),
    CONSTRAINT sensitive_term_scope_check CHECK (source <> 'extension' OR company_id IS NOT NULL)
);

CREATE FUNCTION kasa_sensitive_term_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Słów wrażliwych nie kasujemy - wycofanie to retired_at' USING ERRCODE = 'KS002';
    END IF;
    IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL
       AND (NEW.id, NEW.company_id, NEW.term, NEW.stem, NEW.category, NEW.source, NEW.created_at, NEW.created_by)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.company_id, OLD.term, OLD.stem, OLD.category, OLD.source, OLD.created_at, OLD.created_by)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Słowo wrażliwe można tylko wycofać (retired_at)' USING ERRCODE = 'KS011';
END;
$$;

CREATE TRIGGER sensitive_term_guard
    BEFORE UPDATE OR DELETE ON sensitive_term
    FOR EACH ROW EXECUTE FUNCTION kasa_sensitive_term_guard();
CREATE TRIGGER sensitive_term_no_truncate
    BEFORE TRUNCATE ON sensitive_term
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- Review of phase 2
-- ===========================================================================

-- The regex reading of an unknown PDF (mock provider) is code, not a model.
ALTER TABLE l1_pdf_extraction DROP CONSTRAINT l1_pdf_extraction_method_check;
ALTER TABLE l1_pdf_extraction
    ADD CONSTRAINT l1_pdf_extraction_method_check CHECK (method IN ('model', 'code'));

-- Where our own NIP came from when it was set the first time (source, raw_id, time).
ALTER TABLE company ADD COLUMN nip_evidence jsonb
    CHECK (nip_evidence IS NULL OR jsonb_typeof(nip_evidence) = 'object');

-- Why a value is a person: registered (internal file, bank counterparty of kind
-- person), first_name, context (Pan/Pani, tel., PESEL, BLIK) or known_person.
-- Rows created before 0008 have NULL; load_context re-checks them for evidence.
ALTER TABLE pseudonym_key ADD COLUMN basis text
    CHECK (basis IS NULL OR basis IN ('registered', 'first_name', 'context', 'known_person', 'regex', 'checksum'));
