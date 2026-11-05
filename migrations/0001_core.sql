-- 0001_core: shared types, company registry and generic guard functions.
--
-- Error codes raised by kasa guards (class "KS", outside the SQL standard range):
--   KS001  append-only table: UPDATE/DELETE forbidden (L0, decision log)
--   KS002  DELETE/TRUNCATE forbidden (L1-L3: nothing is ever deleted)
--   KS003  a human decision may be changed only by a human
--   KS004  automation (method = 'rule') without a rule in stage 'auto'
--   KS005  polymorphic subject does not exist in the same company
--   KS006  company_id of an existing row cannot change
--   KS007  allocation target is not an approved cost object

-- Provenance method carried by every L1-L3 row.
CREATE TYPE provenance_method AS ENUM ('code', 'rule', 'model', 'human');

-- Polymorphic subject of suggestions, allocations, categories and decisions.
CREATE TYPE subject_type AS ENUM (
    'transaction',      -- l1_bank_transaction
    'document',         -- l1_document
    'line',             -- l1_document_line
    'internal_record',  -- l1_internal_record
    'cost_object',      -- l3_cost_object
    'rule'              -- l3_rule
);

-- One row per tenant. The extension (kasa/extensions/<slug>/) defines what a
-- cost object is for this company, its categories and import mappings.
CREATE TABLE company (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9_]*$'),
    name            text NOT NULL CHECK (btrim(name) <> ''),
    extension_slug  text NOT NULL CHECK (extension_slug ~ '^[a-z][a-z0-9_]*$'),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Append-only guard: any UPDATE or DELETE raises.
CREATE FUNCTION kasa_forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Tabela % przyjmuje tylko INSERT; operacja % jest zabroniona', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'KS001',
              HINT = 'Surowe dane i dziennik decyzji tylko dopisujemy. Korekta = nowy wiersz.';
END;
$$;

-- "Nothing is deleted": row-level DELETE guard for L1-L2.
CREATE FUNCTION kasa_forbid_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Usuwanie wierszy z % jest zabronione', TG_TABLE_NAME
        USING ERRCODE = 'KS002',
              HINT = 'Nic nie kasujemy: nowa wersja faktu dostaje superseded_by, sugestia zmienia status.';
END;
$$;

-- Statement-level TRUNCATE guard for every shelf.
CREATE FUNCTION kasa_forbid_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE tabeli % jest zabronione', TG_TABLE_NAME
        USING ERRCODE = 'KS002';
END;
$$;
