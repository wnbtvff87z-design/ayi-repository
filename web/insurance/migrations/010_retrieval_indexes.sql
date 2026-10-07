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
