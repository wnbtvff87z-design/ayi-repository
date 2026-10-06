-- Registration is now a brief PG-only "pending" record; the document worker verifies the
-- object (existence, type, size, SHA-256) outside Web. `sha256` is the EXPECTED hash.
ALTER TABLE insurance_documents DROP CONSTRAINT IF EXISTS insurance_documents_status_check;
ALTER TABLE insurance_documents DROP CONSTRAINT IF EXISTS insurance_documents_size_bytes_check;
ALTER TABLE insurance_documents ALTER COLUMN size_bytes DROP NOT NULL;
ALTER TABLE insurance_documents ALTER COLUMN content_type DROP NOT NULL;
ALTER TABLE insurance_documents ALTER COLUMN status SET DEFAULT 'pending_verification';
UPDATE insurance_documents SET status='pending_verification' WHERE status='registered';
UPDATE insurance_documents SET status='verifying' WHERE status='processing';
ALTER TABLE insurance_documents ADD CONSTRAINT insurance_documents_status_check CHECK (status IN (
    'pending_verification','verifying','ready','needs_review','failed',
    'object_missing','hash_mismatch','invalid_object'));
ALTER TABLE insurance_documents ADD CONSTRAINT insurance_documents_size_bytes_check
    CHECK (size_bytes IS NULL OR size_bytes > 0);
ALTER TABLE insurance_documents ADD COLUMN IF NOT EXISTS verified_at timestamptz;
