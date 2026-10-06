ALTER TABLE insurance_case_questions
    ADD COLUMN IF NOT EXISTS updates jsonb NOT NULL DEFAULT '[]'::jsonb;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'insurance_case_questions_updates_array_check'
          AND conrelid = 'insurance_case_questions'::regclass
    ) THEN
        ALTER TABLE insurance_case_questions
            ADD CONSTRAINT insurance_case_questions_updates_array_check
            CHECK (jsonb_typeof(updates) = 'array');
    END IF;
END
$migration$;
