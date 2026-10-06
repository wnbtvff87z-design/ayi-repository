ALTER TABLE insurance_outbox
    DROP CONSTRAINT IF EXISTS insurance_outbox_status_check;

ALTER TABLE insurance_outbox
    ADD CONSTRAINT insurance_outbox_status_check
    CHECK (status IN ('pending', 'processing', 'done', 'failed'));
