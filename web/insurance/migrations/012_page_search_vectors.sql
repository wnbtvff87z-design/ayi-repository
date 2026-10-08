-- Stored full-text vector for page retrieval (same expression the query used to compute on
-- every request) with a GIN index, so matching pages no longer re-tokenize every body.
ALTER TABLE insurance_document_pages
    ADD COLUMN IF NOT EXISTS body_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, translate(lower(body), 'áéíóúüñ', 'aeiouun'))
    ) STORED;

CREATE INDEX IF NOT EXISTS insurance_document_pages_body_tsv_idx
    ON insurance_document_pages USING GIN (body_tsv);

-- Preparation for hybrid (lexical + semantic) retrieval. Optional: only when the pgvector
-- extension is available and the migration role may enable it. The nullable column is not
-- read by retrieval yet; without pgvector this block is a no-op.
DO $$
DECLARE
    vector_schema text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'pgvector available but not enabled: insufficient privilege';
        END;
    END IF;
    SELECT extnamespace::regnamespace::text INTO vector_schema
      FROM pg_extension WHERE extname = 'vector';
    IF vector_schema IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE insurance_document_pages ADD COLUMN IF NOT EXISTS embedding %s.vector(1536)',
            vector_schema);
    END IF;
END
$$;
