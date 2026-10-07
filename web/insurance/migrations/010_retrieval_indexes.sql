-- Measured on 10,000 synthetic policies: replace authorization/document sequential
-- scans. Exact case-insensitive hints need scoped expression indexes; version/page
-- primary keys already cover their joins.
CREATE INDEX IF NOT EXISTS insurance_authorizations_active_retrieval_idx
    ON insurance_authorizations (business_id, customer_id, policy_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS insurance_documents_version_retrieval_idx
    ON insurance_documents (business_id, policy_id, version_id);

CREATE INDEX IF NOT EXISTS insurance_policies_hint_id_retrieval_idx
    ON insurance_policies (business_id, customer_id, lower(policy_id));

CREATE INDEX IF NOT EXISTS insurance_policies_hint_contract_retrieval_idx
    ON insurance_policies (business_id, customer_id, lower(contract_number));

-- Actual verified_customer SQL on 10,000 verifications otherwise scans the PK
-- backwards and discards 9,999 unrelated scopes before finding the latest valid row.
CREATE INDEX IF NOT EXISTS insurance_identity_verifications_scope_idx
    ON insurance_identity_verifications
        (business_id, channel, conversation_ref, session_ref, verification_id DESC)
    WHERE revoked_at IS NULL;
