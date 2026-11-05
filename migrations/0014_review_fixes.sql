-- 0014_review_fixes: fixes of the review of phases 4C and 5 (docs/PRZEGLADY.md, "Fazy 4C i 5").
--
--   1. l2_suggestion: a row written by the agent's role (kasa_agent_suggest, or a login role that inherits it)
--      is ALWAYS a weak proposal of a model - source 'agent', method 'model', open, no rank, score and
--      confidence <= 0.9, evidence.hard = false, evidence.auto_eligible = false, no rule licence, badge low
--      (review point 7: the role could insert hard = true / score 1 and the automation would close it).
--   2. Anonymization of persons (review point 2): kasa_anonymize_persons() also covers every version of a
--      person counterparty (name, name_key, ibans, evidence), l1_document_observation.observed of persons'
--      documents, the texts derived from the records (queue_state.rank_reason, l2_suggestion proposal and
--      evidence, l3_decision_log payload / evidence / reason, l3_rule texts, evidence of L3 rows, notes of
--      control-set labels and their history), the model payloads of those records, and - with p_purge_raw
--      (CLI --shred) - the source files (L0) with persons whose records are all older than the date.
--      Derived texts are rewritten by a map built from the raw values BEFORE they are overwritten (full
--      names -> pseudonym, IBANs, phones, then every word of a person's name -> "[osoba]").
--      The guards that keep those tables append-only accept exactly this, inside the anonymization window
--      (kasa.anonymize = 'on' AND a row of anonymization_log in the same transaction), and only on the text
--      columns listed here; any other change still raises as before.
--   3. metrics.metric_definition.hash_scheme (review point 8): scheme 2 of code_sha256 also covers the whole
--      modules a definition declares (uses_modules: the company report, the allocation loaders). A row of
--      scheme 1 may be re-hashed ONCE to scheme 2 (sync_definitions does it only after proving the code is
--      the same under scheme 1); every other change of a definition is still KS013.
--   4. evidence.queue_state (review point 5): a waiting sentinel is not visible to the agent (its card,
--      its known answer) until it is answered.
--
-- No new error codes; no enum values.

-- ===========================================================================
-- 1. Suggestions of the agent are weak proposals, whatever the server sends
-- ===========================================================================

-- The session works as the agent's suggestion role: the role itself (SET ROLE) or a login role that
-- inherits its privileges (production). A superuser is never taken for the agent.
CREATE FUNCTION kasa_is_agent_suggest_role() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT current_user = 'kasa_agent_suggest'
        OR (NOT coalesce((SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = current_user), false)
            AND pg_has_role(current_user, 'kasa_agent_suggest', 'USAGE'))
$$;

CREATE FUNCTION kasa_agent_suggestion_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_evidence jsonb;
BEGIN
    IF NOT kasa_is_agent_suggest_role() THEN
        RETURN NEW;
    END IF;
    v_evidence := CASE WHEN jsonb_typeof(NEW.evidence) = 'object' THEN NEW.evidence
                       ELSE jsonb_build_object('items', NEW.evidence) END;
    NEW.source := 'agent';
    NEW.method := 'model';
    NEW.status := 'open';
    NEW.resolved_at := NULL;
    NEW.resolved_by_decision := NULL;
    NEW.rank := NULL;
    NEW.score := CASE WHEN NEW.score IS NULL THEN NULL ELSE least(NEW.score, 0.9) END;
    NEW.confidence := CASE WHEN NEW.confidence IS NULL THEN NULL ELSE least(NEW.confidence, 0.9) END;
    NEW.evidence := (v_evidence - 'rule' - 'auto_eligible' - 'hard' - 'tags')
                    || jsonb_build_object('hard', false, 'auto_eligible', false, 'agent', true, 'badge', 'low',
                                          'precedents', 0, 'tags', jsonb_build_array('agent'));
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION kasa_agent_suggestion_guard() IS
    'Sugestia z roli agenta (kasa_agent_suggest) to zawsze słaba propozycja modelu: source=agent, method=model, '
    'open, bez rangi, score/confidence <= 0,9, evidence.hard=false, auto_eligible=false, bez licencji reguły, '
    'odznaka niska (przegląd 4C/5, pkt 7). Automat i tak pomija source=agent.';

CREATE TRIGGER l2_suggestion_agent_guard
    BEFORE INSERT OR UPDATE ON l2_suggestion
    FOR EACH ROW EXECUTE FUNCTION kasa_agent_suggestion_guard();

-- ===========================================================================
-- 2a. The anonymization window and the text map
-- ===========================================================================

-- True only inside kasa_anonymize_persons() of this company (flag + log row of this transaction).
CREATE FUNCTION kasa_anonymization_open(p_company_id uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
BEGIN
    IF coalesce(current_setting('kasa.anonymize', true), '') <> 'on' THEN
        RETURN false;
    END IF;
    RETURN EXISTS (SELECT 1 FROM anonymization_log l
                    WHERE l.company_id = p_company_id AND l.xact_id = pg_current_xact_id());
END;
$$;

-- The letters of a word as a pattern: Polish letters and their plain forms alike ("Wiśniewski" also matches
-- "WISNIEWSKI"); used case-insensitively.
CREATE FUNCTION kasa_anon_letters(p_word text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT string_agg(
        CASE translate(s.ch, 'ąćęłńóśźż', 'acelnoszz')
            WHEN 'a' THEN '[aą]' WHEN 'c' THEN '[cć]' WHEN 'e' THEN '[eę]' WHEN 'l' THEN '[lł]'
            WHEN 'n' THEN '[nń]' WHEN 'o' THEN '[oó]' WHEN 's' THEN '[sś]' WHEN 'z' THEN '[zźż]'
            ELSE regexp_replace(s.ch, '([^[:alnum:]])', '\\\1', 'g')
        END, '' ORDER BY s.n)
      FROM regexp_split_to_table(lower(p_word), '') WITH ORDINALITY AS s (ch, n)
$$;

-- A word of a name as a pattern: the whole word - no letter right before or after it, so also inside a file
-- name like "rachunek_4_2026_wisniewski.pdf" or an e-mail address "jan.wisniewski@...".
CREATE FUNCTION kasa_anon_word_pattern(p_word text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '(?<![[:alpha:]])' || kasa_anon_letters(p_word) || '(?![[:alpha:]])'
$$;

-- A full name (several words) as one pattern: the words in this order, any spaces between them.
CREATE FUNCTION kasa_anon_name_pattern(p_name text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '(?<![[:alpha:]])' || string_agg(kasa_anon_letters(w.word), '\s+' ORDER BY w.n) || '(?![[:alpha:]])'
      FROM regexp_split_to_table(btrim(p_name), '\s+') WITH ORDINALITY AS w (word, n)
     WHERE w.word <> ''
$$;

-- Digits of a number (IBAN, phone) as a pattern that also matches the number written in groups; p_prefix
-- is an optional part before the digits (a country code), never preceded by another digit.
CREATE FUNCTION kasa_anon_digits_pattern(p_digits text, p_separators text DEFAULT '[\s-]?',
                                         p_prefix text DEFAULT '') RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '(?<![0-9])' || p_prefix || string_agg(s.d, p_separators ORDER BY s.n) || '(?![0-9])'
      FROM regexp_split_to_table(regexp_replace(p_digits, '\D', '', 'g'), '') WITH ORDINALITY AS s (d, n)
$$;

-- Rewrites a text with the map of the current anonymization (pg_temp._kasa_anon_map, built by
-- kasa_anonymize_persons from the raw values before they are overwritten). Without a map: unchanged.
CREATE FUNCTION kasa_anon_apply(p_text text) RETURNS text
LANGUAGE plpgsql SET search_path = public, pg_catalog, pg_temp AS $$
DECLARE
    v_out text := p_text;
    v_any text;
    r record;
BEGIN
    IF p_text IS NULL OR p_text = '' OR to_regclass('pg_temp._kasa_anon_any') IS NULL THEN
        RETURN p_text;
    END IF;
    SELECT a.pattern INTO v_any FROM pg_temp._kasa_anon_any a;
    IF v_any IS NULL OR p_text !~* v_any THEN
        RETURN p_text;
    END IF;
    FOR r IN SELECT m.pattern, m.replacement FROM pg_temp._kasa_anon_map m
             ORDER BY m.ord, m.len DESC, m.pattern LOOP
        v_out := regexp_replace(v_out, r.pattern, r.replacement, 'gi');
    END LOOP;
    RETURN v_out;
END;
$$;

-- The same for every string value of a jsonb (keys stay: they are names of fields, not data).
CREATE FUNCTION kasa_anon_jsonb(p jsonb) RETURNS jsonb
LANGUAGE plpgsql SET search_path = public, pg_catalog, pg_temp AS $$
DECLARE
    v_any text;
BEGIN
    IF p IS NULL OR to_regclass('pg_temp._kasa_anon_any') IS NULL THEN
        RETURN p;
    END IF;
    SELECT a.pattern INTO v_any FROM pg_temp._kasa_anon_any a;
    IF v_any IS NULL OR p::text !~* v_any THEN
        RETURN p;
    END IF;
    CASE jsonb_typeof(p)
        WHEN 'string' THEN
            RETURN to_jsonb(kasa_anon_apply(p #>> '{}'));
        WHEN 'object' THEN
            RETURN (SELECT coalesce(jsonb_object_agg(e.key, kasa_anon_jsonb(e.value)), '{}'::jsonb)
                      FROM jsonb_each(p) AS e);
        WHEN 'array' THEN
            RETURN (SELECT coalesce(jsonb_agg(kasa_anon_jsonb(a.value) ORDER BY a.ord), '[]'::jsonb)
                      FROM jsonb_array_elements(p) WITH ORDINALITY AS a (value, ord));
        ELSE
            RETURN p;
    END CASE;
END;
$$;

REVOKE EXECUTE ON FUNCTION kasa_anon_apply(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kasa_anon_jsonb(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kasa_anonymization_open(uuid) FROM PUBLIC;

-- ===========================================================================
-- 2b. Guards: the anonymization window may rewrite text columns, nothing else
-- ===========================================================================

-- L3: inside the window an UPDATE that changes only text columns passes (no decision is changed: the
-- same rows, statuses, amounts and links - only the names in their texts become pseudonyms).
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
    -- texts the anonymization of persons may rewrite (0014)
    v_anon_texts text[] := ARRAY['evidence', 'reason', 'payload', 'question', 'exceptions', 'post_mortem',
                                 'suspended_reason'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Usuwanie z % jest zabronione (id %)', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'KS002',
                  HINT = 'Cofnięcie decyzji to nowa decyzja typu revoke, nie kasowanie.';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF NEW.company_id IS NOT DISTINCT FROM OLD.company_id AND kasa_anonymization_open(OLD.company_id)
           AND (to_jsonb(NEW) - v_anon_texts) = (to_jsonb(OLD) - v_anon_texts) THEN
            RETURN NEW;
        END IF;
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

-- The decision log stays append-only; the anonymization window may rewrite its texts only.
CREATE FUNCTION kasa_decision_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.company_id IS NOT DISTINCT FROM OLD.company_id
       AND kasa_anonymization_open(OLD.company_id)
       AND (to_jsonb(NEW) - ARRAY['payload', 'evidence', 'reason'])
           = (to_jsonb(OLD) - ARRAY['payload', 'evidence', 'reason']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS001',
              HINT = 'Surowe dane i dziennik decyzji tylko dopisujemy. Korekta = nowy wiersz.';
END;
$$;

DROP TRIGGER l3_decision_log_append_only ON l3_decision_log;
CREATE TRIGGER l3_decision_log_append_only
    BEFORE UPDATE OR DELETE ON l3_decision_log
    FOR EACH ROW EXECUTE FUNCTION kasa_decision_log_append_only();

-- Control-set labels: the window may rewrite the texts of a label (a free note) without a history row.
CREATE OR REPLACE FUNCTION kasa_control_set_item_guard() RETURNS trigger
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
    IF kasa_anonymization_open(OLD.company_id)
       AND (to_jsonb(NEW) - ARRAY['label', 'sentinel_expected']) = (to_jsonb(OLD) - ARRAY['label', 'sentinel_expected'])
       AND (NEW.label IS NULL) = (OLD.label IS NULL)
       AND (NEW.sentinel_expected IS NULL) = (OLD.sentinel_expected IS NULL) THEN
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

CREATE FUNCTION kasa_label_history_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND kasa_anonymization_open(OLD.company_id)
       AND (to_jsonb(NEW) - ARRAY['old_label', 'new_label']) = (to_jsonb(OLD) - ARRAY['old_label', 'new_label']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS001',
              HINT = 'Historia etykiet tylko dopisujemy.';
END;
$$;

DROP TRIGGER control_set_label_history_append_only ON control_set_label_history;
CREATE TRIGGER control_set_label_history_append_only
    BEFORE UPDATE OR DELETE ON control_set_label_history
    FOR EACH ROW EXECUTE FUNCTION kasa_label_history_append_only();

-- L0 stays append-only with two exceptions: the purge (kasa_purge_raw, as in 0006) and - inside the
-- anonymization window - the metadata of a file (an e-mail's sender, a file name), never its payload.
CREATE OR REPLACE FUNCTION kasa_l0_raw_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND coalesce(current_setting('kasa.purge', true), '') = 'on'
       AND OLD.purged_at IS NULL
       AND NEW.purged_at IS NOT NULL
       AND NEW.payload = ''::bytea
       AND (NEW.id, NEW.company_id, NEW.channel, NEW.external_ref, NEW.sha256,
            NEW.content_type, NEW.fetched_at, NEW.meta)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.company_id, OLD.channel, OLD.external_ref, OLD.sha256,
            OLD.content_type, OLD.fetched_at, OLD.meta)
       AND EXISTS (SELECT 1 FROM l0_purge_log l
                    WHERE l.raw_id = OLD.id AND l.xact_id = pg_current_xact_id())
    THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.company_id IS NOT DISTINCT FROM OLD.company_id
       AND kasa_anonymization_open(OLD.company_id)
       AND (to_jsonb(NEW) - 'meta') = (to_jsonb(OLD) - 'meta') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS001',
              HINT = 'Surowe dane tylko dopisujemy. Jedyny wyjątek to retencja: SELECT kasa_purge_raw(id, powód).';
END;
$$;

-- What a channel saw of a document is a fact too: it changes only in the anonymization window.
CREATE TRIGGER l1_document_observation_raw_person_guard
    BEFORE UPDATE ON l1_document_observation
    FOR EACH ROW EXECUTE FUNCTION kasa_raw_person_guard('observed');

-- ===========================================================================
-- 2c. kasa_anonymize_persons: records, every derived text, model payloads, source files
-- ===========================================================================

DROP FUNCTION kasa_anonymize_persons(uuid, date, text, text, text[], text[]);

CREATE FUNCTION kasa_anonymize_persons(
    p_company_id uuid, p_before date, p_actor text DEFAULT NULL,
    p_reason text DEFAULT 'retencja: umowa + 1 rok (Krok 9)',
    p_person_keys text[] DEFAULT ARRAY['osoba', 'person', 'imię i nazwisko', 'imie i nazwisko', 'nazwisko',
                                       'pracownik', 'wykonawca'],
    p_drop_keys text[] DEFAULT ARRAY['uwagi', 'notes', 'telefon', 'tel', 'tel.', 'phone', 'iban', 'rachunek',
                                     'konto', 'nr konta', 'adres', 'address', 'pesel', 'email', 'e-mail'],
    p_purge_raw boolean DEFAULT false
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_catalog, pg_temp AS $$
DECLARE
    v_log uuid;
    v_counts jsonb;
    v_total integer;
    v_raw uuid;
    v_any text;
BEGIN
    IF p_before IS NULL THEN
        RAISE EXCEPTION 'Podaj datę: anonimizujemy rekordy sprzed niej' USING ERRCODE = '22023';
    END IF;

    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_tx (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_rec (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_doc (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_pdf (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_party (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_obs (id uuid PRIMARY KEY, v jsonb) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_subject (subject_type text, subject_id uuid,
                                                                PRIMARY KEY (subject_type, subject_id)) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_term (term text, replacement text, kind text) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_map (ord integer, len integer, pattern text,
                                                            replacement text) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_any (pattern text) ON COMMIT DROP;
    -- new values of derived texts (id -> value), computed before the log row (the counts go into the log)
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_queue (id uuid PRIMARY KEY, v text) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_sugg (id uuid PRIMARY KEY, proposal jsonb, evidence jsonb)
        ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_dec (id uuid PRIMARY KEY, payload jsonb, evidence jsonb,
                                                            reason text) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_rule (id uuid PRIMARY KEY, question text, exceptions jsonb,
                                                             evidence jsonb, post_mortem text, suspended_reason text)
        ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_l3 (tbl text, id uuid, evidence jsonb,
                                                           PRIMARY KEY (tbl, id)) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_l1 (tbl text, id uuid, evidence jsonb,
                                                           PRIMARY KEY (tbl, id)) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_label (id uuid PRIMARY KEY, label jsonb, expected jsonb)
        ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_history (id uuid PRIMARY KEY, old_label jsonb,
                                                                new_label jsonb) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_payload (call_id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_raw (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_meta (id uuid PRIMARY KEY, meta jsonb) ON COMMIT DROP;
    TRUNCATE pg_temp._kasa_anon_tx, pg_temp._kasa_anon_rec, pg_temp._kasa_anon_doc, pg_temp._kasa_anon_pdf,
             pg_temp._kasa_anon_party, pg_temp._kasa_anon_obs, pg_temp._kasa_anon_subject, pg_temp._kasa_anon_term,
             pg_temp._kasa_anon_map, pg_temp._kasa_anon_any, pg_temp._kasa_anon_queue, pg_temp._kasa_anon_sugg,
             pg_temp._kasa_anon_dec, pg_temp._kasa_anon_rule, pg_temp._kasa_anon_l3, pg_temp._kasa_anon_label,
             pg_temp._kasa_anon_l1,
             pg_temp._kasa_anon_history, pg_temp._kasa_anon_payload, pg_temp._kasa_anon_raw,
             pg_temp._kasa_anon_meta;

    -- ---------------------------------------------------------------- records (L1) as in 0012
    INSERT INTO pg_temp._kasa_anon_tx
    SELECT t.id FROM l1_bank_transaction t
     WHERE t.company_id = p_company_id
       AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) < p_before
       AND (t.title_raw IS DISTINCT FROM coalesce(t.title_pseudonymized, CASE WHEN t.title_raw IS NULL THEN NULL
                                                                           ELSE '[zanonimizowano]' END)
            OR t.counterparty_name_raw IS DISTINCT FROM CASE WHEN t.counterparty_name_raw IS NULL THEN NULL
                                                       ELSE coalesce(t.counterparty_pseudonym, '[zanonimizowano]') END
            OR (t.counterparty_iban_raw IS NOT NULL AND t.counterparty_iban_pseudonym IS NOT NULL
                AND t.counterparty_iban_raw <> t.counterparty_iban_pseudonym
                AND coalesce((SELECT c.kind::text FROM l1_counterparty c WHERE c.id = t.counterparty_id), 'person')
                    NOT IN ('company', 'institution', 'bank', 'own'))
            OR t.extracted IS DISTINCT FROM kasa_strip_phone_numbers(t.extracted));
    INSERT INTO pg_temp._kasa_anon_rec
    SELECT r.id FROM l1_internal_record r
     WHERE r.company_id = p_company_id AND coalesce(r.record_date, r.created_at::date) < p_before
       AND r.fields IS DISTINCT FROM kasa_mask_internal_fields(r.fields, r.person_pseudonym, p_person_keys, p_drop_keys);
    INSERT INTO pg_temp._kasa_anon_doc
    SELECT d.id FROM l1_document d
     WHERE d.company_id = p_company_id AND d.issue_date < p_before
       AND kasa_doc_is_person(d.kind::text, d.counterparty_id)
       AND ((d.direction = 'purchase' AND d.seller_name IS DISTINCT FROM
                 coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c WHERE c.id = d.counterparty_id), 'osoba'))
            OR (d.direction = 'sales' AND d.buyer_name IS DISTINCT FROM
                 coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c WHERE c.id = d.counterparty_id), 'osoba'))
            OR d.payment_iban IS NOT NULL OR d.extra #> '{payment,bank_account}' IS NOT NULL);
    INSERT INTO pg_temp._kasa_anon_pdf
    SELECT x.id FROM l1_pdf_extraction x
     WHERE x.company_id = p_company_id
       AND coalesce((SELECT d.issue_date FROM l1_document d WHERE d.id = x.document_id), x.created_at::date) < p_before
       AND NOT coalesce((x.extracted ->> 'anonymized')::boolean, false)
       AND (x.document_id IS NULL OR kasa_doc_is_person(x.document_id)
            OR x.extracted ->> 'doc_kind' = 'bill');
    -- every version of a person counterparty (versions chained by superseded_by), unless the person has
    -- records from p_before on (then the person is still in the data - nothing of hers is overwritten here)
    INSERT INTO pg_temp._kasa_anon_party
    WITH RECURSIVE chain (id, head) AS (
        SELECT c.id, c.id FROM l1_counterparty c WHERE c.company_id = p_company_id AND c.superseded_by IS NULL
        UNION ALL
        SELECT o.id, ch.head FROM l1_counterparty o JOIN chain ch ON o.superseded_by = ch.id
    ), recent AS (
        SELECT DISTINCT ch.head FROM chain ch
         WHERE EXISTS (SELECT 1 FROM l1_bank_transaction t WHERE t.counterparty_id = ch.id
                         AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) >= p_before)
            OR EXISTS (SELECT 1 FROM l1_document d WHERE d.counterparty_id = ch.id AND d.issue_date >= p_before)
    )
    SELECT c.id FROM l1_counterparty c JOIN chain ch ON ch.id = c.id
      JOIN l1_counterparty h ON h.id = ch.head
     WHERE (c.kind = 'person' OR h.kind = 'person')
       AND ch.head NOT IN (SELECT head FROM recent)
       AND (c.name IS DISTINCT FROM coalesce(c.name_pseudonymized, 'osoba') OR cardinality(c.ibans) > 0
            OR c.name_key IS NOT NULL OR c.evidence ?| ARRAY['bank_name', 'iban', 'name']);
    INSERT INTO pg_temp._kasa_anon_obs (id)
    SELECT o.id FROM l1_document_observation o JOIN l1_document d ON d.id = o.document_id
     WHERE o.company_id = p_company_id AND d.issue_date < p_before
       AND kasa_doc_is_person(d.kind::text, d.counterparty_id)
       AND NOT coalesce((o.observed ->> 'anonymized')::boolean, false);

    -- ---------------------------------------------------------------- the map (raw values -> pseudonyms)
    INSERT INTO pg_temp._kasa_anon_term
    SELECT t.counterparty_name_raw, coalesce(t.counterparty_pseudonym, '[zanonimizowano]'), 'name'
      FROM l1_bank_transaction t
     WHERE t.id IN (SELECT id FROM pg_temp._kasa_anon_tx) AND t.counterparty_name_raw IS NOT NULL
       AND t.counterparty_name_raw IS DISTINCT FROM t.counterparty_pseudonym
    UNION ALL
    SELECT t.counterparty_iban_raw, coalesce(t.counterparty_iban_pseudonym, '[IBAN]'), 'iban'
      FROM l1_bank_transaction t
     WHERE t.id IN (SELECT id FROM pg_temp._kasa_anon_tx) AND t.counterparty_iban_raw IS NOT NULL
       AND t.counterparty_iban_raw IS DISTINCT FROM t.counterparty_iban_pseudonym
       AND coalesce((SELECT c.kind::text FROM l1_counterparty c WHERE c.id = t.counterparty_id), 'person')
           NOT IN ('company', 'institution', 'bank', 'own')
    UNION ALL
    SELECT p ->> 'number', coalesce(p ->> 'pseudonym', '[telefon]'), 'phone'
      FROM l1_bank_transaction t, jsonb_array_elements(CASE WHEN jsonb_typeof(t.extracted -> 'phones') = 'array'
                                                            THEN t.extracted -> 'phones' ELSE '[]'::jsonb END) AS p
     WHERE t.id IN (SELECT id FROM pg_temp._kasa_anon_tx) AND jsonb_typeof(p) = 'object' AND p ? 'number'
    UNION ALL
    SELECT n.value, coalesce(c.name_pseudonymized, 'osoba'), 'name'
      FROM l1_counterparty c,
           LATERAL (VALUES (c.name), (c.evidence ->> 'bank_name'), (c.evidence ->> 'name')) AS n (value)
     WHERE c.id IN (SELECT id FROM pg_temp._kasa_anon_party) AND n.value IS NOT NULL
       AND n.value IS DISTINCT FROM c.name_pseudonymized
    UNION ALL
    SELECT i.value, '[IBAN]', 'iban'
      FROM l1_counterparty c,
           LATERAL (SELECT unnest(c.ibans) UNION ALL SELECT c.evidence ->> 'iban') AS i (value)
     WHERE c.id IN (SELECT id FROM pg_temp._kasa_anon_party) AND i.value IS NOT NULL
    UNION ALL
    SELECT e.value #>> '{}', coalesce(r.person_pseudonym, 'osoba'), 'name'
      FROM l1_internal_record r, jsonb_each(coalesce(r.fields, '{}'::jsonb)) AS e
     WHERE r.id IN (SELECT id FROM pg_temp._kasa_anon_rec) AND lower(e.key) = ANY (p_person_keys)
       AND jsonb_typeof(e.value) = 'string'
    UNION ALL
    SELECT CASE WHEN d.direction = 'purchase' THEN d.seller_name ELSE d.buyer_name END,
           coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c WHERE c.id = d.counterparty_id), 'osoba'),
           'name'
      FROM l1_document d WHERE d.id IN (SELECT id FROM pg_temp._kasa_anon_doc)
    UNION ALL
    SELECT b.value, '[IBAN]', 'iban'
      FROM l1_document d,
           LATERAL (VALUES (d.payment_iban), (d.extra #>> '{payment,bank_account}')) AS b (value)
     WHERE d.id IN (SELECT id FROM pg_temp._kasa_anon_doc) AND b.value IS NOT NULL;

    DELETE FROM pg_temp._kasa_anon_term
     WHERE term IS NULL OR length(btrim(term)) < 3 OR term = replacement
        OR (kind IN ('iban', 'phone') AND length(regexp_replace(term, '\D', '', 'g')) < 9);

    -- patterns, most specific first: full names, IBANs (also without "PL" and in groups), phones, then every
    -- word of a name of at least 3 letters that its pseudonym does not keep (any form left in a text: the
    -- surname alone, in capitals, without Polish letters, the words in another order; the words of a firm the
    -- pseudonym keeps stay - the firm is not a person)
    INSERT INTO pg_temp._kasa_anon_map (ord, len, pattern, replacement)
    SELECT DISTINCT ON (pattern) ord, len, pattern, replacement FROM (
        SELECT 1 AS ord, kasa_anon_name_pattern(term) AS pattern, replacement, length(term) AS len
          FROM pg_temp._kasa_anon_term WHERE kind = 'name' AND btrim(term) ~ '\s'
        UNION ALL
        SELECT 2, kasa_anon_digits_pattern(substring(regexp_replace(term, '\s', '', 'g') FROM 3), '\s?',
                                           '(?:[A-Za-z]{2}\s?)?'),
               replacement, length(term)
          FROM pg_temp._kasa_anon_term WHERE kind = 'iban' AND regexp_replace(term, '\s', '', 'g') ~ '^[A-Za-z]{2}\d'
        UNION ALL
        SELECT 2, kasa_anon_digits_pattern(term, '\s?'), replacement, length(term)
          FROM pg_temp._kasa_anon_term WHERE kind = 'iban' AND regexp_replace(term, '\s', '', 'g') ~ '^\d'
        UNION ALL
        SELECT 3, kasa_anon_digits_pattern(right(regexp_replace(term, '\D', '', 'g'), 9), '[\s-]?',
                                           '(?:\+?48[\s-]?)?'),
               replacement, length(term)
          FROM pg_temp._kasa_anon_term WHERE kind = 'phone'
        UNION ALL
        SELECT 4, kasa_anon_word_pattern(w.word), '[osoba]', length(w.word)
          FROM pg_temp._kasa_anon_term t, regexp_split_to_table(t.term, '[^[:alpha:]]+') AS w (word)
         WHERE t.kind = 'name' AND length(w.word) >= 3
           AND t.replacement !~* kasa_anon_word_pattern(w.word)     -- a company word the pseudonym keeps stays
    ) m
     WHERE pattern IS NOT NULL
     ORDER BY pattern, ord, (replacement LIKE '[%') , len DESC;

    -- one pattern of all of them finds the texts to rewrite; a very long map (hundreds of persons) would make it
    -- too complex for the regex engine - then every text is looked at (slower, the same result)
    INSERT INTO pg_temp._kasa_anon_any
    SELECT CASE WHEN count(*) <= 400 THEN string_agg('(?:' || pattern || ')', '|') ELSE '.' END
      FROM pg_temp._kasa_anon_map HAVING count(*) > 0;
    SELECT pattern INTO v_any FROM pg_temp._kasa_anon_any;

    -- ---------------------------------------------------------------- derived texts
    -- subjects dated before p_before (their texts are rewritten with the map of the persons above)
    INSERT INTO pg_temp._kasa_anon_subject
    SELECT 'transaction', t.id FROM l1_bank_transaction t
     WHERE t.company_id = p_company_id AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) < p_before
    UNION
    SELECT 'internal_record', r.id FROM l1_internal_record r
     WHERE r.company_id = p_company_id AND coalesce(r.record_date, r.created_at::date) < p_before
    UNION
    SELECT 'document', d.id FROM l1_document d WHERE d.company_id = p_company_id AND d.issue_date < p_before
    UNION
    SELECT 'line', l.id FROM l1_document_line l JOIN l1_document d ON d.id = l.document_id
     WHERE d.company_id = p_company_id AND d.issue_date < p_before;

    IF v_any IS NOT NULL THEN
        INSERT INTO pg_temp._kasa_anon_queue
        SELECT q.id, kasa_anon_apply(q.rank_reason) FROM queue_state q
         WHERE q.company_id = p_company_id AND q.rank_reason ~* v_any
           AND ((q.subject_type::text, q.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject)
                OR q.created_at < p_before);
        INSERT INTO pg_temp._kasa_anon_sugg
        SELECT s.id, kasa_anon_jsonb(s.proposal), kasa_anon_jsonb(s.evidence) FROM l2_suggestion s
         WHERE s.company_id = p_company_id AND (s.proposal::text ~* v_any OR s.evidence::text ~* v_any)
           AND ((s.subject_type::text, s.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject)
                OR s.created_at < p_before);
        INSERT INTO pg_temp._kasa_anon_dec
        SELECT d.id, kasa_anon_jsonb(d.payload), kasa_anon_jsonb(d.evidence), kasa_anon_apply(d.reason)
          FROM l3_decision_log d
         WHERE d.company_id = p_company_id
           AND (d.payload::text ~* v_any OR d.evidence::text ~* v_any OR coalesce(d.reason, '') ~* v_any)
           AND ((d.subject_type::text, d.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject)
                OR d.decided_at < p_before);
        INSERT INTO pg_temp._kasa_anon_rule
        SELECT r.id, kasa_anon_apply(r.question), kasa_anon_jsonb(r.exceptions), kasa_anon_jsonb(r.evidence),
               kasa_anon_apply(r.post_mortem), kasa_anon_apply(r.suspended_reason)
          FROM l3_rule r
         WHERE r.company_id = p_company_id AND r.created_at < p_before
           AND (coalesce(r.question, '') ~* v_any OR r.exceptions::text ~* v_any OR r.evidence::text ~* v_any
                OR coalesce(r.post_mortem, '') ~* v_any OR coalesce(r.suspended_reason, '') ~* v_any);
        INSERT INTO pg_temp._kasa_anon_l3
        SELECT 'l3_match', x.id, kasa_anon_jsonb(x.evidence) FROM l3_match x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.transaction_id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'transaction')
        UNION ALL
        SELECT 'l3_allocation', x.id, kasa_anon_jsonb(x.evidence) FROM l3_allocation x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND (x.subject_type::text, x.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject)
        UNION ALL
        SELECT 'l3_transaction_status', x.id, kasa_anon_jsonb(x.evidence) FROM l3_transaction_status x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.transaction_id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'transaction')
        UNION ALL
        SELECT 'l3_category_decision', x.id, kasa_anon_jsonb(x.evidence) FROM l3_category_decision x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND (x.subject_type::text, x.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject);
        INSERT INTO pg_temp._kasa_anon_label
        SELECT i.id, kasa_anon_jsonb(i.label), kasa_anon_jsonb(i.sentinel_expected) FROM control_set_item i
         WHERE i.company_id = p_company_id
           AND (coalesce(i.label::text, '') ~* v_any OR coalesce(i.sentinel_expected::text, '') ~* v_any)
           AND (i.subject_type::text, i.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject);
        INSERT INTO pg_temp._kasa_anon_history
        SELECT h.id, kasa_anon_jsonb(h.old_label), kasa_anon_jsonb(h.new_label) FROM control_set_label_history h
          JOIN control_set_item i ON i.id = h.item_id
         WHERE h.company_id = p_company_id AND (h.old_label::text ~* v_any OR h.new_label::text ~* v_any)
           AND (i.subject_type::text, i.subject_id) IN (SELECT subject_type, subject_id FROM pg_temp._kasa_anon_subject);
        -- evidence of the facts (a file name of the e-mail attachment, a name the parser kept)
        INSERT INTO pg_temp._kasa_anon_l1
        SELECT 'l1_bank_transaction', x.id, kasa_anon_jsonb(x.evidence) FROM l1_bank_transaction x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'transaction')
        UNION ALL
        SELECT 'l1_internal_record', x.id, kasa_anon_jsonb(x.evidence) FROM l1_internal_record x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'internal_record')
        UNION ALL
        SELECT 'l1_document', x.id, kasa_anon_jsonb(x.evidence) FROM l1_document x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'document')
        UNION ALL
        SELECT 'l1_document_observation', x.id, kasa_anon_jsonb(x.evidence) FROM l1_document_observation x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND x.document_id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'document')
        UNION ALL
        SELECT 'l1_pdf_extraction', x.id, kasa_anon_jsonb(x.evidence) FROM l1_pdf_extraction x
         WHERE x.company_id = p_company_id AND x.evidence::text ~* v_any
           AND (x.document_id IN (SELECT subject_id FROM pg_temp._kasa_anon_subject WHERE subject_type = 'document')
                OR (x.document_id IS NULL AND x.created_at < p_before));
        -- metadata of source files (an e-mail's sender and subject, the attachment's file name)
        INSERT INTO pg_temp._kasa_anon_meta
        SELECT r.id, kasa_anon_jsonb(r.meta) FROM l0_raw r
         WHERE r.company_id = p_company_id AND r.fetched_at < p_before AND r.meta::text ~* v_any;
    END IF;
    -- what a channel saw of a person's document: names and accounts out, the rest (amounts, lines) stays
    UPDATE pg_temp._kasa_anon_obs a
       SET v = kasa_anon_jsonb(o.observed #- '{seller,address}' #- '{buyer,address}' #- '{seller,iban}'
                               #- '{seller,bank_account}' #- '{seller,email}' #- '{seller,phone}'
                               #- '{buyer,email}' #- '{buyer,phone}' #- '{payment,bank_account}'
                               #- '{payment,iban}' #- '{payment_iban}' #- '{extra,payment,bank_account}')
               || jsonb_build_object('anonymized', true)
      FROM l1_document_observation o WHERE o.id = a.id;
    UPDATE pg_temp._kasa_anon_obs a
       SET v = jsonb_set(jsonb_set(a.v, '{seller,name}',
                                   to_jsonb(CASE WHEN d.direction = 'purchase'
                                                 THEN coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c
                                                                 WHERE c.id = d.counterparty_id), 'osoba')
                                                 ELSE coalesce(a.v #>> '{seller,name}', '') END), false),
                         '{buyer,name}',
                         to_jsonb(CASE WHEN d.direction = 'sales'
                                       THEN coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c
                                                       WHERE c.id = d.counterparty_id), 'osoba')
                                       ELSE coalesce(a.v #>> '{buyer,name}', '') END), false)
      FROM l1_document_observation o JOIN l1_document d ON d.id = o.document_id
     WHERE o.id = a.id;
    -- model prompts and answers of the records are past their use too (retention of 90 days anyway)
    INSERT INTO pg_temp._kasa_anon_payload
    SELECT p.call_id FROM llm_call_payload p
     WHERE p.company_id = p_company_id AND p.expired_at IS NULL AND p.created_at < p_before;
    -- source files with persons whose records are all older than p_before (only with p_purge_raw)
    IF p_purge_raw THEN
        INSERT INTO pg_temp._kasa_anon_raw
        SELECT r.id FROM l0_raw r
         WHERE r.company_id = p_company_id AND r.purged_at IS NULL AND r.fetched_at < p_before
           AND (r.id IN (SELECT t.raw_id FROM l1_bank_transaction t
                          WHERE t.company_id = p_company_id
                            AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) < p_before
                            AND (t.id IN (SELECT id FROM pg_temp._kasa_anon_tx) OR t.counterparty_id IS NULL
                                 OR t.counterparty_id IN (SELECT c.id FROM l1_counterparty c WHERE c.kind = 'person')))
                OR r.id IN (SELECT x.file_raw_id FROM l1_internal_record x
                             WHERE x.company_id = p_company_id AND coalesce(x.record_date, x.created_at::date) < p_before)
                OR r.id IN (SELECT o.raw_id FROM l1_document_observation o JOIN l1_document d ON d.id = o.document_id
                             WHERE o.company_id = p_company_id AND d.issue_date < p_before
                               AND kasa_doc_is_person(d.kind::text, d.counterparty_id))
                OR r.id IN (SELECT e.raw_id FROM l1_pdf_extraction e
                             WHERE e.id IN (SELECT id FROM pg_temp._kasa_anon_pdf)
                                OR (e.company_id = p_company_id AND coalesce((e.extracted ->> 'anonymized')::boolean,
                                                                             false))))
           AND NOT EXISTS (SELECT 1 FROM l1_bank_transaction t WHERE t.raw_id = r.id
                             AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) >= p_before)
           AND NOT EXISTS (SELECT 1 FROM l1_internal_record x WHERE x.file_raw_id = r.id
                             AND coalesce(x.record_date, x.created_at::date) >= p_before)
           AND NOT EXISTS (SELECT 1 FROM l1_document_observation o JOIN l1_document d ON d.id = o.document_id
                            WHERE o.raw_id = r.id AND d.issue_date >= p_before);
    END IF;

    -- ---------------------------------------------------------------- nothing changed: nothing to log
    DELETE FROM pg_temp._kasa_anon_queue a USING queue_state q WHERE q.id = a.id AND q.rank_reason = a.v;
    DELETE FROM pg_temp._kasa_anon_sugg a USING l2_suggestion s
     WHERE s.id = a.id AND s.proposal = a.proposal AND s.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_dec a USING l3_decision_log d
     WHERE d.id = a.id AND d.payload = a.payload AND d.evidence = a.evidence AND d.reason IS NOT DISTINCT FROM a.reason;
    DELETE FROM pg_temp._kasa_anon_rule a USING l3_rule r
     WHERE r.id = a.id AND (r.question, r.exceptions, r.evidence, r.post_mortem, r.suspended_reason)
                           IS NOT DISTINCT FROM (a.question, a.exceptions, a.evidence, a.post_mortem, a.suspended_reason);
    DELETE FROM pg_temp._kasa_anon_label a USING control_set_item i
     WHERE i.id = a.id AND (i.label, i.sentinel_expected) IS NOT DISTINCT FROM (a.label, a.expected);
    DELETE FROM pg_temp._kasa_anon_meta a USING l0_raw r WHERE r.id = a.id AND r.meta = a.meta;
    DELETE FROM pg_temp._kasa_anon_l3 a USING l3_match x
     WHERE a.tbl = 'l3_match' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l3 a USING l3_allocation x
     WHERE a.tbl = 'l3_allocation' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l3 a USING l3_transaction_status x
     WHERE a.tbl = 'l3_transaction_status' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l3 a USING l3_category_decision x
     WHERE a.tbl = 'l3_category_decision' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l1 a USING l1_bank_transaction x
     WHERE a.tbl = 'l1_bank_transaction' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l1 a USING l1_internal_record x
     WHERE a.tbl = 'l1_internal_record' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l1 a USING l1_document x
     WHERE a.tbl = 'l1_document' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l1 a USING l1_document_observation x
     WHERE a.tbl = 'l1_document_observation' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_l1 a USING l1_pdf_extraction x
     WHERE a.tbl = 'l1_pdf_extraction' AND x.id = a.id AND x.evidence = a.evidence;
    DELETE FROM pg_temp._kasa_anon_history a USING control_set_label_history h
     WHERE h.id = a.id AND (h.old_label, h.new_label) IS NOT DISTINCT FROM (a.old_label, a.new_label);

    v_counts := jsonb_build_object(
        'l1_bank_transaction', (SELECT count(*) FROM pg_temp._kasa_anon_tx),
        'l1_internal_record', (SELECT count(*) FROM pg_temp._kasa_anon_rec),
        'l1_document', (SELECT count(*) FROM pg_temp._kasa_anon_doc),
        'l1_pdf_extraction', (SELECT count(*) FROM pg_temp._kasa_anon_pdf),
        'l1_counterparty', (SELECT count(*) FROM pg_temp._kasa_anon_party),
        'l1_document_observation', (SELECT count(*) FROM pg_temp._kasa_anon_obs),
        'queue_state', (SELECT count(*) FROM pg_temp._kasa_anon_queue),
        'l2_suggestion', (SELECT count(*) FROM pg_temp._kasa_anon_sugg),
        'l3_decision_log', (SELECT count(*) FROM pg_temp._kasa_anon_dec),
        'l3_rule', (SELECT count(*) FROM pg_temp._kasa_anon_rule),
        'l3_evidence', (SELECT count(*) FROM pg_temp._kasa_anon_l3),
        'l1_evidence', (SELECT count(*) FROM pg_temp._kasa_anon_l1),
        'control_set_item', (SELECT count(*) FROM pg_temp._kasa_anon_label),
        'control_set_label_history', (SELECT count(*) FROM pg_temp._kasa_anon_history),
        'llm_call_payload', (SELECT count(*) FROM pg_temp._kasa_anon_payload),
        'l0_raw', (SELECT count(*) FROM pg_temp._kasa_anon_raw),
        'l0_raw_meta', (SELECT count(*) FROM pg_temp._kasa_anon_meta));
    SELECT sum(value::integer) INTO v_total FROM jsonb_each_text(v_counts);
    IF coalesce(v_total, 0) = 0 THEN
        RETURN NULL;
    END IF;

    INSERT INTO anonymization_log (company_id, before_date, reason, counts, anonymized_by)
    VALUES (p_company_id, p_before, btrim(p_reason), v_counts, coalesce(nullif(btrim(p_actor), ''), session_user))
    RETURNING id INTO v_log;

    PERFORM set_config('kasa.anonymize', 'on', true);
    -- derived texts first (their map needs nothing more from the records)
    UPDATE queue_state q SET rank_reason = a.v FROM pg_temp._kasa_anon_queue a WHERE q.id = a.id;
    UPDATE l2_suggestion s SET proposal = a.proposal, evidence = a.evidence
      FROM pg_temp._kasa_anon_sugg a WHERE s.id = a.id;
    UPDATE l3_decision_log d SET payload = a.payload, evidence = a.evidence, reason = a.reason
      FROM pg_temp._kasa_anon_dec a WHERE d.id = a.id;
    UPDATE l3_rule r SET question = a.question, exceptions = a.exceptions, evidence = a.evidence,
                         post_mortem = a.post_mortem, suspended_reason = a.suspended_reason
      FROM pg_temp._kasa_anon_rule a WHERE r.id = a.id;
    UPDATE l3_match x SET evidence = a.evidence FROM pg_temp._kasa_anon_l3 a WHERE a.tbl = 'l3_match' AND x.id = a.id;
    UPDATE l3_allocation x SET evidence = a.evidence FROM pg_temp._kasa_anon_l3 a
     WHERE a.tbl = 'l3_allocation' AND x.id = a.id;
    UPDATE l3_transaction_status x SET evidence = a.evidence FROM pg_temp._kasa_anon_l3 a
     WHERE a.tbl = 'l3_transaction_status' AND x.id = a.id;
    UPDATE l3_category_decision x SET evidence = a.evidence FROM pg_temp._kasa_anon_l3 a
     WHERE a.tbl = 'l3_category_decision' AND x.id = a.id;
    UPDATE control_set_item i SET label = a.label, sentinel_expected = a.expected
      FROM pg_temp._kasa_anon_label a WHERE i.id = a.id;
    UPDATE control_set_label_history h SET old_label = a.old_label, new_label = a.new_label
      FROM pg_temp._kasa_anon_history a WHERE h.id = a.id;
    UPDATE l1_document_observation o SET observed = a.v FROM pg_temp._kasa_anon_obs a WHERE o.id = a.id;
    UPDATE l0_raw r SET meta = a.meta FROM pg_temp._kasa_anon_meta a WHERE r.id = a.id;
    UPDATE l1_bank_transaction x SET evidence = a.evidence FROM pg_temp._kasa_anon_l1 a
     WHERE a.tbl = 'l1_bank_transaction' AND x.id = a.id;
    UPDATE l1_internal_record x SET evidence = a.evidence FROM pg_temp._kasa_anon_l1 a
     WHERE a.tbl = 'l1_internal_record' AND x.id = a.id;
    UPDATE l1_document x SET evidence = a.evidence FROM pg_temp._kasa_anon_l1 a WHERE a.tbl = 'l1_document' AND x.id = a.id;
    UPDATE l1_document_observation x SET evidence = a.evidence FROM pg_temp._kasa_anon_l1 a
     WHERE a.tbl = 'l1_document_observation' AND x.id = a.id;
    UPDATE l1_pdf_extraction x SET evidence = a.evidence FROM pg_temp._kasa_anon_l1 a
     WHERE a.tbl = 'l1_pdf_extraction' AND x.id = a.id;

    -- the records (as in 0012)
    UPDATE l1_bank_transaction t
       SET title_raw = coalesce(t.title_pseudonymized, CASE WHEN t.title_raw IS NULL THEN NULL ELSE '[zanonimizowano]' END),
           counterparty_name_raw = CASE WHEN t.counterparty_name_raw IS NULL THEN NULL
                                        ELSE coalesce(t.counterparty_pseudonym, '[zanonimizowano]') END,
           counterparty_iban_raw = CASE
               WHEN t.counterparty_iban_raw IS NULL THEN NULL
               WHEN coalesce((SELECT c.kind::text FROM l1_counterparty c WHERE c.id = t.counterparty_id), 'person')
                    IN ('company', 'institution', 'bank', 'own') THEN t.counterparty_iban_raw
               ELSE coalesce(t.counterparty_iban_pseudonym, '[zanonimizowano]') END,
           extracted = kasa_strip_phone_numbers(t.extracted)
     WHERE t.id IN (SELECT id FROM pg_temp._kasa_anon_tx);
    UPDATE l1_internal_record r
       SET fields = kasa_mask_internal_fields(r.fields, r.person_pseudonym, p_person_keys, p_drop_keys)
     WHERE r.id IN (SELECT id FROM pg_temp._kasa_anon_rec);
    UPDATE l1_document d
       SET seller_name = CASE WHEN d.direction = 'purchase'
                              THEN coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c
                                              WHERE c.id = d.counterparty_id), 'osoba')
                              ELSE d.seller_name END,
           buyer_name = CASE WHEN d.direction = 'sales'
                             THEN coalesce((SELECT c.name_pseudonymized FROM l1_counterparty c
                                             WHERE c.id = d.counterparty_id), 'osoba')
                             ELSE d.buyer_name END,
           payment_iban = NULL,
           extra = d.extra #- '{payment,bank_account}' #- '{seller,address}' #- '{buyer,address}'
     WHERE d.id IN (SELECT id FROM pg_temp._kasa_anon_doc);
    UPDATE l1_pdf_extraction x
       SET extracted = (x.extracted #- '{seller,name}' #- '{seller,address}' #- '{seller,iban}'
                        #- '{seller,bank_account}' #- '{seller,email}' #- '{seller,phone}' #- '{seller,tax_id}'
                        #- '{payment,bank_account}' #- '{payment,iban}') || '{"anonymized": true}'::jsonb
     WHERE x.id IN (SELECT id FROM pg_temp._kasa_anon_pdf);
    -- every version of the person: the pseudonym stays, the name, its key, the accounts and the bank's
    -- spelling of the name go
    UPDATE l1_counterparty c
       SET name = coalesce(c.name_pseudonymized, 'osoba'), name_key = NULL, ibans = '{}'::text[],
           evidence = CASE WHEN jsonb_typeof(c.evidence) = 'object'
                           THEN kasa_anon_jsonb(c.evidence - ARRAY['bank_name', 'iban', 'name'])
                                || jsonb_build_object('anonymized', true)
                           ELSE kasa_anon_jsonb(c.evidence) END
     WHERE c.id IN (SELECT id FROM pg_temp._kasa_anon_party);
    PERFORM set_config('kasa.anonymize', 'off', true);

    -- model payloads: emptied by the same rule as kasa_expire_llm_payloads (the digests stay)
    PERFORM set_config('kasa.expire_payload', 'on', true);
    UPDATE llm_call_payload p SET prompt_pseudonymized = NULL, response = NULL, expired_at = now()
     WHERE p.call_id IN (SELECT call_id FROM pg_temp._kasa_anon_payload);
    PERFORM set_config('kasa.expire_payload', 'off', true);

    -- source files: the only change of L0 the database allows (logged in l0_purge_log)
    FOR v_raw IN SELECT id FROM pg_temp._kasa_anon_raw ORDER BY id LOOP
        PERFORM kasa_purge_raw(v_raw, 'anonimizacja osób (rekordy sprzed ' || p_before::text || '): ' || btrim(p_reason),
                               coalesce(nullif(btrim(p_actor), ''), session_user));
    END LOOP;
    RETURN v_log;
END;
$$;

REVOKE EXECUTE ON FUNCTION kasa_anonymize_persons(uuid, date, text, text, text[], text[], boolean) FROM PUBLIC;

COMMENT ON FUNCTION kasa_anonymize_persons(uuid, date, text, text, text[], text[], boolean) IS
    'Jedyna ścieżka UPDATE nadpisująca surowe kolumny osób (tytuł, nadawca, IBAN osoby, telefony, pola arkusza, '
    'odczyt PDF i obserwacje dokumentu osoby, wszystkie wersje kontrahenta-osoby) formą pseudonimową, a teksty '
    'pochodne rekordów (powód rangi kolejki, fakty sugestii i decyzji, reguły, notatki etykiet) mapą z surowych '
    'wartości; prompty modelu tych rekordów wygasają, a z p_purge_raw (--shred) także pliki źródłowe z osobami '
    '(kasa_purge_raw); wpis w anonymization_log (przegląd 4C/5, pkt 2).';

-- ===========================================================================
-- 3. Metric definitions: the hashing scheme (scheme 2 covers the shared modules)
-- ===========================================================================

ALTER TABLE metrics.metric_definition
    ADD COLUMN hash_scheme smallint NOT NULL DEFAULT 1 CHECK (hash_scheme IN (1, 2));
ALTER TABLE metrics.metric_definition ALTER COLUMN hash_scheme SET DEFAULT 2;

COMMENT ON COLUMN metrics.metric_definition.hash_scheme IS
    'Schemat skrótu code_sha256: 1 = klasa definicji i jej funkcje pomocnicze (uses); 2 = także całe moduły '
    'wspólne (uses_modules: raport firmy, loadery alokacji). Wiersz schematu 1 przechodzi na 2 raz, przy '
    'synchronizacji rejestru, po sprawdzeniu, że kod definicji się nie zmienił.';

CREATE OR REPLACE FUNCTION kasa_metric_definition_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status = 'deprecated'
       AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
        RETURN NEW;
    END IF;
    -- one re-hash when the scheme grows (the registry proves first that the code is the same)
    IF TG_OP = 'UPDATE' AND NEW.hash_scheme > OLD.hash_scheme
       AND (to_jsonb(NEW) - ARRAY['code_sha256', 'hash_scheme']) = (to_jsonb(OLD) - ARRAY['code_sha256', 'hash_scheme'])
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Definicja metryki %@% jest niezmienna (zmiana = nowa wersja; dozwolone tylko active -> deprecated)',
            OLD.key, OLD.version
        USING ERRCODE = 'KS013';
END;
$$;

-- ===========================================================================
-- 4. evidence.queue_state without the waiting sentinels (rebuilt mirrors)
-- ===========================================================================

CREATE OR REPLACE FUNCTION kasa_refresh_agent_views() RETURNS integer
LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
DECLARE
    v_relations text[] := ARRAY[
        'company', 'l0_raw', 'l1_document', 'l1_document_line', 'l1_document_observation', 'l1_bank_account',
        'l1_bank_transaction', 'l1_bank_balance', 'l1_counterparty', 'l1_fx_rate', 'l1_internal_record',
        'l1_semantics', 'l1_title_semantics', 'l2_suggestion', 'l2_cost_object_member', 'l2_rule_observation',
        'l2_audit_sample', 'l3_rule', 'l3_decision_log', 'l3_cost_object', 'l3_match', 'l3_allocation',
        'l3_category_decision', 'l3_transaction_status', 'truth_check_result', 'queue_state', 'document_lineage',
        'title_semantics_current', 'rule_stage_effective'];
    -- only these columns (everything else of the table stays out)
    v_include jsonb := '{
        "company": ["id", "slug", "name", "extension_slug", "created_at", "nip"],
        "l0_raw": ["id", "company_id", "channel", "external_ref", "sha256", "content_type", "fetched_at", "purged_at"]
    }';
    v_exclude jsonb := '{"l1_semantics": ["input_pseudonymized"]}';
    v_masks jsonb := jsonb_build_object(
        'l1_bank_transaction', jsonb_build_object(
            'title_raw', 'CASE WHEN x.sensitive OR kasa_tx_is_payroll(x.id) THEN NULL ELSE x.title_pseudonymized END',
            'title_pseudonymized',
                'CASE WHEN x.sensitive OR kasa_tx_is_payroll(x.id) THEN NULL ELSE x.title_pseudonymized END',
            'counterparty_name_raw', 'CASE WHEN kasa_tx_is_payroll(x.id) THEN NULL ELSE x.counterparty_pseudonym END',
            'counterparty_pseudonym', 'CASE WHEN kasa_tx_is_payroll(x.id) THEN NULL ELSE x.counterparty_pseudonym END',
            'counterparty_iban_raw',
                'CASE WHEN kasa_tx_is_payroll(x.id) THEN NULL WHEN (SELECT c.kind FROM public.l1_counterparty c '
                'WHERE c.id = x.counterparty_id) IN (''company'', ''institution'', ''bank'', ''own'') '
                'THEN x.counterparty_iban_raw ELSE x.counterparty_iban_pseudonym END',
            'counterparty_iban_pseudonym',
                'CASE WHEN kasa_tx_is_payroll(x.id) THEN NULL ELSE x.counterparty_iban_pseudonym END',
            'counterparty_id', 'CASE WHEN kasa_tx_is_payroll(x.id) THEN NULL ELSE x.counterparty_id END',
            'extracted',
                'CASE WHEN kasa_tx_is_payroll(x.id) THEN ''{}''::jsonb ELSE kasa_strip_phone_numbers(x.extracted) END',
            'evidence', 'CASE WHEN kasa_tx_is_payroll(x.id) THEN ''{}''::jsonb ELSE x.evidence END'),
        'l1_counterparty', jsonb_build_object(
            'name', 'CASE WHEN x.kind = ''person'' THEN coalesce(x.name_pseudonymized, ''osoba'') '
                    'ELSE coalesce(x.name_pseudonymized, x.name) END',
            'name_key', 'NULL::text',
            'ibans', 'CASE WHEN x.kind = ''person'' THEN ''{}''::text[] ELSE x.ibans END',
            'evidence', '''{}''::jsonb'),
        'l1_document', jsonb_build_object(
            'seller_name', 'CASE WHEN x.direction = ''purchase'' THEN coalesce(kasa_party_display(x.counterparty_id), '
                           'CASE WHEN kasa_doc_is_person(x.kind::text, x.counterparty_id) THEN ''osoba'' '
                           'ELSE x.seller_name END) ELSE x.seller_name END',
            'buyer_name', 'CASE WHEN x.direction = ''sales'' THEN coalesce(kasa_party_display(x.counterparty_id), '
                          'CASE WHEN kasa_doc_is_person(x.kind::text, x.counterparty_id) THEN ''osoba'' '
                          'ELSE x.buyer_name END) ELSE x.buyer_name END',
            'payment_iban', 'CASE WHEN kasa_doc_is_person(x.kind::text, x.counterparty_id) THEN NULL '
                            'ELSE x.payment_iban END',
            'extra', 'CASE WHEN kasa_doc_is_person(x.kind::text, x.counterparty_id) THEN x.extra '
                     '#- ''{payment,bank_account}'' #- ''{seller,address}'' #- ''{buyer,address}'' ELSE x.extra END'),
        'l1_document_observation', jsonb_build_object('observed', '''{}''::jsonb'),
        'l1_internal_record', jsonb_build_object(
            'fields', 'kasa_mask_internal_fields(x.fields, x.person_pseudonym)'),
        'l2_suggestion', jsonb_build_object('evidence', 'x.evidence - ''facts'' - ''detail'''),
        'l3_decision_log', jsonb_build_object('reason', 'NULL::text', 'evidence', 'x.evidence - ''facts'' - ''detail'''),
        'queue_state', jsonb_build_object(
            'rank_reason', 'kasa_rank_reason_masked(x.rank_reason, x.subject_type, x.subject_id)',
            'sentinel_id', 'NULL::uuid'));
    v_filters jsonb := '{
        "company": "x.id = kasa_agent_company()",
        "l1_fx_rate": "(x.company_id IS NULL OR x.company_id = kasa_agent_company())",
        "queue_state": "x.company_id = kasa_agent_company() AND (x.card_kind <> ''sentinel'' OR x.closed_at IS NOT NULL)"
    }';
    v_rel text;
    v_col record;
    v_cols text;
    v_count integer := 0;
BEGIN
    FOREACH v_rel IN ARRAY v_relations LOOP
        v_cols := NULL;
        FOR v_col IN
            SELECT column_name::text AS name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = v_rel ORDER BY ordinal_position
        LOOP
            IF v_include ? v_rel AND NOT (v_include -> v_rel) ? v_col.name THEN
                CONTINUE;
            END IF;
            IF v_exclude ? v_rel AND (v_exclude -> v_rel) ? v_col.name THEN
                CONTINUE;
            END IF;
            v_cols := concat_ws(', ', v_cols,
                                coalesce(v_masks -> v_rel ->> v_col.name, format('x.%I', v_col.name))
                                || ' AS ' || quote_ident(v_col.name));
        END LOOP;
        IF v_cols IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('DROP VIEW IF EXISTS evidence.%I', v_rel);
        EXECUTE format('CREATE VIEW evidence.%I WITH (security_barrier) AS SELECT %s FROM public.%I x WHERE %s',
                       v_rel, v_cols, v_rel, coalesce(v_filters ->> v_rel, 'x.company_id = kasa_agent_company()'));
        EXECUTE format('GRANT SELECT ON evidence.%I TO kasa_agent_ro', v_rel);
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$$;

REVOKE EXECUTE ON FUNCTION kasa_refresh_agent_views() FROM PUBLIC;

SELECT kasa_refresh_agent_views();
