-- Independent notification status for professional + human agent (idempotent dual dispatch).

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS notification_results jsonb NOT NULL DEFAULT '{}'::jsonb;
