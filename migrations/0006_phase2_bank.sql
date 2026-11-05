-- 0006_phase2_bank: phase 2 (acquisition) - bank facts and the review items
-- that had to land before the first import (docs/PRZEGLADY.md, points 1-4, 14).
--
--   1. L0 retention: the only allowed change of a raw payload is a purge done by
--      kasa_purge_raw() (payload -> empty, sha256 kept, purged_at set, logged).
--   2. Pseudonym keys (pseudonym_key) and pseudonymized columns of bank transactions.
--   3. Split payment (MPP) as one occurrence among facts: mpp_group_id + mpp_role.
--   4. Counterparties as facts (l1_counterparty) + counterparty_id in transactions
--      and documents, one shared upsert function kasa_upsert_counterparty().
--   5. Format of the KSeF number on l1_document.
--   + bank balances reported by the bank (l1_bank_balance), wider bank transaction
--     record, results of truth checks (truth_check_result).
--
-- New error code:
--   KS008  pseudonym key: only shredding (value -> NULL) is allowed, nothing else changes

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ===========================================================================
-- 1. L0 retention
-- ===========================================================================

ALTER TABLE l0_raw ADD COLUMN purged_at timestamptz;

-- The digest is kept after a purge: it still proves what was stored and keeps
-- the (company_id, sha256) dedup key, so a purged file is not stored again.
ALTER TABLE l0_raw DROP CONSTRAINT l0_raw_sha256_matches_payload;
ALTER TABLE l0_raw ADD CONSTRAINT l0_raw_sha256_matches_payload
    CHECK (purged_at IS NOT NULL OR sha256 = encode(sha256(payload), 'hex'));
ALTER TABLE l0_raw ADD CONSTRAINT l0_raw_purged_payload_empty
    CHECK (purged_at IS NULL OR payload = ''::bytea);

-- One row per purge; written by kasa_purge_raw() before the payload is cleared.
CREATE TABLE l0_purge_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_id         uuid NOT NULL,
    company_id     uuid NOT NULL REFERENCES company (id),
    channel        raw_channel NOT NULL,
    external_ref   text,
    sha256         text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    payload_bytes  bigint NOT NULL CHECK (payload_bytes >= 0),
    reason         text NOT NULL CHECK (btrim(reason) <> ''),
    purged_by      text NOT NULL CHECK (btrim(purged_by) <> ''),
    purged_at      timestamptz NOT NULL DEFAULT now(),
    xact_id        xid8 NOT NULL DEFAULT pg_current_xact_id(),

    CONSTRAINT l0_purge_log_raw_key UNIQUE (raw_id),
    CONSTRAINT l0_purge_log_raw_fk FOREIGN KEY (raw_id, company_id) REFERENCES l0_raw (id, company_id)
);

CREATE INDEX l0_purge_log_company_idx ON l0_purge_log (company_id, purged_at);

CREATE TRIGGER l0_purge_log_append_only
    BEFORE UPDATE OR DELETE ON l0_purge_log
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();

CREATE TRIGGER l0_purge_log_no_truncate
    BEFORE TRUNCATE ON l0_purge_log
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- Append-only guard of l0_raw with exactly one exception: the purge performed by
-- kasa_purge_raw(). The exception needs all of: the transaction-local flag
-- kasa.purge = 'on' (set only inside kasa_purge_raw), a purge log row written in
-- the same transaction, and a change that only empties the payload and sets
-- purged_at. Any other UPDATE and every DELETE still raise KS001.
CREATE FUNCTION kasa_l0_raw_guard() RETURNS trigger
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
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS001',
              HINT = 'Surowe dane tylko dopisujemy. Jedyny wyjątek to retencja: SELECT kasa_purge_raw(id, powód).';
END;
$$;

DROP TRIGGER l0_raw_append_only ON l0_raw;
CREATE TRIGGER l0_raw_append_only
    BEFORE UPDATE OR DELETE ON l0_raw
    FOR EACH ROW EXECUTE FUNCTION kasa_l0_raw_guard();

-- Retention purge (briefing, step 9): clears the payload, keeps the row, the
-- digest and the metadata, and logs who purged what and why. Idempotent:
-- purging an already purged record returns the existing log id.
CREATE FUNCTION kasa_purge_raw(p_raw_id uuid, p_reason text, p_actor text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    r      l0_raw%ROWTYPE;
    v_log  uuid;
BEGIN
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'Podaj powód usunięcia treści surowego rekordu' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO r FROM l0_raw WHERE id = p_raw_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Nie ma surowego rekordu %', p_raw_id USING ERRCODE = 'P0002';
    END IF;
    IF r.purged_at IS NOT NULL THEN
        SELECT id INTO v_log FROM l0_purge_log WHERE raw_id = p_raw_id;
        RETURN v_log;
    END IF;

    INSERT INTO l0_purge_log (raw_id, company_id, channel, external_ref, sha256, payload_bytes, reason, purged_by)
    VALUES (r.id, r.company_id, r.channel, r.external_ref, r.sha256, octet_length(r.payload),
            btrim(p_reason), coalesce(nullif(btrim(p_actor), ''), session_user))
    RETURNING id INTO v_log;

    PERFORM set_config('kasa.purge', 'on', true);
    UPDATE l0_raw SET payload = ''::bytea, purged_at = now() WHERE id = p_raw_id;
    PERFORM set_config('kasa.purge', 'off', true);
    RETURN v_log;
END;
$$;

-- ===========================================================================
-- 2. Pseudonym keys
-- ===========================================================================

CREATE TYPE pseudonym_kind AS ENUM ('person', 'iban', 'phone', 'email', 'pesel', 'address');

-- One row per personal value of a company. value_hash = HMAC-SHA256 of the
-- normalized value with the secret from KASA_PSEUDONYM_SECRET (the same value
-- always gets the same pseudonym); value_encrypted = pgp_sym_encrypt() of the
-- value as first seen, with a key derived from the same secret. The key never
-- lives in the database. Shredding (value_encrypted -> NULL) anonymizes for good.
CREATE TABLE pseudonym_key (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       uuid NOT NULL REFERENCES company (id),
    kind             pseudonym_kind NOT NULL,
    pseudonym        text NOT NULL CHECK (pseudonym ~ '^[A-Z]-[0-9a-f]{4,16}$'),
    value_hash       text NOT NULL CHECK (value_hash ~ '^[0-9a-f]{64}$'),
    value_encrypted  bytea,
    key_version      integer NOT NULL DEFAULT 1 CHECK (key_version > 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    shredded_at      timestamptz,

    CONSTRAINT pseudonym_key_pseudonym_key UNIQUE (company_id, pseudonym),
    CONSTRAINT pseudonym_key_value_key UNIQUE (company_id, kind, value_hash),
    CONSTRAINT pseudonym_key_shred_check CHECK ((shredded_at IS NULL) = (value_encrypted IS NOT NULL))
);

CREATE INDEX pseudonym_key_kind_idx ON pseudonym_key (company_id, kind);
REVOKE ALL ON pseudonym_key FROM PUBLIC;

CREATE FUNCTION kasa_pseudonym_key_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Usuwanie kluczy pseudonimów jest zabronione' USING ERRCODE = 'KS002',
            HINT = 'Anonimizacja = zniszczenie klucza (value_encrypted -> NULL), pseudonim zostaje.';
    END IF;
    IF OLD.shredded_at IS NULL AND NEW.shredded_at IS NOT NULL AND NEW.value_encrypted IS NULL
       AND (NEW.id, NEW.company_id, NEW.kind, NEW.pseudonym, NEW.value_hash, NEW.key_version, NEW.created_at)
           IS NOT DISTINCT FROM
           (OLD.id, OLD.company_id, OLD.kind, OLD.pseudonym, OLD.value_hash, OLD.key_version, OLD.created_at)
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Klucz pseudonimu można tylko zniszczyć (value_encrypted -> NULL)' USING ERRCODE = 'KS008';
END;
$$;

CREATE TRIGGER pseudonym_key_guard
    BEFORE UPDATE OR DELETE ON pseudonym_key
    FOR EACH ROW EXECUTE FUNCTION kasa_pseudonym_key_guard();

CREATE TRIGGER pseudonym_key_no_truncate
    BEFORE TRUNCATE ON pseudonym_key
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- 4. Counterparties (facts; decisions about them come in phase 4)
-- ===========================================================================

CREATE TYPE counterparty_kind AS ENUM ('company', 'person', 'institution', 'bank', 'own');

-- Normalizers shared by SQL and the application.
CREATE FUNCTION kasa_name_key(p text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT nullif(btrim(regexp_replace(
        lower(translate(coalesce(p, ''), 'ĄĆĘŁŃÓŚŹŻąćęłńóśźż', 'ACELNOSZZacelnoszz')),
        '[^a-z0-9]+', ' ', 'g')), '')
$$;

-- 10 digits of a Polish NIP ("PL 526-000-12-46" -> "5260001246"), or NULL.
CREATE FUNCTION kasa_normalize_nip(p text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN v ~ '^[0-9]{10}$' THEN v END
      FROM (SELECT regexp_replace(regexp_replace(upper(coalesce(p, '')), '[^0-9A-Z]', '', 'g'), '^PL', '') AS v) s
$$;

CREATE FUNCTION kasa_nip_valid(p text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    w int[] := ARRAY[6, 5, 7, 2, 3, 4, 5, 6, 7];
    s int := 0;
BEGIN
    IF p IS NULL OR p !~ '^[0-9]{10}$' THEN
        RETURN false;
    END IF;
    FOR i IN 1..9 LOOP
        s := s + substr(p, i, 1)::int * w[i];
    END LOOP;
    RETURN s % 11 <> 10 AND s % 11 = substr(p, 10, 1)::int;
END;
$$;

CREATE FUNCTION kasa_iban_valid(p text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    v text;
    ch text;
    r int := 0;
    d text;
BEGIN
    IF p IS NULL OR p !~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$' THEN
        RETURN false;
    END IF;
    v := substr(p, 5) || substr(p, 1, 4);
    FOR i IN 1..length(v) LOOP
        ch := substr(v, i, 1);
        d := CASE WHEN ch ~ '[0-9]' THEN ch ELSE (ascii(ch) - 55)::text END;
        FOR j IN 1..length(d) LOOP
            r := (r * 10 + substr(d, j, 1)::int) % 97;
        END LOOP;
    END LOOP;
    RETURN r = 1;
END;
$$;

-- Upper-case IBAN without separators; a bare 26-digit NRB gets "PL". NULL when
-- the result is not a valid IBAN (mod-97).
CREATE FUNCTION kasa_normalize_iban(p text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN kasa_iban_valid(v) THEN v END
      FROM (SELECT CASE WHEN w ~ '^[0-9]{26}$' THEN 'PL' || w ELSE w END AS v
              FROM (SELECT regexp_replace(regexp_replace(upper(coalesce(p, '')), '[^0-9A-Z]', '', 'g'), '^IBAN', '') AS w) s0) s
$$;

CREATE FUNCTION kasa_ibans_valid(p text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT p IS NOT NULL AND coalesce(bool_and(x IS NOT NULL AND x ~ '^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$'), true)
      FROM unnest(p) AS x
$$;

-- A counterparty is one party as seen in facts (NIP from invoices and MPP
-- blocks, IBAN and name from the bank). It is enriched in place (gaps filled,
-- IBANs added, never overwritten); two rows found to be the same party are
-- merged by pointing the old one at the survivor through superseded_by.
CREATE TABLE l1_counterparty (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id          uuid NOT NULL REFERENCES company (id),
    nip                 text CHECK (nip ~ '^[0-9]{10}$'),
    foreign_tax_id      text CHECK (foreign_tax_id IS NULL OR btrim(foreign_tax_id) <> ''),
    name                text,
    name_key            text,               -- kasa_name_key(name), lookup key for parties known by name only
    name_pseudonymized  text,               -- name as the model may see it (persons replaced by pseudonyms)
    ibans               text[] NOT NULL DEFAULT '{}' CHECK (kasa_ibans_valid(ibans)),
    kind                counterparty_kind NOT NULL,
    superseded_by       uuid,

    -- provenance
    source              text NOT NULL CHECK (btrim(source) <> ''),
    method              provenance_method NOT NULL CHECK (method <> 'rule'),
    confidence          numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) IN ('object', 'array')),
    schema_version      integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    prompt_version      text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL CHECK (btrim(created_by) <> ''),
    CONSTRAINT l1_counterparty_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_counterparty_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_counterparty_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_counterparty (id, company_id),
    CONSTRAINT l1_counterparty_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    CONSTRAINT l1_counterparty_identity_check
        CHECK (nip IS NOT NULL OR foreign_tax_id IS NOT NULL OR name IS NOT NULL OR cardinality(ibans) > 0)
);

-- One current counterparty per NIP and one "own" counterparty per company.
CREATE UNIQUE INDEX l1_counterparty_nip_key ON l1_counterparty (company_id, nip)
    WHERE nip IS NOT NULL AND superseded_by IS NULL;
CREATE UNIQUE INDEX l1_counterparty_own_key ON l1_counterparty (company_id)
    WHERE kind = 'own' AND superseded_by IS NULL;
CREATE INDEX l1_counterparty_ibans_idx ON l1_counterparty USING gin (ibans);
CREATE INDEX l1_counterparty_name_key_idx ON l1_counterparty (company_id, name_key);
CREATE INDEX l1_counterparty_foreign_tax_id_idx ON l1_counterparty (company_id, foreign_tax_id)
    WHERE foreign_tax_id IS NOT NULL;

CREATE TRIGGER l1_counterparty_no_delete
    BEFORE DELETE ON l1_counterparty
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l1_counterparty_no_truncate
    BEFORE TRUNCATE ON l1_counterparty
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- The company itself as a counterparty ("own"): transfers between its accounts
-- and documents issued by it point here. Creates the row if missing, fills the
-- NIP when it becomes known and keeps the IBANs of all company accounts.
CREATE FUNCTION kasa_own_counterparty(p_company_id uuid, p_name text DEFAULT NULL, p_nip text DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
    v_nip      text := kasa_normalize_nip(p_nip);
    v_own      uuid;
    v_own_nip  text;
    v_other    uuid;
    v_ibans    text[];
BEGIN
    IF nullif(btrim(coalesce(p_nip, '')), '') IS NOT NULL AND NOT kasa_nip_valid(v_nip) THEN
        RAISE EXCEPTION 'Niepoprawny NIP firmy: %', p_nip USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kasa_counterparty:' || p_company_id::text, 0));

    SELECT coalesce(array_agg(DISTINCT iban ORDER BY iban), '{}') INTO v_ibans
      FROM l1_bank_account WHERE company_id = p_company_id;
    SELECT id, nip INTO v_own, v_own_nip
      FROM l1_counterparty WHERE company_id = p_company_id AND kind = 'own' AND superseded_by IS NULL;
    IF v_nip IS NOT NULL THEN
        SELECT id INTO v_other FROM l1_counterparty
         WHERE company_id = p_company_id AND nip = v_nip AND superseded_by IS NULL AND kind <> 'own';
    END IF;

    IF v_own IS NULL THEN
        IF v_other IS NOT NULL THEN
            -- A row created before we knew our NIP carries it: that row is us.
            UPDATE l1_counterparty SET kind = 'own' WHERE id = v_other;
            v_own := v_other;
        ELSE
            INSERT INTO l1_counterparty (company_id, nip, name, name_key, ibans, kind, source, method, confidence,
                                         evidence, created_by)
            SELECT p_company_id, v_nip, coalesce(nullif(btrim(p_name), ''), c.name),
                   kasa_name_key(coalesce(nullif(btrim(p_name), ''), c.name)), v_ibans, 'own', 'company', 'code', 1,
                   jsonb_build_object('reason', 'company itself'), 'kasa'
              FROM company c WHERE c.id = p_company_id
            RETURNING id INTO v_own;
        END IF;
    ELSIF v_nip IS NOT NULL THEN
        IF v_own_nip IS NULL THEN
            IF v_other IS NOT NULL THEN
                UPDATE l1_counterparty SET superseded_by = v_own WHERE id = v_other;
            END IF;
            UPDATE l1_counterparty SET nip = v_nip WHERE id = v_own;
        ELSIF v_own_nip <> v_nip THEN
            RAISE EXCEPTION 'Firma ma już własny NIP %, podano %', v_own_nip, v_nip USING ERRCODE = '22023';
        END IF;
    END IF;

    UPDATE l1_counterparty c
       SET ibans = (SELECT array_agg(DISTINCT x ORDER BY x) FROM unnest(c.ibans || v_ibans) AS x)
     WHERE c.id = v_own AND NOT c.ibans @> v_ibans;
    RETURN v_own;
END;
$$;

-- THE way to find or create a counterparty (bank ingestion and document
-- ingestion both call it). Lookup order: NIP -> foreign tax id -> IBAN (our own
-- account = the own counterparty) -> name (only for parties known by name
-- alone). An existing row is only enriched: missing NIP/name filled, new IBAN
-- appended. Returns the counterparty id (NULL when nothing identifies a party).
CREATE FUNCTION kasa_upsert_counterparty(
    p_company_id      uuid,
    p_nip             text,
    p_name            text,
    p_iban            text,
    p_kind            counterparty_kind DEFAULT NULL,
    p_foreign_tax_id  text DEFAULT NULL,
    p_source          text DEFAULT 'code',
    p_method          provenance_method DEFAULT 'code',
    p_confidence      double precision DEFAULT NULL,
    p_evidence        jsonb DEFAULT '{}'::jsonb,
    p_created_by      text DEFAULT 'kasa'
) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
    v_raw_tax  text := nullif(btrim(coalesce(p_nip, '')), '');
    v_nip      text := kasa_normalize_nip(p_nip);
    v_ftid     text := nullif(upper(regexp_replace(coalesce(p_foreign_tax_id, ''), '\s', '', 'g')), '');
    v_iban     text := kasa_normalize_iban(p_iban);
    v_name     text := nullif(btrim(coalesce(p_name, '')), '');
    v_key      text := kasa_name_key(p_name);
    v_id       uuid;
BEGIN
    IF p_company_id IS NULL THEN
        RAISE EXCEPTION 'company_id jest wymagane' USING ERRCODE = '22023';
    END IF;
    IF v_raw_tax IS NOT NULL AND v_nip IS NULL THEN
        -- not a Polish NIP: keep it as a foreign tax id (VAT-UE, USt-IdNr, ...)
        v_ftid := coalesce(v_ftid, upper(regexp_replace(v_raw_tax, '\s', '', 'g')));
    ELSIF v_nip IS NOT NULL AND NOT kasa_nip_valid(v_nip) THEN
        RAISE EXCEPTION 'Niepoprawny NIP (suma kontrolna): %', p_nip USING ERRCODE = '22023';
    END IF;
    IF p_kind = 'own' THEN
        RETURN kasa_own_counterparty(p_company_id, v_name, v_nip);
    END IF;
    IF v_nip IS NULL AND v_ftid IS NULL AND v_iban IS NULL AND v_name IS NULL THEN
        RETURN NULL;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('kasa_counterparty:' || p_company_id::text, 0));

    IF v_nip IS NOT NULL THEN
        SELECT id INTO v_id FROM l1_counterparty
         WHERE company_id = p_company_id AND nip = v_nip AND superseded_by IS NULL;
    END IF;
    IF v_id IS NULL AND v_ftid IS NOT NULL THEN
        SELECT id INTO v_id FROM l1_counterparty
         WHERE company_id = p_company_id AND foreign_tax_id = v_ftid AND superseded_by IS NULL
         ORDER BY created_at, id LIMIT 1;
    END IF;
    IF v_iban IS NOT NULL AND EXISTS (SELECT 1 FROM l1_bank_account WHERE company_id = p_company_id AND iban = v_iban) THEN
        -- Our own account is never another party's IBAN.
        IF v_id IS NULL AND v_nip IS NULL AND v_ftid IS NULL THEN
            RETURN kasa_own_counterparty(p_company_id, v_name, NULL);
        END IF;
        v_iban := NULL;
    END IF;
    IF v_id IS NULL AND v_iban IS NOT NULL THEN
        SELECT id INTO v_id FROM l1_counterparty
         WHERE company_id = p_company_id AND superseded_by IS NULL AND ibans @> ARRAY[v_iban]
           AND (v_nip IS NULL OR nip IS NULL)
           AND (v_ftid IS NULL OR foreign_tax_id IS NULL OR foreign_tax_id = v_ftid)
         ORDER BY (nip IS NOT NULL) DESC, created_at, id LIMIT 1;
    END IF;
    IF v_id IS NULL AND v_nip IS NULL AND v_ftid IS NULL AND v_iban IS NULL AND v_key IS NOT NULL THEN
        SELECT id INTO v_id FROM l1_counterparty
         WHERE company_id = p_company_id AND superseded_by IS NULL AND name_key = v_key
           AND nip IS NULL AND foreign_tax_id IS NULL
         ORDER BY created_at, id LIMIT 1;
    END IF;

    IF v_id IS NULL THEN
        INSERT INTO l1_counterparty (company_id, nip, foreign_tax_id, name, name_key, ibans, kind,
                                     source, method, confidence, evidence, created_by)
        VALUES (p_company_id, v_nip, v_ftid, v_name, v_key,
                CASE WHEN v_iban IS NULL THEN '{}'::text[] ELSE ARRAY[v_iban] END,
                coalesce(p_kind, 'company'), coalesce(nullif(btrim(p_source), ''), 'code'), p_method, p_confidence,
                coalesce(p_evidence, '{}'::jsonb), coalesce(nullif(btrim(p_created_by), ''), 'kasa'))
        RETURNING id INTO v_id;
        RETURN v_id;
    END IF;

    UPDATE l1_counterparty c
       SET nip = coalesce(c.nip, v_nip),
           foreign_tax_id = coalesce(c.foreign_tax_id, v_ftid),
           name = coalesce(c.name, v_name),
           name_key = coalesce(c.name_key, v_key),
           ibans = CASE WHEN v_iban IS NULL OR c.ibans @> ARRAY[v_iban] THEN c.ibans ELSE c.ibans || v_iban END
     WHERE c.id = v_id
       AND ((c.nip IS NULL AND v_nip IS NOT NULL)
            OR (c.foreign_tax_id IS NULL AND v_ftid IS NOT NULL)
            OR (c.name IS NULL AND v_name IS NOT NULL)
            OR (v_iban IS NOT NULL AND NOT c.ibans @> ARRAY[v_iban]));
    RETURN v_id;
END;
$$;

-- ===========================================================================
-- Bank accounts and transactions: the wide record (briefing, step 2)
-- ===========================================================================

ALTER TABLE l1_bank_account
    ADD COLUMN provider     text,           -- 'enablebanking', 'camt053', 'mt940', ...
    ADD COLUMN external_id  text,           -- account id at the provider (EnableBanking account uid)
    ADD COLUMN bic          text,
    ADD COLUMN owner_name   text;

-- Operation types present in bank data beyond the phase 1 list.
ALTER TYPE bank_op_type ADD VALUE IF NOT EXISTS 'direct_debit';
ALTER TYPE bank_op_type ADD VALUE IF NOT EXISTS 'foreign_transfer';
ALTER TYPE bank_op_type ADD VALUE IF NOT EXISTS 'interest';

ALTER TABLE l1_bank_transaction
    ADD COLUMN transaction_date             date,   -- card/BLIK: day of the operation (pending rows have only this)
    ADD COLUMN bank_tx_code                 text,   -- ISO 20022 domain/family/sub-family, e.g. PMNT/ICDT/DMCT
    ADD COLUMN bank_tx_description          text,   -- bank's own description of the operation type
    ADD COLUMN merchant_category_code       text,
    ADD COLUMN reference_number             text,
    ADD COLUMN counterparty_bic             text,
    ADD COLUMN balance_after_computed       boolean NOT NULL DEFAULT false,  -- true: computed by us, not given by the bank
    ADD COLUMN counterparty_id              uuid,
    ADD COLUMN title_pseudonymized          text,   -- the only form of the title a model may see
    ADD COLUMN counterparty_pseudonym       text,   -- counterparty name with persons replaced by pseudonyms
    ADD COLUMN counterparty_iban_pseudonym  text,
    ADD COLUMN sensitive                    boolean NOT NULL DEFAULT false,  -- sensitive word: bypasses the model
    ADD COLUMN mpp_group_id                 uuid,   -- legs of one split payment share it (set by code)
    ADD COLUMN mpp_role                     text,
    ADD COLUMN raw_id                       uuid,   -- L0 payload this row was parsed from
    ADD COLUMN raw_locator                  text,   -- position inside the payload
    ADD CONSTRAINT l1_bank_transaction_counterparty_fk FOREIGN KEY (counterparty_id, company_id)
        REFERENCES l1_counterparty (id, company_id),
    ADD CONSTRAINT l1_bank_transaction_raw_fk FOREIGN KEY (raw_id, company_id)
        REFERENCES l0_raw (id, company_id),
    ADD CONSTRAINT l1_bank_transaction_mpp_role_check
        CHECK (mpp_role IS NULL OR mpp_role IN ('main', 'vat_leg', 'vat_rebooking')),
    ADD CONSTRAINT l1_bank_transaction_mpp_pair_check CHECK ((mpp_group_id IS NULL) = (mpp_role IS NULL)),
    ADD CONSTRAINT l1_bank_transaction_balance_computed_check
        CHECK (NOT balance_after_computed OR balance_after_minor IS NOT NULL),
    ADD CONSTRAINT l1_bank_transaction_some_date_check
        CHECK (booking_date IS NOT NULL OR transaction_date IS NOT NULL OR value_date IS NOT NULL);

CREATE INDEX l1_bank_transaction_counterparty_idx ON l1_bank_transaction (counterparty_id)
    WHERE counterparty_id IS NOT NULL;
CREATE INDEX l1_bank_transaction_mpp_group_idx ON l1_bank_transaction (mpp_group_id)
    WHERE mpp_group_id IS NOT NULL;
CREATE INDEX l1_bank_transaction_raw_idx ON l1_bank_transaction (raw_id) WHERE raw_id IS NOT NULL;

-- Balances as reported by the bank (EnableBanking /balances, camt.053 Bal,
-- MT940 :60F:/:62F:): OPBD = at the start of reference_date, CLBD = at its end,
-- ITAV/CLAV = available. The truth check "balance continuity" compares them
-- with the booked transactions.
CREATE TABLE l1_bank_balance (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id                  uuid NOT NULL REFERENCES company (id),
    account_id                  uuid NOT NULL,
    balance_type                text NOT NULL CHECK (balance_type ~ '^[A-Z]{4}$'),
    reference_date              date NOT NULL,
    amount_minor                bigint NOT NULL,
    currency                    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    name                        text,
    last_change_at              timestamptz,
    last_committed_transaction  text,
    raw_id                      uuid,
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
    CONSTRAINT l1_bank_balance_model_prompt_check CHECK (method <> 'model' OR prompt_version IS NOT NULL),

    CONSTRAINT l1_bank_balance_id_company_key UNIQUE (id, company_id),
    CONSTRAINT l1_bank_balance_account_fk FOREIGN KEY (account_id, company_id)
        REFERENCES l1_bank_account (id, company_id),
    CONSTRAINT l1_bank_balance_raw_fk FOREIGN KEY (raw_id, company_id) REFERENCES l0_raw (id, company_id),
    CONSTRAINT l1_bank_balance_superseded_fk FOREIGN KEY (superseded_by, company_id)
        REFERENCES l1_bank_balance (id, company_id),
    CONSTRAINT l1_bank_balance_not_self_superseded CHECK (superseded_by IS DISTINCT FROM id),
    CONSTRAINT l1_bank_balance_current_excl
        EXCLUDE USING btree (account_id WITH =, balance_type WITH =, reference_date WITH =, source WITH =)
        WHERE (superseded_by IS NULL)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX l1_bank_balance_account_idx ON l1_bank_balance (account_id, reference_date);

CREATE TRIGGER l1_bank_balance_no_delete
    BEFORE DELETE ON l1_bank_balance
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_delete();
CREATE TRIGGER l1_bank_balance_no_truncate
    BEFORE TRUNCATE ON l1_bank_balance
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();

-- ===========================================================================
-- Documents: counterparty and KSeF number format (review point 14)
-- ===========================================================================

-- counterparty_id of a document = the other party (buyer of a sale, seller of
-- a purchase), filled by document ingestion through kasa_upsert_counterparty().
ALTER TABLE l1_document
    ADD COLUMN counterparty_id uuid,
    ADD CONSTRAINT l1_document_counterparty_fk FOREIGN KEY (counterparty_id, company_id)
        REFERENCES l1_counterparty (id, company_id),
    ADD CONSTRAINT l1_document_ksef_number_format
        CHECK (ksef_number ~ '^[0-9]{10}-[0-9]{8}-[0-9A-F]{12}-[0-9A-F]{2}$');

CREATE INDEX l1_document_counterparty_idx ON l1_document (counterparty_id) WHERE counterparty_id IS NOT NULL;

-- ===========================================================================
-- Truth checks (briefing: "Kontrola prawdy"), one row per run, append-only.
-- ===========================================================================

CREATE TABLE truth_check_result (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    uuid NOT NULL REFERENCES company (id),
    check_name    text NOT NULL CHECK (check_name ~ '^[a-z][a-z0-9_]*$'),
    scope         text,                 -- e.g. the account IBAN; NULL = the whole company
    period_start  date NOT NULL,
    period_end    date NOT NULL,
    ok            boolean NOT NULL,
    detail        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),
    run_at        timestamptz NOT NULL DEFAULT now(),
    run_by        text NOT NULL DEFAULT current_user,
    CONSTRAINT truth_check_result_period_check CHECK (period_end >= period_start)
);

CREATE INDEX truth_check_result_lookup_idx
    ON truth_check_result (company_id, check_name, period_start, period_end, run_at DESC);

CREATE TRIGGER truth_check_result_append_only
    BEFORE UPDATE OR DELETE ON truth_check_result
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();
CREATE TRIGGER truth_check_result_no_truncate
    BEFORE TRUNCATE ON truth_check_result
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
