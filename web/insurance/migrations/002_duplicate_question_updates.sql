ALTER TABLE insurance_case_questions
    ADD COLUMN IF NOT EXISTS updates jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(updates) = 'array');
