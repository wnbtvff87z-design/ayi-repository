ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS affected_items jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS evidence_matrix jsonb NOT NULL DEFAULT '[]'::jsonb;
