-- Identity = name + surnames + DNI/NIE matching exactly one active customer of the resolved business.
ALTER TABLE insurance_customers ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
-- Duplicates must be detectable (-> identity_ambiguous), so the DNI index is no longer unique.
DROP INDEX IF EXISTS insurance_customers_document_idx;
CREATE INDEX IF NOT EXISTS insurance_customers_document_idx
    ON insurance_customers (business_id, document_hmac) WHERE document_hmac IS NOT NULL;

-- A verification is bound to business, channel and session (Voice: CallSid; WhatsApp: '').
-- No DNI, name or phone is stored here: conversation_ref is an HMAC.
ALTER TABLE insurance_identity_verifications ADD COLUMN IF NOT EXISTS channel text;
ALTER TABLE insurance_identity_verifications ADD COLUMN IF NOT EXISTS session_ref text NOT NULL DEFAULT '';

-- Failed attempts only (values entered are never stored).
CREATE TABLE IF NOT EXISTS insurance_identity_attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL,
    channel text NOT NULL,
    conversation_ref text NOT NULL,
    session_ref text NOT NULL DEFAULT '',
    outcome text NOT NULL CHECK (outcome IN ('no_match','ambiguous')),
    attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS insurance_identity_attempts_idx
    ON insurance_identity_attempts (business_id, channel, conversation_ref, attempted_at);

-- Short-lived dialogue state so the caller need not repeat the original question.
-- Holds the pending question, declared name, DNI as HMAC + masked tail, contract number as text.
CREATE TABLE IF NOT EXISTS insurance_conversation_state (
    business_id text NOT NULL,
    channel text NOT NULL,
    conversation_ref text NOT NULL,
    session_ref text NOT NULL DEFAULT '',
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, channel, conversation_ref, session_ref)
);
