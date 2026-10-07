-- Voice traces are masked at ingestion; no CallSid, phone or webhook identifier is stored.
ALTER TABLE insurance_admin_users ADD COLUMN IF NOT EXISTS can_read_voice boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS insurance_voice_trace (
    business_id text NOT NULL,
    call_ref text NOT NULL CHECK (call_ref ~ '^[0-9a-f]{64}$'),
    turn_no bigint NOT NULL CHECK (turn_no > 0),
    webhook_ref text NOT NULL CHECK (webhook_ref ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    recognized text NOT NULL,
    normalized text NOT NULL,
    stage text NOT NULL,
    diagnostic text,
    reply text NOT NULL,
    customer_id text,
    policy_id text,
    version_id text,
    pages jsonb NOT NULL DEFAULT '[]'::jsonb,
    transport jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (business_id, call_ref, turn_no),
    UNIQUE (business_id, call_ref, webhook_ref),
    FOREIGN KEY (business_id, customer_id)
        REFERENCES insurance_customers (business_id, customer_id) ON DELETE CASCADE,
    FOREIGN KEY (business_id, policy_id, version_id)
        REFERENCES insurance_policy_versions (business_id, policy_id, version_id) ON DELETE CASCADE,
    CHECK (customer_id IS NOT NULL OR (policy_id IS NULL AND version_id IS NULL AND pages = '[]'::jsonb))
);
