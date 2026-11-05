-- 0002_l0_raw: shelf L0, raw payloads exactly as fetched. INSERT only.

CREATE TYPE raw_channel AS ENUM ('bank', 'ksef', 'file', 'pdf', 'email');

CREATE TABLE l0_raw (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id    uuid NOT NULL REFERENCES company (id),
    channel       raw_channel NOT NULL,
    external_ref  text,                 -- KSeF number, bank batch id, Message-ID, file name
    sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type  text NOT NULL CHECK (btrim(content_type) <> ''),
    payload       bytea NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    meta          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(meta) = 'object'),

    -- The database recomputes the digest, so a stored hash can never lie.
    CONSTRAINT l0_raw_sha256_matches_payload CHECK (sha256 = encode(sha256(payload), 'hex')),
    -- Repeating a fetch never creates a duplicate.
    CONSTRAINT l0_raw_company_sha256_key UNIQUE (company_id, sha256),
    CONSTRAINT l0_raw_id_company_key UNIQUE (id, company_id)
);

CREATE INDEX l0_raw_company_channel_idx ON l0_raw (company_id, channel, fetched_at);
CREATE INDEX l0_raw_external_ref_idx ON l0_raw (company_id, external_ref) WHERE external_ref IS NOT NULL;

CREATE TRIGGER l0_raw_append_only
    BEFORE UPDATE OR DELETE ON l0_raw
    FOR EACH ROW EXECUTE FUNCTION kasa_forbid_change();

CREATE TRIGGER l0_raw_no_truncate
    BEFORE TRUNCATE ON l0_raw
    FOR EACH STATEMENT EXECUTE FUNCTION kasa_forbid_truncate();
