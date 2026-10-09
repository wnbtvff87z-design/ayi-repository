-- Source locators contain only opaque record identifiers, never source field values.
CREATE TABLE IF NOT EXISTS insurance_master_sources (
    business_id text PRIMARY KEY,
    source_id text NOT NULL,
    base_id text NOT NULL,
    tables jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS insurance_master_record_map (
    business_id text NOT NULL,
    source_id text NOT NULL,
    entity text NOT NULL CHECK (entity IN ('customers','policies','versions','documents')),
    table_id text NOT NULL,
    record_id text NOT NULL,
    internal_id text NOT NULL,
    parent_id text,
    PRIMARY KEY (business_id, source_id, entity, record_id),
    UNIQUE (business_id, entity, internal_id)
);
CREATE TABLE IF NOT EXISTS insurance_hmac_keys (
    business_id text PRIMARY KEY,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS insurance_master_map_lookup
    ON insurance_master_record_map (business_id, entity, internal_id);
