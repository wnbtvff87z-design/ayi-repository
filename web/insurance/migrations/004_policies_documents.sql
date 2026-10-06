CREATE TABLE IF NOT EXISTS insurance_customers (
    business_id text NOT NULL,
    customer_id text NOT NULL,
    display_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, customer_id)
);

CREATE TABLE IF NOT EXISTS insurance_policies (
    business_id text NOT NULL,
    policy_id text NOT NULL,
    customer_id text NOT NULL,
    product text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, policy_id),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id)
);

CREATE TABLE IF NOT EXISTS insurance_policy_versions (
    business_id text NOT NULL,
    policy_id text NOT NULL,
    version_id text NOT NULL,
    valid_from date NOT NULL,
    valid_to date,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, policy_id, version_id),
    FOREIGN KEY (business_id, policy_id) REFERENCES insurance_policies (business_id, policy_id),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS insurance_documents (
    document_id text NOT NULL,
    business_id text NOT NULL,
    policy_id text NOT NULL,
    version_id text NOT NULL,
    object_key text NOT NULL UNIQUE,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    content_type text NOT NULL,
    status text NOT NULL DEFAULT 'registered'
        CHECK (status IN ('registered','processing','ready','needs_review','failed')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    locked_until timestamptz,
    registered_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    PRIMARY KEY (business_id, document_id),
    FOREIGN KEY (business_id, policy_id, version_id)
        REFERENCES insurance_policy_versions (business_id, policy_id, version_id)
);

CREATE TABLE IF NOT EXISTS insurance_document_pages (
    business_id text NOT NULL,
    document_id text NOT NULL,
    page_number integer NOT NULL CHECK (page_number > 0),
    section text NOT NULL DEFAULT 'general'
        CHECK (section IN ('particular','general','annex','exclusions','general_conditions','coverage')),
    source text NOT NULL CHECK (source IN ('text','ocr','none')),
    quality text NOT NULL CHECK (quality IN ('ok','empty','illegible','failed')),
    body text NOT NULL DEFAULT '',
    indexed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (business_id, document_id, page_number),
    FOREIGN KEY (business_id, document_id) REFERENCES insurance_documents (business_id, document_id)
);

CREATE TABLE IF NOT EXISTS insurance_authorizations (
    authorization_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL,
    customer_id text NOT NULL,
    policy_id text NOT NULL,
    granted_by text NOT NULL,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    revoked_at timestamptz,
    FOREIGN KEY (business_id, policy_id) REFERENCES insurance_policies (business_id, policy_id),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id)
);

-- Written only by an external verifier or an authenticated admin; never by the
-- customer-facing dialogue. Keyed by an HMAC of the conversation, not the phone.
CREATE TABLE IF NOT EXISTS insurance_identity_verifications (
    verification_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL,
    conversation_ref text NOT NULL,
    customer_id text NOT NULL,
    method text NOT NULL,
    verified_by text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (business_id, customer_id) REFERENCES insurance_customers (business_id, customer_id)
);

CREATE TABLE IF NOT EXISTS insurance_admin_users (
    actor_id text PRIMARY KEY,
    business_id text NOT NULL,
    token_hmac text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS insurance_audit_log (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id text NOT NULL,
    business_id text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    outcome text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
