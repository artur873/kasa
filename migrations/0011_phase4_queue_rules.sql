-- 0011_phase4_queue_rules: phase 4 (part C) - the review queue and the rules learned from clicks.
--
--   1. l3_rule: business question, scope (known counterparties: "does not cover new ones"),
--      last hit, reason of a suspension and a post-mortem draft (briefing, step 6).
--   2. l2_rule_observation: "would the rule have done what you did?" - one row per rule and
--      human decision; the stages hint -> shadow -> ready -> dormant are DERIVED from these rows
--      (view rule_stage_effective; review of phase 1, point 6; decision K9).
--   3. l2_audit_sample: 2% of the automation's decisions a month (at least 20) back to a person
--      (review of phase 1, point 7).
--   4. queue_state / queue_sentinel: the state of the review queue (rank, reason, deferral,
--      first time shown - kept with COALESCE, review of phase 3, point 8 - decision time) and the
--      sentinels with a known answer (about every 50th card; the result is hidden).
--   5. l3_decision_log.undo_until (toast of 10 s) and batch_id (a homogeneous batch: every item
--      its own decision, one batch id).
--   6. sensitive_exception: "this is not sensitive" for a core/extension term is an exception for
--      one subject; a term added by a person is retired instead (review of phase 3, point 3).
--   7. KS003 sharpened (review of phase 1, point 13): an UPDATE written as 'human' on L3 must
--      carry a NEW decision (decision_id) or a revocation (revoked_by_decision); bookkeeping
--      columns (updated_at, updated_by, last_hit_at) may change alone.
--   8. decision_kind 'duplicate' (a person's verdict on a duplicate document suggestion).
--
-- Enum values added here are not used anywhere in this file (PostgreSQL allows adding them in
-- the runner's transaction only when nothing uses them before commit).

-- ===========================================================================
-- 1. Rules: question, scope, last hit, suspension, post-mortem
-- ===========================================================================

ALTER TABLE l3_rule
    ADD COLUMN question          text,
    ADD COLUMN scope             jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(scope) = 'object'),
    ADD COLUMN last_hit_at       timestamptz,
    ADD COLUMN suspended_reason  text,
    ADD COLUMN post_mortem       text;

COMMENT ON COLUMN l3_rule.question IS 'Pytanie biznesowe karty reguły, po polsku („Czy nadawca X to zawsze ZUS?”).';
COMMENT ON COLUMN l3_rule.scope IS
    'Zakres reguły: {"counterparties": [id...]} = tylko znani kontrahenci z dowodów (nowy kontrahent = brak '
    'trafienia, briefing: „nie obejmuje nowych kontrahentów”); {} = bez ograniczenia (reguły wbudowane).';
COMMENT ON COLUMN l3_rule.last_hit_at IS
    'Ostatnie trafienie (obserwacja albo decyzja automatu) - księgowość, nie decyzja; etap uśpienia liczy widok '
    'rule_stage_effective.';

CREATE UNIQUE INDEX l3_rule_signature_key ON l3_rule (company_id, (evidence ->> 'signature'), version)
    WHERE evidence ? 'signature';

-- ===========================================================================
-- 2. Observations of rules (derived stages)
-- ===========================================================================

CREATE TABLE l2_rule_observation (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id             uuid NOT NULL REFERENCES company (id),
    rule_id                uuid NOT NULL,
    rule_version           integer NOT NULL,
    stage                  rule_stage NOT NULL,      -- effective stage of the rule when observed
    subject_type           subject_type NOT NULL,
    subject_id             uuid NOT NULL,
    decision_id            uuid,                     -- the person's decision observed (NULL: simulation)
    predicted              jsonb NOT NULL CHECK (jsonb_typeof(predicted) = 'object'),
    actual                 jsonb NOT NULL CHECK (jsonb_typeof(actual) = 'object'),
    agreed                 boolean NOT NULL,
    observed_at            timestamptz NOT NULL DEFAULT now(),
    withdrawn_at           timestamptz,              -- the decision was revoked: the point is taken back
    withdrawn_by_decision  uuid,

    -- provenance
    source                 text NOT NULL CHECK (btrim(source) <> ''),
    method                 provenance_method NOT NULL CHECK (method = 'code'),
    confidence             numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence               jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version         integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version         text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l2_rule_observation_rule_fk FOREIGN KEY (rule_id, rule_version) REFERENCES l3_rule (id, version),
    CONSTRAINT l2_rule_observation_rule_company_fk FOREIGN KEY (rule_id, company_id) REFERENCES l3_rule (id, company_id),
    CONSTRAINT l2_rule_observation_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l2_rule_observation_withdrawn_fk FOREIGN KEY (withdrawn_by_decision, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l2_rule_observation_withdrawn_check CHECK ((withdrawn_at IS NULL) = (withdrawn_by_decision IS NULL))
);

CREATE UNIQUE INDEX l2_rule_observation_decision_key ON l2_rule_observation (rule_id, decision_id)
    WHERE decision_id IS NOT NULL;
CREATE INDEX l2_rule_observation_rule_idx ON l2_rule_observation (rule_id, observed_at) WHERE withdrawn_at IS NULL;
CREATE INDEX l2_rule_observation_decision_idx ON l2_rule_observation (decision_id) WHERE decision_id IS NOT NULL;

-- An observation is written once; the only change is taking the point back (once).
CREATE FUNCTION kasa_rule_observation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.withdrawn_at IS NULL AND NEW.withdrawn_at IS NOT NULL
       AND (to_jsonb(NEW) - ARRAY['withdrawn_at', 'withdrawn_by_decision'])
           = (to_jsonb(OLD) - ARRAY['withdrawn_at', 'withdrawn_by_decision']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Obserwację reguły (id %) można tylko raz wycofać (withdrawn_at), nie zmienić', OLD.id
        USING ERRCODE = 'KS012';
END;
$$;

CREATE TRIGGER l2_rule_observation_guard
    BEFORE UPDATE ON l2_rule_observation
    FOR EACH ROW EXECUTE FUNCTION kasa_rule_observation_guard();
CREATE TRIGGER l2_rule_observation_no_delete
    BEFORE DELETE ON l2_rule_observation
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l2_rule_observation_no_truncate
    BEFORE TRUNCATE ON l2_rule_observation
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
CREATE TRIGGER l2_rule_observation_subject_guard
    BEFORE INSERT OR UPDATE ON l2_rule_observation
    FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard();

-- Effective stage of the CURRENT version of every rule (stage stored by a person + what the
-- observations say). Thresholds of the briefing, step 6:
--   hint -> shadow: >= 30 observations of this version;
--   shadow -> ready for auto: lower bound of the 95% Wilson interval of agreement >= 0.95
--             (n agreed of n: n / (n + 1.96^2) >= 0.95 -> n >= 73 with 0 errors);
--   dormant: 12 months without a hit (observation or automatic decision); after a sleep the
--            first hit brings the rule back as a hint (not auto) until a person says otherwise.
CREATE VIEW rule_stage_effective AS
WITH current_rule AS (
    SELECT DISTINCT ON (company_id, rule_key) r.*
      FROM l3_rule r
     ORDER BY company_id, rule_key, version DESC
), since AS (
    SELECT r.id, coalesce(d.decided_at, r.created_at) AS stage_since
      FROM current_rule r LEFT JOIN l3_decision_log d ON d.id = r.decision_id
), obs AS (
    SELECT rule_id, count(*) AS observations, count(*) FILTER (WHERE agreed) AS agreed,
           count(*) FILTER (WHERE NOT agreed) AS disagreed, max(observed_at) AS last_observed_at
      FROM l2_rule_observation WHERE withdrawn_at IS NULL
     GROUP BY rule_id
), auto_decisions AS (
    SELECT d.rule_id, count(*) AS automatic, max(d.decided_at) AS last_auto_at
      FROM l3_decision_log d
     WHERE d.method = 'rule' AND d.rule_id IS NOT NULL AND d.kind <> 'revoke'
       AND NOT EXISTS (SELECT 1 FROM l3_decision_log v WHERE v.reverts_decision_id = d.id)
     GROUP BY d.rule_id
), hits AS (
    SELECT o.rule_id, o.observed_at AS at FROM l2_rule_observation o WHERE o.withdrawn_at IS NULL
    UNION ALL
    SELECT d.rule_id, d.decided_at FROM l3_decision_log d
     WHERE d.method = 'rule' AND d.rule_id IS NOT NULL AND d.kind <> 'revoke'
), ordered_hits AS (
    SELECT h.rule_id, h.at, s.stage_since, lag(h.at) OVER (PARTITION BY h.rule_id ORDER BY h.at) AS previous_at
      FROM hits h JOIN since s ON s.id = h.rule_id
     WHERE h.at >= s.stage_since
), slept AS (
    SELECT rule_id, bool_or(at - coalesce(previous_at, stage_since) > interval '12 months') AS slept
      FROM ordered_hits GROUP BY rule_id
), base AS (
    SELECT r.id AS rule_id, r.company_id, r.rule_key, r.version, r.stage AS stored_stage, r.method,
           r.pattern, r.action, r.scope, r.question, r.evidence_count, r.exceptions, r.suspended_reason,
           r.post_mortem, r.created_at, s.stage_since,
           coalesce(o.observations, 0) AS observations, coalesce(o.agreed, 0) AS agreed,
           coalesce(o.disagreed, 0) AS disagreed, coalesce(a.automatic, 0) AS automatic_decisions,
           greatest(r.last_hit_at, o.last_observed_at, a.last_auto_at) AS last_hit_at,
           coalesce(sl.slept, false) AS slept
      FROM current_rule r
      JOIN since s ON s.id = r.id
      LEFT JOIN obs o ON o.rule_id = r.id
      LEFT JOIN auto_decisions a ON a.rule_id = r.id
      LEFT JOIN slept sl ON sl.rule_id = r.id
), scored AS (
    SELECT b.*,
           CASE WHEN b.observations = 0 THEN 0::numeric ELSE round((
               (b.agreed::numeric / b.observations + 1.9208 / b.observations
                - 1.96 * sqrt((b.agreed::numeric / b.observations * (1 - b.agreed::numeric / b.observations)
                               + 0.9604 / b.observations) / b.observations))
               / (1 + 3.8416 / b.observations))::numeric, 4) END AS wilson_lower,
           CASE
               WHEN b.stored_stage IN ('candidate', 'suspended', 'dormant') THEN b.stored_stage
               WHEN coalesce(b.last_hit_at, b.stage_since) < now() - interval '12 months' THEN 'dormant'::rule_stage
               WHEN b.slept THEN 'hint'::rule_stage
               WHEN b.stored_stage = 'auto' THEN 'auto'::rule_stage
               WHEN b.stored_stage = 'shadow' OR b.observations >= 30 THEN 'shadow'::rule_stage
               ELSE 'hint'::rule_stage
           END AS effective_stage
      FROM base b
)
SELECT s.*, (s.effective_stage = 'shadow' AND s.wilson_lower >= 0.95) AS ready_for_auto
  FROM scored s;

COMMENT ON VIEW rule_stage_effective IS
    'Etap bieżącej wersji reguły liczony jako stan pochodny z l2_rule_observation (przegląd fazy 1, pkt 6; K9): '
    'podpowiedź → cień po 30 obserwacjach, gotowa do auto przy dolnej granicy Wilsona ≥ 0,95, uśpiona po 12 mies. '
    'bez trafienia (pierwsze trafienie po uśpieniu = znów podpowiedź).';

-- ===========================================================================
-- 3. Audit sample of the automation
-- ===========================================================================

CREATE TABLE l2_audit_sample (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          uuid NOT NULL REFERENCES company (id),
    decision_id         uuid NOT NULL,             -- the automatic decision under audit
    month               text NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
    sampled_at          timestamptz NOT NULL DEFAULT now(),
    rule_id             uuid,
    rule_version        integer,
    subject_type        subject_type NOT NULL,
    subject_id          uuid NOT NULL,
    amount_minor        bigint,                    -- |amount| of the subject (errors weighted by amount)
    population          integer NOT NULL CHECK (population > 0),
    sample_size         integer NOT NULL CHECK (sample_size > 0),
    seed                bigint,
    verdict             text CHECK (verdict IN ('confirmed', 'error')),
    reviewed_by         text,
    reviewed_at         timestamptz,
    review_decision_id  uuid,                      -- 'error': the revocation of the automatic decision
    note                text,

    -- provenance
    source              text NOT NULL CHECK (btrim(source) <> ''),
    method              provenance_method NOT NULL CHECK (method = 'code'),
    confidence          numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version      integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL CHECK (btrim(created_by) <> ''),

    CONSTRAINT l2_audit_sample_decision_key UNIQUE (decision_id),
    CONSTRAINT l2_audit_sample_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l2_audit_sample_review_fk FOREIGN KEY (review_decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id),
    CONSTRAINT l2_audit_sample_review_check CHECK ((verdict IS NULL) = (reviewed_at IS NULL)
                                                   AND (verdict IS NULL) = (reviewed_by IS NULL)),
    CONSTRAINT l2_audit_sample_confirmed_check CHECK (verdict IS DISTINCT FROM 'confirmed' OR review_decision_id IS NULL)
);

CREATE INDEX l2_audit_sample_month_idx ON l2_audit_sample (company_id, month);
CREATE INDEX l2_audit_sample_open_idx ON l2_audit_sample (company_id) WHERE verdict IS NULL;

-- The sample is drawn once; the verdict is given once.
CREATE FUNCTION kasa_audit_sample_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.verdict IS NULL AND NEW.verdict IS NOT NULL
       AND (to_jsonb(NEW) - ARRAY['verdict', 'reviewed_by', 'reviewed_at', 'review_decision_id', 'note'])
           = (to_jsonb(OLD) - ARRAY['verdict', 'reviewed_by', 'reviewed_at', 'review_decision_id', 'note']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Próbka audytu (id %) przyjmuje werdykt tylko raz; losowanie jest niezmienne', OLD.id
        USING ERRCODE = 'KS012';
END;
$$;

CREATE TRIGGER l2_audit_sample_guard
    BEFORE UPDATE ON l2_audit_sample
    FOR EACH ROW EXECUTE FUNCTION kasa_audit_sample_guard();
CREATE TRIGGER l2_audit_sample_no_delete
    BEFORE DELETE ON l2_audit_sample
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l2_audit_sample_no_truncate
    BEFORE TRUNCATE ON l2_audit_sample
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
CREATE TRIGGER l2_audit_sample_subject_guard
    BEFORE INSERT OR UPDATE ON l2_audit_sample
    FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard();

-- ===========================================================================
-- 4. The review queue: state of every card, sentinels
-- ===========================================================================

-- A card in the queue (one decision). Built from open suggestions, candidates, rules ready for
-- auto and audit samples by kasa.core.queue.build; a card leaves the queue with closed_at (never
-- deleted). shown_at is the FIRST time the card was sent (COALESCE - a reload never resets the
-- clock), duration_ms = decision - shown_at.
CREATE TABLE queue_state (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid NOT NULL REFERENCES company (id),
    card_kind        text NOT NULL CHECK (card_kind IN (
                         'match', 'allocation', 'category', 'sensitive', 'duplicate', 'cost_object_candidate',
                         'rule_candidate', 'rule_ready', 'audit', 'sentinel')),
    subject_type     subject_type NOT NULL,
    subject_id       uuid NOT NULL,
    suggestion_ids   uuid[] NOT NULL DEFAULT '{}',   -- the proposals of the card, by rank
    audit_sample_id  uuid REFERENCES l2_audit_sample (id),
    sentinel_id      uuid,
    rank_reason      text NOT NULL CHECK (btrim(rank_reason) <> ''),
    priority         numeric NOT NULL,
    position         integer,
    badge            text CHECK (badge IN ('high', 'medium', 'low')),
    amount_minor     bigint,
    batch_key        text,                            -- homogeneous batch (same rule/tag, counterparty, high)
    deferred_until   timestamptz,
    shown_at         timestamptz,
    skipped_at       timestamptz,
    decided_at       timestamptz,
    decided_by       text,
    decision_id      uuid,
    outcome          text CHECK (outcome IN ('accepted', 'alternative', 'other', 'rejected', 'answered', 'batch',
                                             'confirmed', 'error', 'not_sensitive', 'resolved_elsewhere')),
    duration_ms      integer CHECK (duration_ms >= 0),
    fast_alarm       boolean NOT NULL DEFAULT false,  -- decided in < 0.8 s on a "low" card (briefing, step 7)
    built_at         timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT queue_state_decision_fk FOREIGN KEY (decision_id, company_id)
        REFERENCES l3_decision_log (id, company_id)
);

CREATE UNIQUE INDEX queue_state_open_key ON queue_state (company_id, card_kind, subject_type, subject_id)
    WHERE closed_at IS NULL;
CREATE INDEX queue_state_order_idx ON queue_state (company_id, position) WHERE closed_at IS NULL;
CREATE INDEX queue_state_decision_idx ON queue_state (decision_id) WHERE decision_id IS NOT NULL;

CREATE TRIGGER queue_state_no_delete
    BEFORE DELETE ON queue_state
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER queue_state_no_truncate
    BEFORE TRUNCATE ON queue_state
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- A sentinel: a card with a known answer (an earlier decision of a person or a label of a
-- control set), shown like any other card about every 50 cards. The answer is compared and
-- stored; it never becomes a decision, and the result is not shown on the card.
CREATE TABLE queue_sentinel (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid NOT NULL REFERENCES company (id),
    subject_type     subject_type NOT NULL,
    subject_id       uuid NOT NULL,
    suggestion_id    uuid,                   -- the proposal shown first (any status)
    suggestion_ids   uuid[] NOT NULL DEFAULT '{}',
    expected         jsonb NOT NULL CHECK (jsonb_typeof(expected) = 'object'),
    expected_source  text NOT NULL CHECK (expected_source IN ('decision', 'control_set')),
    source_ref       uuid,                   -- decision id or control-set item id
    answer           jsonb CHECK (answer IS NULL OR jsonb_typeof(answer) = 'object'),
    shown_at         timestamptz,
    answered_at      timestamptz,
    answered_by      text,
    duration_ms      integer CHECK (duration_ms >= 0),
    passed           boolean,
    created_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT queue_sentinel_answered_check CHECK ((answer IS NULL) = (answered_at IS NULL)
                                                  AND (answer IS NULL) = (passed IS NULL))
);

CREATE INDEX queue_sentinel_company_idx ON queue_sentinel (company_id, answered_at);

CREATE FUNCTION kasa_queue_sentinel_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Wartowników kolejki nie kasujemy' USING ERRCODE = 'KS002';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['shown_at', 'answer', 'answered_at', 'answered_by', 'duration_ms', 'passed'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['shown_at', 'answer', 'answered_at', 'answered_by', 'duration_ms', 'passed'])
       OR (OLD.shown_at IS NOT NULL AND NEW.shown_at IS DISTINCT FROM OLD.shown_at)
       OR (OLD.answered_at IS NOT NULL AND (NEW.answer, NEW.answered_at, NEW.passed)
                                           IS DISTINCT FROM (OLD.answer, OLD.answered_at, OLD.passed)) THEN
        RAISE EXCEPTION 'Wartownik (id %): znana odpowiedź jest niezmienna, odpowiedź zapisuje się raz', OLD.id
            USING ERRCODE = 'KS012';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER queue_sentinel_guard
    BEFORE UPDATE OR DELETE ON queue_sentinel
    FOR EACH ROW EXECUTE FUNCTION kasa_queue_sentinel_guard();
CREATE TRIGGER queue_sentinel_no_truncate
    BEFORE TRUNCATE ON queue_sentinel
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

ALTER TABLE queue_state
    ADD CONSTRAINT queue_state_sentinel_fk FOREIGN KEY (sentinel_id) REFERENCES queue_sentinel (id);

-- ===========================================================================
-- 5. Decision log: undo window and batches
-- ===========================================================================

ALTER TABLE l3_decision_log
    ADD COLUMN undo_until timestamptz,
    ADD COLUMN batch_id   uuid;

COMMENT ON COLUMN l3_decision_log.undo_until IS
    'Do kiedy karta pokazuje toast „Cofnij” (decyzja człowieka + 10 s); później cofnięcie z ekranu Historia.';
COMMENT ON COLUMN l3_decision_log.batch_id IS
    'Paczka zbiorcza: każda pozycja to osobna decyzja, wspólny batch_id; cofnięcie paczki cofa dokładnie te decyzje.';

CREATE INDEX l3_decision_log_batch_idx ON l3_decision_log (batch_id) WHERE batch_id IS NOT NULL;

CREATE FUNCTION kasa_decision_undo_window() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.undo_until IS NULL AND NEW.method = 'human' AND NEW.kind <> 'revoke' THEN
        NEW.undo_until := NEW.decided_at + interval '10 seconds';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER l3_decision_log_undo_window
    BEFORE INSERT ON l3_decision_log
    FOR EACH ROW EXECUTE FUNCTION kasa_decision_undo_window();

-- ===========================================================================
-- 6. "This is not sensitive" for one subject
-- ===========================================================================

CREATE TABLE sensitive_exception (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    uuid NOT NULL REFERENCES company (id),
    subject_type  subject_type NOT NULL,
    subject_id    uuid NOT NULL,
    terms         text[] NOT NULL DEFAULT '{}',     -- the core/extension terms the person waived here
    reason        text,
    decided_by    text NOT NULL CHECK (btrim(decided_by) <> ''),
    decided_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sensitive_exception_subject_idx ON sensitive_exception (company_id, subject_type, subject_id);

CREATE TRIGGER sensitive_exception_append_only
    BEFORE UPDATE OR DELETE ON sensitive_exception
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER sensitive_exception_no_truncate
    BEFORE TRUNCATE ON sensitive_exception
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
CREATE TRIGGER sensitive_exception_subject_guard
    BEFORE INSERT ON sensitive_exception
    FOR EACH ROW EXECUTE FUNCTION kasa_subject_guard();

-- ===========================================================================
-- 7. KS003 sharpened: a human UPDATE carries a new decision (review of phase 1, point 13)
-- ===========================================================================

CREATE OR REPLACE FUNCTION kasa_l3_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_row       jsonb;
    v_old       jsonb;
    v_rule_id   uuid;
    v_rule_ver  integer;
    v_rule      record;
    -- columns that are bookkeeping, not a decision: they may change without a new decision
    v_bookkeeping text[] := ARRAY['updated_at', 'updated_by', 'last_hit_at'];
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
        -- A write that says "human" must point at a new decision of a person (or a revocation):
        -- "human" is not a password that lets code change a decision silently.
        IF NEW.method = 'human' THEN
            v_row := to_jsonb(NEW);
            v_old := to_jsonb(OLD);
            IF (v_row -> 'decision_id') IS NOT DISTINCT FROM (v_old -> 'decision_id')
               AND (v_row -> 'revoked_by_decision') IS NOT DISTINCT FROM (v_old -> 'revoked_by_decision')
               AND (v_row - v_bookkeeping) IS DISTINCT FROM (v_old - v_bookkeeping) THEN
                RAISE EXCEPTION 'Zmiana wiersza % (id %) zapisana jako decyzja człowieka bez nowej decyzji '
                                '(decision_id albo revoked_by_decision)', TG_TABLE_NAME, OLD.id
                    USING ERRCODE = 'KS003',
                          HINT = 'Każda zmiana decyzji to nowy wpis w l3_decision_log wskazany w wierszu.';
            END IF;
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

-- ===========================================================================
-- 8. A person's verdict on a duplicate document
-- ===========================================================================

ALTER TYPE decision_kind ADD VALUE IF NOT EXISTS 'duplicate';
