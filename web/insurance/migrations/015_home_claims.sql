CREATE SEQUENCE IF NOT EXISTS insurance_claim_ref_seq START WITH 1;

CREATE TABLE IF NOT EXISTS insurance_professionals (
    professional_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL,
    service_type text NOT NULL CHECK (service_type IN ('fontanería', 'cristalería', 'mobiliario', 'pintura', 'asistencia')),
    name text NOT NULL,
    email text NOT NULL,
    phone text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, service_type)
);

CREATE TABLE IF NOT EXISTS insurance_claims (
    claim_uuid uuid PRIMARY KEY,
    business_id text NOT NULL,
    customer_id text NOT NULL,
    policy_id text NOT NULL,
    policy_version_id text,
    claim_ref text UNIQUE NOT NULL,
    channel text NOT NULL CHECK (channel IN ('Voice', 'WhatsApp')),
    original_description text,
    structured_interpretation jsonb NOT NULL DEFAULT '{}'::jsonb,
    coverage_evaluation jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'INFORMATION_GATHERING' CHECK (state IN (
        'INFORMATION_GATHERING', 'INCIDENT_INTERPRETED', 'COVERAGE_CHECK', 'PHOTOS_REQUESTED',
        'PHOTOS_RECEIVED', 'READY_FOR_HUMAN_REVIEW', 'HUMAN_REVIEW', 'APPROVED', 'REJECTED', 'CLOSED',
        'INVOICE_REQUESTED', 'INVOICE_RECEIVED', 'ADDITIONAL_DOCUMENTS_REQUIRED', 'MORE_INFO_REQUIRED'
    )),
    photos jsonb NOT NULL DEFAULT '[]'::jsonb,
    invoices jsonb NOT NULL DEFAULT '[]'::jsonb,
    service_notified boolean NOT NULL DEFAULT false,
    service_notification_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id),
    FOREIGN KEY (business_id, policy_id) REFERENCES insurance_policies (business_id, policy_id)
);

CREATE INDEX IF NOT EXISTS insurance_claims_ref_idx ON insurance_claims (claim_ref);
CREATE INDEX IF NOT EXISTS insurance_claims_customer_idx ON insurance_claims (business_id, customer_id);
