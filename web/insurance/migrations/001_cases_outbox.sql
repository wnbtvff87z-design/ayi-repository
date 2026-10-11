CREATE TABLE IF NOT EXISTS insurance_cases (
    case_id uuid PRIMARY KEY,
    business_id text NOT NULL,
    customer_ref text NOT NULL,
    thread_key text NOT NULL,
    product text NOT NULL,
    policy_id text,
    policy_version_id text,
    status text NOT NULL CHECK (status IN ('pending', 'resolved')),
    latest_reason text NOT NULL CHECK (
        latest_reason IN (
            'insufficient_evidence',
            'missing_information',
            'ambiguity',
            'contradiction',
            'unreadable_document',
            'human_interpretation',
            'identity_not_verified'
        )
    ),
    urgency text NOT NULL CHECK (urgency IN ('normal', 'high', 'critical')),
    next_action text NOT NULL,
    revision integer NOT NULL DEFAULT 0,
    airtable_record_id text,
    resolution text,
    resolved_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS insurance_cases_one_pending_thread_idx
    ON insurance_cases (business_id, customer_ref, thread_key)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS insurance_case_questions (
    question_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES insurance_cases(case_id),
    business_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('Voice', 'WhatsApp')),
    external_id text NOT NULL,
    question text NOT NULL,
    policy_id text,
    policy_version_id text,
    reason text NOT NULL CHECK (
        reason IN (
            'insufficient_evidence',
            'missing_information',
            'ambiguity',
            'contradiction',
            'unreadable_document',
            'human_interpretation',
            'identity_not_verified'
        )
    ),
    urgency text NOT NULL CHECK (urgency IN ('normal', 'high', 'critical')),
    next_action text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, channel, external_id)
);

CREATE INDEX IF NOT EXISTS insurance_case_questions_case_idx
    ON insurance_case_questions (case_id, created_at, question_id);

CREATE TABLE IF NOT EXISTS insurance_case_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES insurance_cases(case_id),
    event_type text NOT NULL CHECK (event_type IN ('created', 'question_added', 'question_updated', 'resolved')),
    actor text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS insurance_outbox (
    outbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES insurance_cases(case_id),
    revision integer NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CONSTRAINT insurance_outbox_status_check
        CHECK (status IN ('pending', 'processing', 'done')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (case_id, revision)
);

CREATE INDEX IF NOT EXISTS insurance_outbox_ready_idx
    ON insurance_outbox (next_attempt_at, outbox_id)
    WHERE status IN ('pending', 'processing');
