ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS end_time text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'Cerrada';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS admin_status text NOT NULL DEFAULT 'Cerrada';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS airtable_record_id text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'Airtable';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS synced_at timestamptz;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS occupied integer NOT NULL DEFAULT 0;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS remaining_capacity integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS booking_slots_open_idx ON booking_slots(business_id,status,slot_date,start_time);
