ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS end_time text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'Cerrada';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS airtable_record_id text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'Airtable';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS synced_at timestamptz;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS occupied integer NOT NULL DEFAULT 0;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS remaining_capacity integer;
CREATE UNIQUE INDEX IF NOT EXISTS booking_slots_airtable_record_idx ON booking_slots(airtable_record_id) WHERE airtable_record_id IS NOT NULL;
UPDATE booking_slots s SET occupied=x.used, remaining_capacity=GREATEST(s.capacity-x.used,0), status=CASE WHEN s.capacity-x.used<=0 THEN 'Cerrada' ELSE s.status END FROM (SELECT bs.business_id,bs.slot_id,COALESCE(SUM(br.party_size) FILTER (WHERE br.status='Confirmada'),0)::int AS used FROM booking_slots bs LEFT JOIN booking_reservations br ON br.business_id=bs.business_id AND br.slot_id=bs.slot_id GROUP BY bs.business_id,bs.slot_id) x WHERE s.business_id=x.business_id AND s.slot_id=x.slot_id;
