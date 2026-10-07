-- Tiered conversational memory (additive). PostgreSQL is the source of truth.
-- Partial-name identity: HMACs of every name prefix with >=2 words ("celia zorro", "celia zorro condes").
-- Customers provisioned before this migration keep matching by full-name HMAC until re-provisioned.
ALTER TABLE insurance_customers ADD COLUMN IF NOT EXISTS name_prefix_hmacs text[] NOT NULL DEFAULT '{}';

-- Tier A: every Insurance turn. Identity data is never stored here (content is redacted/empty for it).
CREATE TABLE IF NOT EXISTS insurance_conversation_turns (
    turn_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL,
    channel text NOT NULL,
    conversation_ref text NOT NULL,
    session_ref text NOT NULL DEFAULT '',
    customer_id text,
    role text NOT NULL CHECK (role IN ('user','assistant')),
    kind text NOT NULL CHECK (kind IN ('question','answer','clarification','confirmation','identity','other')),
    external_id text NOT NULL,
    reply_to bigint,
    content text NOT NULL DEFAULT '',
    normalized text,
    policy_id text,
    version_id text,
    decision text,
    pages jsonb NOT NULL DEFAULT '[]'::jsonb,
    correlation_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- webhook retries reuse external_id: the second insert is a no-op
    UNIQUE (business_id, channel, conversation_ref, session_ref, external_id, role),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id),
    FOREIGN KEY (business_id, policy_id, version_id)
        REFERENCES insurance_policy_versions (business_id, policy_id, version_id)
);
CREATE INDEX IF NOT EXISTS insurance_turns_scope_idx
    ON insurance_conversation_turns (business_id, channel, conversation_ref, customer_id, turn_id DESC);
CREATE INDEX IF NOT EXISTS insurance_turns_created_idx ON insurance_conversation_turns (created_at);

-- Tier C: incremental structured summary, one per (business, channel, conversation, customer).
CREATE TABLE IF NOT EXISTS insurance_conversation_summary (
    business_id text NOT NULL,
    channel text NOT NULL,
    conversation_ref text NOT NULL,
    customer_id text NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_turn_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, channel, conversation_ref, customer_id),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id)
);
