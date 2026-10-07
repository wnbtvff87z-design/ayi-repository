-- EXPLAIN ANALYZE on 12,000 synthetic policies / 48,000 documents showed
-- authorization and document sequential scans (11,999 / 47,999 discarded rows).
-- Composite PKs already cover policy/version/page lookups; these FK-side scopes did not.
CREATE INDEX IF NOT EXISTS insurance_authorizations_scope_idx
    ON insurance_authorizations (business_id, customer_id, policy_id)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS insurance_documents_scope_idx
    ON insurance_documents (business_id, policy_id, version_id, status, document_id);
