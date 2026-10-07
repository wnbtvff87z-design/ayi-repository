-- Voice summaries must not cross CallSid boundaries. Existing Voice summaries cannot be
-- attributed to one call reliably; preserve them under an unreachable legacy namespace.
ALTER TABLE insurance_conversation_summary ADD COLUMN IF NOT EXISTS session_ref text NOT NULL DEFAULT '';
UPDATE insurance_conversation_summary SET session_ref='legacy-unscoped-summary'
    WHERE channel='Voice' AND session_ref='';
ALTER TABLE insurance_conversation_summary DROP CONSTRAINT IF EXISTS insurance_conversation_summary_pkey;
ALTER TABLE insurance_conversation_summary ADD PRIMARY KEY
    (business_id,channel,conversation_ref,session_ref,customer_id);
-- A reply may reference only a turn from the identical customer/conversation/call.
-- NOT VALID enforces new writes without rewriting historical links. Legacy rows
-- can be audited and constraints validated separately; scoped readers exclude bad links.
-- These unique keys are required by PostgreSQL for the composite foreign keys,
-- not speculative query-performance indexes. Existing insurance_turns_scope_idx serves reads.
CREATE UNIQUE INDEX IF NOT EXISTS insurance_turns_reply_scope_idx ON insurance_conversation_turns
    (turn_id,business_id,channel,conversation_ref,session_ref,customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS insurance_turns_reply_conversation_idx ON insurance_conversation_turns
    (turn_id,business_id,channel,conversation_ref,session_ref);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='insurance_turns_reply_scope_fk'
                   AND conrelid='insurance_conversation_turns'::regclass) THEN
        ALTER TABLE insurance_conversation_turns ADD CONSTRAINT insurance_turns_reply_scope_fk
            FOREIGN KEY (reply_to,business_id,channel,conversation_ref,session_ref,customer_id)
            REFERENCES insurance_conversation_turns
                (turn_id,business_id,channel,conversation_ref,session_ref,customer_id)
                ON DELETE CASCADE NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='insurance_turns_reply_conversation_fk'
                   AND conrelid='insurance_conversation_turns'::regclass) THEN
        ALTER TABLE insurance_conversation_turns ADD CONSTRAINT insurance_turns_reply_conversation_fk
            FOREIGN KEY (reply_to,business_id,channel,conversation_ref,session_ref)
            REFERENCES insurance_conversation_turns
                (turn_id,business_id,channel,conversation_ref,session_ref)
                ON DELETE CASCADE NOT VALID;
    END IF;
END $$;

-- MATCH SIMPLE skips the customer FK when customer_id is NULL. Check this case
-- explicitly so an unverified reply cannot attach to a verified customer's turn.
CREATE OR REPLACE FUNCTION insurance_check_reply_customer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM insurance_conversation_turns a
        LEFT JOIN insurance_conversation_turns q ON q.turn_id=a.reply_to
        AND q.business_id=a.business_id AND q.channel=a.channel
        AND q.conversation_ref=a.conversation_ref AND q.session_ref=a.session_ref
        AND q.customer_id IS NOT DISTINCT FROM a.customer_id
        WHERE a.turn_id=NEW.turn_id AND a.reply_to IS NOT NULL AND q.turn_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Reply customer scope mismatch' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS insurance_reply_customer_scope ON insurance_conversation_turns;
CREATE CONSTRAINT TRIGGER insurance_reply_customer_scope
    AFTER INSERT OR UPDATE ON insurance_conversation_turns
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION insurance_check_reply_customer();
