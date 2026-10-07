-- Session-scoped summaries are additive: preserve the legacy summary table and all retained turns.
-- Legacy Voice summaries cannot be attributed to one CallSid and are deliberately not imported.
CREATE TABLE IF NOT EXISTS insurance_session_summary (
    business_id text NOT NULL,
    channel text NOT NULL,
    conversation_ref text NOT NULL,
    session_ref text NOT NULL DEFAULT '',
    customer_id text NOT NULL,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_turn_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (business_id, channel, conversation_ref, session_ref, customer_id),
    FOREIGN KEY (business_id, customer_id)
        REFERENCES insurance_customers (business_id, customer_id)
);

INSERT INTO insurance_session_summary (
    business_id, channel, conversation_ref, session_ref, customer_id, summary, last_turn_id, updated_at
)
SELECT business_id, channel, conversation_ref, '', customer_id, summary, last_turn_id, updated_at
FROM insurance_conversation_summary
WHERE channel = 'WhatsApp'
ON CONFLICT (business_id, channel, conversation_ref, session_ref, customer_id) DO NOTHING;

-- Date provenance stays attached to the specific exchange, not only to transient dialogue state.
ALTER TABLE insurance_conversation_turns ADD COLUMN IF NOT EXISTS event_date date;
