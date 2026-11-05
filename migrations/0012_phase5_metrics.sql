-- 0012_phase5_metrics: phase 5 - the metric layer, the agent (MCP), the dashboard, the export for the
-- accounting office and the retention items of the reviews (briefing, steps 8 and 9).
--
--   1. Schemas of the agent: metrics (definitions), views (question views), evidence (read-only,
--      per-company mirrors of the L1-L3 facts and truth_check_result without raw personal data).
--   2. metrics.metric_definition: versioned definitions (key, version, traps, SHA-256 of the code);
--      a change of a definition = a new version (KS013 guards the row).
--   3. agent_call_log (every MCP call, arguments after pseudonymization, append-only),
--      metric_proposal (the agent proposes a metric instead of inventing a number),
--      export_log (every hand-over of data to the accounting office, append-only).
--   4. Roles (briefing, step 8: "zabezpieczenia są w bazie, nie w prompcie"):
--      kasa_agent_ro - SELECT only on metrics/views/evidence, statement_timeout 15 s, company filter
--      by current_setting('kasa.company_id'); kasa_agent_suggest - INSERT only on l2_suggestion,
--      metric_proposal, agent_call_log.
--   5. Retention (reviews: phase 1 point 3, phase 3 points 4 and 10):
--      l1_semantics keeps the SHA-256 and the length of the model input; the text itself is emptied
--      after 90 days only by kasa_expire_semantics_inputs(); kasa_anonymize_persons() is the ONLY
--      UPDATE path that overwrites raw personal columns with their pseudonymous form (logged,
--      KS014 guards the columns).
--   6. suggestion_kind 'payment_reminder' (draft of a payment reminder - a person sends it).
--
-- New error codes:
--   KS013  metric definition: (key, version) is immutable, only active -> deprecated
--   KS014  raw personal column of L1 changed outside kasa_anonymize_persons() / semantics input
--          changed outside kasa_expire_semantics_inputs()
--
-- Enum values added here are not used anywhere in this file.

-- ===========================================================================
-- 1. Schemas
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS metrics;
CREATE SCHEMA IF NOT EXISTS views;
CREATE SCHEMA IF NOT EXISTS evidence;

COMMENT ON SCHEMA metrics IS 'Warstwa metryk: wersjonowane definicje (agent i dashboard czytają tę samą).';
COMMENT ON SCHEMA views IS 'Widoki pytań agenta (tylko odczyt, filtr firmy z kasa.company_id, teksty jako untrusted_text).';
COMMENT ON SCHEMA evidence IS
    'Dowody: lustra tabel L1-L3 i truth_check_result jednej firmy (kasa.company_id), bez surowych danych osób: '
    'kolumny *_raw z danymi osób niosą formę pseudonimową, wynagrodzenia osób bez powiązania z osobą.';

ALTER TYPE suggestion_kind ADD VALUE IF NOT EXISTS 'payment_reminder';

-- ===========================================================================
-- 2. Metric definitions
-- ===========================================================================

CREATE TABLE metrics.metric_definition (
    key             text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    version         integer NOT NULL CHECK (version > 0),
    name_pl         text NOT NULL CHECK (btrim(name_pl) <> ''),
    description_pl  text NOT NULL CHECK (btrim(description_pl) <> ''),
    grain           text NOT NULL CHECK (grain IN ('company', 'cost_object', 'counterparty', 'document', 'day')),
    unit            text NOT NULL CHECK (unit IN ('pln_minor', 'days', 'ratio', 'count')),
    implementation  text NOT NULL CHECK (implementation ~ '^(python|sql):'),   -- sql_or_python ref
    dimensions      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(dimensions) = 'array'),
    filters         jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(filters) = 'object'),
    traps_pl        text[] NOT NULL DEFAULT '{}',
    code_sha256     text NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deprecated')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT current_user,
    PRIMARY KEY (key, version)
);

COMMENT ON TABLE metrics.metric_definition IS
    'Definicja metryki: klucz + wersja (zmiana definicji = nowa wersja z testem), ziarno, jednostka, implementacja, '
    'wymiary, filtry, pułapki rozstrzygane definicją, skrót SHA-256 kodu definicji (zmiana kodu bez nowej wersji '
    'jest wykrywana przy synchronizacji rejestru).';

CREATE FUNCTION kasa_metric_definition_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status = 'deprecated'
       AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Definicja metryki %@% jest niezmienna (zmiana = nowa wersja; dozwolone tylko active -> deprecated)',
            OLD.key, OLD.version
        USING ERRCODE = 'KS013';
END;
$$;

CREATE TRIGGER metric_definition_guard
    BEFORE UPDATE OR DELETE ON metrics.metric_definition
    FOR EACH ROW EXECUTE FUNCTION kasa_metric_definition_guard();
CREATE TRIGGER metric_definition_no_truncate
    BEFORE TRUNCATE ON metrics.metric_definition
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- 3. Agent call log, metric proposals, export log
-- ===========================================================================

CREATE TABLE agent_call_log (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          uuid NOT NULL REFERENCES company (id),
    tool                text NOT NULL CHECK (tool ~ '^[a-z][a-z0-9_]*$'),
    args_pseudonymized  jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(args_pseudonymized) = 'object'),
    model               text,
    prompt_version      text,
    client              text,
    latency_ms          integer CHECK (latency_ms >= 0),
    status              text NOT NULL CHECK (status IN ('ok', 'refused', 'error')),
    result_summary      jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result_summary) = 'object'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT current_user
);

CREATE INDEX agent_call_log_company_idx ON agent_call_log (company_id, created_at);

COMMENT ON TABLE agent_call_log IS
    'Każde wywołanie narzędzia MCP: narzędzie, argumenty po pseudonimizacji, model, wersja promptu, klient, czas, '
    'skrót wyniku (bez tekstów); tylko dopisywanie (log dostępu - 3 lata, Krok 9).';

CREATE TRIGGER agent_call_log_append_only
    BEFORE UPDATE OR DELETE ON agent_call_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER agent_call_log_no_truncate
    BEFORE TRUNCATE ON agent_call_log
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

CREATE TABLE metric_proposal (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id     uuid NOT NULL REFERENCES company (id),
    question       text NOT NULL CHECK (btrim(question) <> ''),     -- after pseudonymization
    requested_key  text,
    reason         text,
    proposed_by    text NOT NULL CHECK (proposed_by IN ('agent', 'human')),
    status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected')),
    agent_call_id  uuid,
    decided_by     text,
    decided_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL DEFAULT current_user,
    CONSTRAINT metric_proposal_decided_check CHECK ((status = 'open') = (decided_at IS NULL)
                                                    AND (decided_at IS NULL) = (decided_by IS NULL))
);

CREATE INDEX metric_proposal_open_idx ON metric_proposal (company_id, created_at) WHERE status = 'open';

COMMENT ON TABLE metric_proposal IS
    'Pytanie poza warstwą metryk (np. CIT): agent odmawia i zapisuje propozycję metryki zamiast wymyślać liczbę. '
    'Decyzję (accepted/rejected) podejmuje człowiek, raz.';

CREATE FUNCTION kasa_metric_proposal_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'open' AND NEW.status IN ('accepted', 'rejected')
       AND (to_jsonb(NEW) - ARRAY['status', 'decided_by', 'decided_at'])
           = (to_jsonb(OLD) - ARRAY['status', 'decided_by', 'decided_at']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Propozycja metryki (id %) przyjmuje decyzję tylko raz; nic nie kasujemy', OLD.id
        USING ERRCODE = 'KS012';
END;
$$;

CREATE TRIGGER metric_proposal_guard
    BEFORE UPDATE OR DELETE ON metric_proposal
    FOR EACH ROW EXECUTE FUNCTION kasa_metric_proposal_guard();
CREATE TRIGGER metric_proposal_no_truncate
    BEFORE TRUNCATE ON metric_proposal
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

CREATE TABLE export_log (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   uuid NOT NULL REFERENCES company (id),
    exported_by  text NOT NULL CHECK (btrim(exported_by) <> ''),
    recipient    text NOT NULL DEFAULT 'biuro rachunkowe',
    scope        jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),      -- month, format, kinds
    files        jsonb NOT NULL CHECK (jsonb_typeof(files) = 'array'),       -- [{name, sha256, bytes, rows}]
    exported_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX export_log_company_idx ON export_log (company_id, exported_at);

COMMENT ON TABLE export_log IS
    'Każde udostępnienie danych biuru (Krok 9): kto, kiedy, zakres, skróty plików; tylko dopisywanie.';

CREATE TRIGGER export_log_append_only
    BEFORE UPDATE OR DELETE ON export_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER export_log_no_truncate
    BEFORE TRUNCATE ON export_log
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- 4a. Helpers used by the agent's views (company filter, masking of personal data)
-- ===========================================================================

-- The company of the session (set by the MCP server: SET kasa.company_id = '<uuid>'); NULL = nothing.
CREATE FUNCTION kasa_agent_company() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('kasa.company_id', true), '')::uuid
$$;

-- A transfer that is a person's remuneration (payroll): decided or proposed "no document / salary",
-- or a "salary" title to a person without any document. The agent never sees it tied to a person.
CREATE FUNCTION kasa_tx_is_payroll(p_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM l3_transaction_status s
                    WHERE s.transaction_id = p_id AND s.status = 'active' AND s.final_status = 'no_document'
                      AND s.subtype = 'salary')
        OR EXISTS (SELECT 1 FROM l2_suggestion g
                    WHERE g.subject_type = 'transaction' AND g.subject_id = p_id AND g.kind = 'match'
                      AND g.status = 'open' AND g.rank = 1 AND g.proposal ->> 'final_status' = 'no_document'
                      AND g.proposal ->> 'subtype' = 'salary')
        OR EXISTS (SELECT 1 FROM l1_bank_transaction t
                     JOIN l1_counterparty c ON c.id = t.counterparty_id AND c.kind = 'person'
                     JOIN title_semantics_current s ON s.transaction_id = t.id AND s.intent = 'salary'
                    WHERE t.id = p_id AND t.amount_minor < 0
                      AND NOT EXISTS (SELECT 1 FROM l3_match m WHERE m.transaction_id = t.id AND m.status = 'active'
                                         AND (m.document_id IS NOT NULL OR m.internal_record_id IS NOT NULL)))
$$;

-- A document of a person (bill of a person, or a person as the counterparty).
CREATE FUNCTION kasa_doc_is_person(p_kind text, p_counterparty uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
    SELECT p_kind = 'bill' OR EXISTS (SELECT 1 FROM l1_counterparty c WHERE c.id = p_counterparty AND c.kind = 'person')
$$;

CREATE FUNCTION kasa_doc_is_person(p_document uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
    SELECT coalesce((SELECT kasa_doc_is_person(d.kind::text, d.counterparty_id) FROM l1_document d
                      WHERE d.id = p_document), false)
$$;

-- The pseudonymous display name of a counterparty (persons only by code; companies as named).
CREATE FUNCTION kasa_party_display(p_counterparty uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
    SELECT CASE WHEN c.kind = 'person' THEN coalesce(c.name_pseudonymized, 'osoba')
                ELSE coalesce(c.name_pseudonymized, c.name) END
      FROM l1_counterparty c
     WHERE c.id = p_counterparty AND c.company_id = coalesce(kasa_agent_company(), c.company_id)
$$;

-- Fields of a row of an internal file without personal data: the person -> pseudonym, contact
-- data and free notes dropped (keys compared case-insensitively).
CREATE FUNCTION kasa_mask_internal_fields(
    p_fields jsonb, p_pseudonym text,
    p_person_keys text[] DEFAULT ARRAY['osoba', 'person', 'imię i nazwisko', 'imie i nazwisko', 'nazwisko',
                                       'pracownik', 'wykonawca'],
    p_drop_keys text[] DEFAULT ARRAY['uwagi', 'notes', 'telefon', 'tel', 'tel.', 'phone', 'iban', 'rachunek',
                                     'konto', 'nr konta', 'adres', 'address', 'pesel', 'email', 'e-mail']
) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(jsonb_object_agg(e.k, CASE WHEN lower(e.k) = ANY (p_person_keys)
                                               THEN to_jsonb(coalesce(p_pseudonym, 'osoba')) ELSE e.v END)
                        FILTER (WHERE NOT lower(e.k) = ANY (p_drop_keys)), '{}'::jsonb)
      FROM jsonb_each(coalesce(p_fields, '{}'::jsonb)) AS e (k, v)
$$;

-- extracted.phones without the numbers (only the pseudonyms stay).
CREATE FUNCTION kasa_strip_phone_numbers(p_extracted jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_extracted IS NULL OR NOT p_extracted ? 'phones' OR jsonb_typeof(p_extracted -> 'phones') <> 'array'
            THEN p_extracted
        ELSE jsonb_set(p_extracted, '{phones}', coalesce((
            SELECT jsonb_agg(CASE WHEN jsonb_typeof(p) = 'object' THEN p - 'number' ELSE '"T-?"'::jsonb END)
              FROM jsonb_array_elements(p_extracted -> 'phones') AS p), '[]'::jsonb))
    END
$$;

-- The rank reason of a queue card with the counterparty of a transfer in its pseudonymous form.
CREATE FUNCTION kasa_rank_reason_masked(p_reason text, p_subject_type subject_type, p_subject_id uuid) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
    SELECT coalesce((
        SELECT CASE WHEN t.counterparty_name_raw IS NOT NULL
                         AND t.counterparty_pseudonym IS DISTINCT FROM t.counterparty_name_raw
                    THEN replace(p_reason, t.counterparty_name_raw, coalesce(t.counterparty_pseudonym, 'osoba'))
                    ELSE p_reason END
          FROM l1_bank_transaction t
         WHERE p_subject_type = 'transaction' AND t.id = p_subject_id), p_reason)
$$;

-- The polymorphic subject check runs for the agent's suggestion role too (it has no SELECT on L1).
ALTER FUNCTION kasa_subject_exists(subject_type, uuid, uuid) SECURITY DEFINER;
ALTER FUNCTION kasa_subject_exists(subject_type, uuid, uuid) SET search_path = public, pg_catalog;

-- ===========================================================================
-- 4b. evidence: per-company mirrors of the facts without raw personal data (rebuilt by a function,
--     so that a column added by a later migration reaches the agent with the same masking rules)
-- ===========================================================================

CREATE FUNCTION kasa_refresh_agent_views() RETURNS integer
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
            'rank_reason', 'kasa_rank_reason_masked(x.rank_reason, x.subject_type, x.subject_id)'));
    v_filters jsonb := '{
        "company": "x.id = kasa_agent_company()",
        "l1_fx_rate": "(x.company_id IS NULL OR x.company_id = kasa_agent_company())"
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

-- ===========================================================================
-- 4c. Roles (cluster-wide; created once, privileges per database)
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kasa_agent_ro') THEN
        CREATE ROLE kasa_agent_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kasa_agent_suggest') THEN
        CREATE ROLE kasa_agent_suggest NOLOGIN;
    END IF;
END;
$$;

COMMENT ON ROLE kasa_agent_ro IS
    'Agent MCP: tylko SELECT na schematach metrics, views, evidence; limit 15 s; firma z kasa.company_id.';
COMMENT ON ROLE kasa_agent_suggest IS 'Agent MCP: tylko INSERT do l2_suggestion, metric_proposal, agent_call_log.';

ALTER ROLE kasa_agent_ro SET statement_timeout = '15s';
ALTER ROLE kasa_agent_ro SET search_path = evidence, views, metrics, public;
ALTER ROLE kasa_agent_ro SET default_transaction_read_only = on;
ALTER ROLE kasa_agent_suggest SET statement_timeout = '15s';
ALTER ROLE kasa_agent_suggest SET search_path = public;

GRANT USAGE ON SCHEMA metrics, views, evidence TO kasa_agent_ro;
GRANT INSERT ON l2_suggestion, metric_proposal, agent_call_log TO kasa_agent_suggest;

-- Functions that change data with the owner's rights: never for PUBLIC (the agent's roles included).
REVOKE EXECUTE ON FUNCTION kasa_purge_raw(uuid, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kasa_expire_llm_payloads(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kasa_refresh_agent_views() FROM PUBLIC;

-- ===========================================================================
-- 4d. views: question views of the agent (filters chosen by the server, never free SQL)
-- ===========================================================================

CREATE VIEW views.transactions WITH (security_barrier) AS
SELECT t.id, t.entry_reference, t.booking_date, t.amount_minor, t.currency::text AS currency,
       CASE WHEN t.amount_minor >= 0 THEN 'wpływ' ELSE 'wypływ' END AS direction,
       a.kind::text AS account_kind, a.iban AS account_iban, t.op_type::text AS op_type,
       coalesce(kasa_party_display(t.counterparty_id), t.counterparty_pseudonym) AS counterparty,
       c.kind::text AS counterparty_kind,
       jsonb_build_object('untrusted_text', t.title_pseudonymized) AS title,
       s.final_status::text AS final_status, s.subtype, s.method::text AS status_method,
       (SELECT array_agg(DISTINCT d.number_raw ORDER BY d.number_raw) FROM public.l3_match m
          JOIN public.l1_document d ON d.id = m.document_id
         WHERE m.transaction_id = t.id AND m.status = 'active') AS matched_documents,
       (SELECT cd.category FROM public.l3_category_decision cd WHERE cd.subject_type = 'transaction'
           AND cd.subject_id = t.id AND cd.status = 'active' LIMIT 1) AS category,
       (SELECT array_agg(DISTINCT coalesce(r.entry_reference, r.id::text)) FROM public.l3_match m
          JOIN public.l1_bank_transaction r ON r.id = CASE WHEN m.transaction_id = t.id THEN m.related_transaction_id
                                                           ELSE m.transaction_id END
         WHERE m.status = 'active' AND m.related_transaction_id IS NOT NULL
           AND (m.transaction_id = t.id OR m.related_transaction_id = t.id)) AS related_transactions,
       ts.intent, t.mpp_role
  FROM public.l1_bank_transaction t
  JOIN public.l1_bank_account a ON a.id = t.account_id
  LEFT JOIN public.l1_counterparty c ON c.id = t.counterparty_id
  LEFT JOIN public.l3_transaction_status s ON s.transaction_id = t.id AND s.status = 'active'
  LEFT JOIN public.title_semantics_current ts ON ts.transaction_id = t.id
 WHERE t.company_id = kasa_agent_company() AND t.superseded_by IS NULL AND t.status = 'booked'
   AND NOT t.sensitive AND NOT kasa_tx_is_payroll(t.id);

COMMENT ON VIEW views.transactions IS
    'Zaksięgowane przelewy firmy: tytuł tylko jako {"untrusted_text": tytuł po pseudonimizacji}, kontrahent-osoba '
    'kodem; bez przelewów ze słowem wrażliwym (dane tylko w kolejce) i bez wynagrodzeń osób (kasa_tx_is_payroll).';

CREATE VIEW views.documents WITH (security_barrier) AS
WITH paid AS (
    SELECT l.head_id, sum(coalesce(m.document_amount_minor, m.amount_minor)) AS paid_minor,
           array_agg(DISTINCT t.entry_reference) FILTER (WHERE t.entry_reference IS NOT NULL) AS transfers
      FROM public.l3_match m
      JOIN public.document_lineage l ON l.document_id = m.document_id
      JOIN public.l1_bank_transaction t ON t.id = m.transaction_id
     WHERE m.status = 'active' AND m.company_id = kasa_agent_company()
     GROUP BY l.head_id
)
SELECT d.id, d.direction::text AS direction, d.kind::text AS kind, d.number_raw AS number, d.ksef_number,
       d.issue_date, d.sale_date, d.due_date, d.service_period_start, d.service_period_end,
       CASE WHEN kasa_doc_is_person(d.kind::text, d.counterparty_id)
            THEN coalesce(kasa_party_display(d.counterparty_id), 'osoba')
            ELSE coalesce(kasa_party_display(d.counterparty_id),
                          CASE WHEN d.direction = 'sales' THEN d.buyer_name ELSE d.seller_name END) END AS counterparty,
       d.currency::text AS currency, d.net_minor, d.vat_minor, d.gross_minor, d.gross_pln_minor,
       coalesce(p.paid_minor, 0) AS paid_minor, p.transfers, d.corrects_document_id, d.source
  FROM public.l1_document d
  LEFT JOIN paid p ON p.head_id = d.id
 WHERE d.company_id = kasa_agent_company() AND d.superseded_by IS NULL;

COMMENT ON VIEW views.documents IS
    'Bieżące dokumenty firmy z zapłatą (aktywne dopasowania łańcucha wersji); osoby tylko kodem.';

CREATE VIEW views.receivables WITH (security_barrier) AS
SELECT d.id, d.number, d.ksef_number, d.kind, d.issue_date, d.due_date, d.due_date IS NULL AS due_date_missing,
       d.counterparty, d.currency, d.gross_minor, d.paid_minor, d.gross_minor - d.paid_minor AS open_minor
  FROM views.documents d
 WHERE d.direction = 'sales' AND d.kind NOT IN ('proforma', 'correction')
   AND d.gross_minor - d.paid_minor
       + coalesce((SELECT sum(k.gross_minor - k.paid_minor) FROM views.documents k
                    WHERE k.corrects_document_id = d.id), 0) <> 0;

COMMENT ON VIEW views.receivables IS
    'Faktury sprzedaży z otwartym saldem (łańcuch z korektami); wiek liczy serwer na dzień as_of '
    '(brak terminu = od wystawienia, oznaczone). Kwoty i kubełki - metryka receivables_open.';

CREATE VIEW views.cost_objects WITH (security_barrier) AS
SELECT o.id, o.name, o.kind, o.status::text AS status, o.system_code, o.attrs ->> 'client' AS client,
       o.attrs ->> 'city' AS city, (o.attrs ->> 'date_start') AS date_start, (o.attrs ->> 'date_end') AS date_end,
       (SELECT count(*) FROM public.l3_allocation a WHERE a.cost_object_id = o.id AND a.status = 'active')
           AS allocations,
       o.method::text AS method
  FROM public.l3_cost_object o
 WHERE o.company_id = kasa_agent_company() AND o.status IN ('approved', 'candidate');

CREATE VIEW views.allocations WITH (security_barrier) AS
SELECT a.id, a.subject_type::text AS subject_type, a.subject_id,
       CASE a.subject_type
           WHEN 'line' THEN (SELECT d.number_raw || ' - ' || coalesce(l.name, '') FROM public.l1_document_line l
                               JOIN public.l1_document d ON d.id = l.document_id WHERE l.id = a.subject_id)
           WHEN 'document' THEN (SELECT d.number_raw FROM public.l1_document d WHERE d.id = a.subject_id)
           WHEN 'internal_record' THEN (SELECT 'arkusz: ' || coalesce(r.cost_object_ref, '') || ' '
                                               || coalesce(r.record_date::text, '') FROM public.l1_internal_record r
                                         WHERE r.id = a.subject_id)
           WHEN 'transaction' THEN (SELECT 'przelew ' || t.booking_date::text FROM public.l1_bank_transaction t
                                     WHERE t.id = a.subject_id)
       END AS subject_label,
       CASE a.subject_type
           WHEN 'line' THEN (SELECT CASE WHEN kasa_doc_is_person(d.kind::text, d.counterparty_id)
                                         THEN coalesce(kasa_party_display(d.counterparty_id), 'osoba')
                                         ELSE coalesce(kasa_party_display(d.counterparty_id), d.seller_name) END
                               FROM public.l1_document_line l JOIN public.l1_document d ON d.id = l.document_id
                              WHERE l.id = a.subject_id)
           WHEN 'document' THEN (SELECT CASE WHEN kasa_doc_is_person(d.kind::text, d.counterparty_id)
                                             THEN coalesce(kasa_party_display(d.counterparty_id), 'osoba')
                                             ELSE coalesce(kasa_party_display(d.counterparty_id), d.seller_name) END
                                   FROM public.l1_document d WHERE d.id = a.subject_id)
           WHEN 'transaction' THEN (SELECT CASE WHEN kasa_tx_is_payroll(t.id) THEN 'wynagrodzenie (osoba ukryta)'
                                                ELSE coalesce(kasa_party_display(t.counterparty_id),
                                                              t.counterparty_pseudonym) END
                                      FROM public.l1_bank_transaction t WHERE t.id = a.subject_id)
           ELSE 'arkusz wewnętrzny'
       END AS counterparty,
       o.name AS cost_object, o.system_code, a.share, a.amount_minor, a.method::text AS method,
       a.signal_strength, a.cascade_step, a.decision_id, dl.decided_at, dl.decided_by
  FROM public.l3_allocation a
  JOIN public.l3_cost_object o ON o.id = a.cost_object_id
  LEFT JOIN public.l3_decision_log dl ON dl.id = a.decision_id
 WHERE a.company_id = kasa_agent_company() AND a.status = 'active';

COMMENT ON VIEW views.allocations IS
    'Alokacje w mocy z dowodem (decyzja, kto, kiedy); wiersze arkuszy bez osoby, wynagrodzenia bez osoby.';

CREATE VIEW views.queue WITH (security_barrier) AS
SELECT q.id, q.position, q.card_kind, q.subject_type::text AS subject_type, q.subject_id,
       jsonb_build_object('untrusted_text', kasa_rank_reason_masked(q.rank_reason, q.subject_type, q.subject_id))
           AS rank_reason,
       q.badge, q.amount_minor, q.priority, q.deferred_until, q.built_at
  FROM public.queue_state q
 WHERE q.company_id = kasa_agent_company() AND q.closed_at IS NULL AND q.card_kind <> 'sentinel';

COMMENT ON VIEW views.queue IS
    'Otwarte karty kolejki przeglądu (bez wartowników - ich wynik jest ukryty); powód rangi jako untrusted_text.';

GRANT SELECT ON ALL TABLES IN SCHEMA views TO kasa_agent_ro;
GRANT SELECT ON metrics.metric_definition TO kasa_agent_ro;

SELECT kasa_refresh_agent_views();

-- ===========================================================================
-- 5a. Retention: the model input of l1_semantics (review of phase 3, points 4 and 10)
-- ===========================================================================

ALTER TABLE l1_semantics
    ADD COLUMN input_sha256      text CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN input_chars       integer CHECK (input_chars >= 0),
    ADD COLUMN input_expires_at  timestamptz,
    ADD COLUMN input_expired_at  timestamptz;

UPDATE l1_semantics
   SET input_sha256 = encode(sha256(convert_to(input_pseudonymized, 'UTF8')), 'hex'),
       input_chars = length(input_pseudonymized),
       input_expires_at = created_at + interval '90 days';

ALTER TABLE l1_semantics
    ALTER COLUMN input_pseudonymized DROP NOT NULL,
    ALTER COLUMN input_sha256 SET NOT NULL,
    ALTER COLUMN input_chars SET NOT NULL,
    ALTER COLUMN input_expires_at SET NOT NULL,
    ADD CONSTRAINT l1_semantics_input_expired_check
        CHECK ((input_expired_at IS NULL) = (input_pseudonymized IS NOT NULL));

COMMENT ON COLUMN l1_semantics.input_pseudonymized IS
    'To, co widział model (po bramce) - tylko 90 dni; potem NULL przez kasa_expire_semantics_inputs(), '
    'zostają input_sha256 i input_chars.';

CREATE FUNCTION kasa_semantics_input_digest() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.input_sha256 := coalesce(NEW.input_sha256,
                                 encode(sha256(convert_to(coalesce(NEW.input_pseudonymized, ''), 'UTF8')), 'hex'));
    NEW.input_chars := coalesce(NEW.input_chars, length(coalesce(NEW.input_pseudonymized, '')));
    NEW.input_expires_at := coalesce(NEW.input_expires_at, coalesce(NEW.created_at, now()) + interval '90 days');
    RETURN NEW;
END;
$$;

CREATE TRIGGER l1_semantics_input_digest
    BEFORE INSERT ON l1_semantics
    FOR EACH ROW EXECUTE FUNCTION kasa_semantics_input_digest();

CREATE FUNCTION kasa_semantics_input_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.input_pseudonymized, NEW.input_sha256, NEW.input_chars, NEW.input_expires_at, NEW.input_expired_at)
       IS NOT DISTINCT FROM
       (OLD.input_pseudonymized, OLD.input_sha256, OLD.input_chars, OLD.input_expires_at, OLD.input_expired_at) THEN
        RETURN NEW;
    END IF;
    IF coalesce(current_setting('kasa.expire_semantics', true), '') = 'on'
       AND OLD.input_expired_at IS NULL AND NEW.input_expired_at IS NOT NULL AND NEW.input_pseudonymized IS NULL
       AND (NEW.input_sha256, NEW.input_chars, NEW.input_expires_at)
           IS NOT DISTINCT FROM (OLD.input_sha256, OLD.input_chars, OLD.input_expires_at) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Wejście modelu w l1_semantics (id %) zmienia tylko retencja: SELECT kasa_expire_semantics_inputs()',
            OLD.id
        USING ERRCODE = 'KS014';
END;
$$;

CREATE TRIGGER l1_semantics_input_guard
    BEFORE UPDATE ON l1_semantics
    FOR EACH ROW EXECUTE FUNCTION kasa_semantics_input_guard();

-- Empties every model input past its 90 days (as of p_now); the digest and the length stay.
CREATE FUNCTION kasa_expire_semantics_inputs(p_now timestamptz DEFAULT now()) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    v_count integer;
BEGIN
    PERFORM set_config('kasa.expire_semantics', 'on', true);
    UPDATE l1_semantics
       SET input_pseudonymized = NULL, input_expired_at = p_now
     WHERE input_expired_at IS NULL AND input_expires_at <= p_now;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    PERFORM set_config('kasa.expire_semantics', 'off', true);
    RETURN v_count;
END;
$$;

REVOKE EXECUTE ON FUNCTION kasa_expire_semantics_inputs(timestamptz) FROM PUBLIC;

-- ===========================================================================
-- 5b. Retention: anonymization of persons (review of phase 2, point 3)
-- ===========================================================================

CREATE TABLE anonymization_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id     uuid NOT NULL REFERENCES company (id),
    before_date    date NOT NULL,
    reason         text NOT NULL CHECK (btrim(reason) <> ''),
    counts         jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object'),
    anonymized_by  text NOT NULL CHECK (btrim(anonymized_by) <> ''),
    anonymized_at  timestamptz NOT NULL DEFAULT now(),
    xact_id        xid8 NOT NULL DEFAULT pg_current_xact_id()
);

CREATE INDEX anonymization_log_company_idx ON anonymization_log (company_id, anonymized_at);

CREATE TRIGGER anonymization_log_append_only
    BEFORE UPDATE OR DELETE ON anonymization_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER anonymization_log_no_truncate
    BEFORE TRUNCATE ON anonymization_log
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- Raw personal columns (TG_ARGV: column names, 'a.b' = a jsonb path) change only inside
-- kasa_anonymize_persons(): the flag kasa.anonymize = 'on' and a log row of this transaction.
CREATE FUNCTION kasa_raw_person_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_column text;
    v_path text[];
BEGIN
    FOREACH v_column IN ARRAY TG_ARGV LOOP
        v_path := string_to_array(v_column, '.');
        IF (to_jsonb(NEW) #> v_path) IS DISTINCT FROM (to_jsonb(OLD) #> v_path) THEN
            IF coalesce(current_setting('kasa.anonymize', true), '') = 'on'
               AND EXISTS (SELECT 1 FROM anonymization_log l
                            WHERE l.company_id = NEW.company_id AND l.xact_id = pg_current_xact_id()) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Kolumna surowa %.% (id %) zmienia się tylko przez kasa_anonymize_persons()',
                    TG_TABLE_NAME, v_column, OLD.id
                USING ERRCODE = 'KS014',
                      HINT = 'Fakty L1 nie są poprawiane; anonimizacja osób po retencji to jedyny wyjątek.';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER l1_bank_transaction_raw_person_guard
    BEFORE UPDATE ON l1_bank_transaction
    FOR EACH ROW EXECUTE FUNCTION kasa_raw_person_guard('title_raw', 'counterparty_name_raw',
                                                        'counterparty_iban_raw', 'extracted.phones');
CREATE TRIGGER l1_internal_record_raw_person_guard
    BEFORE UPDATE ON l1_internal_record
    FOR EACH ROW EXECUTE FUNCTION kasa_raw_person_guard('fields');
CREATE TRIGGER l1_pdf_extraction_raw_person_guard
    BEFORE UPDATE ON l1_pdf_extraction
    FOR EACH ROW EXECUTE FUNCTION kasa_raw_person_guard('extracted');
CREATE TRIGGER l1_document_raw_person_guard
    BEFORE UPDATE ON l1_document
    FOR EACH ROW EXECUTE FUNCTION kasa_raw_person_guard('seller_name', 'buyer_name', 'payment_iban');

-- Records of a company dated before p_before: raw personal columns overwritten by their
-- pseudonymous form (amounts, dates, cost objects and companies stay - briefing, step 9, D6).
-- Idempotent; returns the log id (NULL when nothing was left to anonymize).
CREATE FUNCTION kasa_anonymize_persons(
    p_company_id uuid, p_before date, p_actor text DEFAULT NULL,
    p_reason text DEFAULT 'retencja: umowa + 1 rok (Krok 9)',
    p_person_keys text[] DEFAULT ARRAY['osoba', 'person', 'imię i nazwisko', 'imie i nazwisko', 'nazwisko',
                                       'pracownik', 'wykonawca'],
    p_drop_keys text[] DEFAULT ARRAY['uwagi', 'notes', 'telefon', 'tel', 'tel.', 'phone', 'iban', 'rachunek',
                                     'konto', 'nr konta', 'adres', 'address', 'pesel', 'email', 'e-mail']
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_catalog, pg_temp AS $$
DECLARE
    v_log uuid;
    v_tx integer;
    v_rec integer;
    v_doc integer;
    v_pdf integer;
    v_party integer;
BEGIN
    IF p_before IS NULL THEN
        RAISE EXCEPTION 'Podaj datę: anonimizujemy rekordy sprzed niej' USING ERRCODE = '22023';
    END IF;

    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_tx (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_rec (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_doc (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_pdf (id uuid PRIMARY KEY) ON COMMIT DROP;
    CREATE TEMP TABLE IF NOT EXISTS pg_temp._kasa_anon_party (id uuid PRIMARY KEY) ON COMMIT DROP;
    TRUNCATE pg_temp._kasa_anon_tx, pg_temp._kasa_anon_rec, pg_temp._kasa_anon_doc, pg_temp._kasa_anon_pdf, pg_temp._kasa_anon_party;

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
    INSERT INTO pg_temp._kasa_anon_party
    SELECT c.id FROM l1_counterparty c
     WHERE c.company_id = p_company_id AND c.kind = 'person'
       AND (c.name IS DISTINCT FROM coalesce(c.name_pseudonymized, 'osoba') OR cardinality(c.ibans) > 0)
       AND NOT EXISTS (SELECT 1 FROM l1_bank_transaction t WHERE t.counterparty_id = c.id
                         AND coalesce(t.booking_date, t.transaction_date, t.created_at::date) >= p_before)
       AND NOT EXISTS (SELECT 1 FROM l1_document d WHERE d.counterparty_id = c.id AND d.issue_date >= p_before);

    SELECT count(*) INTO v_tx FROM pg_temp._kasa_anon_tx;
    SELECT count(*) INTO v_rec FROM pg_temp._kasa_anon_rec;
    SELECT count(*) INTO v_doc FROM pg_temp._kasa_anon_doc;
    SELECT count(*) INTO v_pdf FROM pg_temp._kasa_anon_pdf;
    SELECT count(*) INTO v_party FROM pg_temp._kasa_anon_party;
    IF v_tx + v_rec + v_doc + v_pdf + v_party = 0 THEN
        RETURN NULL;
    END IF;

    INSERT INTO anonymization_log (company_id, before_date, reason, counts, anonymized_by)
    VALUES (p_company_id, p_before, btrim(p_reason),
            jsonb_build_object('l1_bank_transaction', v_tx, 'l1_internal_record', v_rec, 'l1_document', v_doc,
                               'l1_pdf_extraction', v_pdf, 'l1_counterparty', v_party),
            coalesce(nullif(btrim(p_actor), ''), session_user))
    RETURNING id INTO v_log;

    PERFORM set_config('kasa.anonymize', 'on', true);
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
    UPDATE l1_counterparty c
       SET name = coalesce(c.name_pseudonymized, 'osoba'), name_key = NULL, ibans = '{}'::text[]
     WHERE c.id IN (SELECT id FROM pg_temp._kasa_anon_party);
    PERFORM set_config('kasa.anonymize', 'off', true);
    RETURN v_log;
END;
$$;

REVOKE EXECUTE ON FUNCTION kasa_anonymize_persons(uuid, date, text, text, text[], text[]) FROM PUBLIC;

COMMENT ON FUNCTION kasa_anonymize_persons(uuid, date, text, text, text[], text[]) IS
    'Jedyna ścieżka UPDATE nadpisująca surowe kolumny osób (tytuł, nadawca, IBAN osoby, telefony, pola arkusza, '
    'odczyt PDF i nazwa na rachunku osoby, kontrahent-osoba) formą pseudonimową; wpis w anonymization_log.';
