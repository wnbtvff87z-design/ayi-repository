-- Customer lookup keys (HMAC only; no plaintext DNI/NIE) and the contractual policy number.
ALTER TABLE insurance_customers ADD COLUMN IF NOT EXISTS document_hmac text;
ALTER TABLE insurance_customers ADD COLUMN IF NOT EXISTS name_hmac text;
CREATE UNIQUE INDEX IF NOT EXISTS insurance_customers_document_idx
    ON insurance_customers (business_id, document_hmac) WHERE document_hmac IS NOT NULL;

-- policy_id is the internal key used in object keys; contract_number is the contractual
-- number a caller says out loud. Text, so leading zeros survive. Never derived from document ids.
ALTER TABLE insurance_policies ADD COLUMN IF NOT EXISTS contract_number text;
CREATE UNIQUE INDEX IF NOT EXISTS insurance_policies_contract_idx
    ON insurance_policies (business_id, contract_number) WHERE contract_number IS NOT NULL;

-- Attribution of a case. customer_id is set ONLY from a verified identity.
ALTER TABLE insurance_cases ADD COLUMN IF NOT EXISTS customer_id text;
ALTER TABLE insurance_cases ADD COLUMN IF NOT EXISTS attribution_state text NOT NULL DEFAULT 'customer_unknown';
ALTER TABLE insurance_cases DROP CONSTRAINT IF EXISTS insurance_cases_attribution_state_check;
ALTER TABLE insurance_cases ADD CONSTRAINT insurance_cases_attribution_state_check CHECK (
    attribution_state IN ('verified_authorized','candidate_pending_identity',
                          'customer_unknown','policy_pending_confirmation'));
ALTER TABLE insurance_cases DROP CONSTRAINT IF EXISTS insurance_cases_customer_fk;
ALTER TABLE insurance_cases ADD CONSTRAINT insurance_cases_customer_fk
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id);
ALTER TABLE insurance_case_questions ADD COLUMN IF NOT EXISTS diagnostic_code text;

-- Unverified declaration made by the caller. Never proof of identity. DNI/NIE only as HMAC + masked tail.
CREATE TABLE IF NOT EXISTS insurance_case_claims (
    claim_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES insurance_cases(case_id),
    business_id text NOT NULL,
    channel text NOT NULL,
    external_id text NOT NULL,
    claimed_document_hmac text,
    claimed_document_tail text,
    claimed_name text,
    claimed_contract_number text,
    candidate_customer_id text,
    match_status text NOT NULL CHECK (match_status IN
        ('no_claim','candidate_found','not_found','name_mismatch')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, channel, external_id)
);

-- Per-person human access (replaces the shared key for reading case detail).
ALTER TABLE insurance_admin_users ADD COLUMN IF NOT EXISTS can_read_cases boolean NOT NULL DEFAULT false;
