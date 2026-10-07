-- EXPLAIN ANALYZE on 12,000 synthetic policies / 48,000 documents showed
-- authorization and document sequential scans (11,999 / 47,999 discarded rows).
-- Composite PKs already cover policy/version/page lookups; these FK-side scopes did not.
CREATE INDEX IF NOT EXISTS insurance_authorizations_scope_idx
    ON insurance_authorizations (business_id, customer_id, policy_id)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS insurance_documents_scope_idx
    ON insurance_documents (business_id, policy_id, version_id, status, document_id);

-- On 24,000 turns, point recall scanned 300 scoped answers without a reply-to lookup.
-- On 12,001 verifications, the latest-valid lookup discarded 12,000 rows via the PK.
-- Existing turn uniqueness and the session-summary PK already cover their complete scopes.
CREATE INDEX IF NOT EXISTS insurance_turns_reply_idx
    ON insurance_conversation_turns (reply_to)
    WHERE role = 'assistant' AND kind = 'answer';
CREATE INDEX IF NOT EXISTS insurance_verifications_scope_idx
    ON insurance_identity_verifications
        (business_id, channel, conversation_ref, session_ref, verification_id DESC)
    WHERE revoked_at IS NULL;
