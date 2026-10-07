-- Accent-folded PostgreSQL full-text candidates are combined with normalized
-- token scoring after the policy and document authorization scope is selected.
CREATE INDEX IF NOT EXISTS insurance_document_pages_fts_idx
    ON insurance_document_pages
    USING GIN (to_tsvector('simple', translate(lower(body), 'áéíóúüñ', 'aeiouun')))
    WHERE indexed AND quality='ok' AND source IN ('text','ocr') AND length(btrim(body))>0;
