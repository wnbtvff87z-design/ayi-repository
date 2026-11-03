-- Incremental: vehicle registration on policies, product canonicalization,
-- and claim states for automatic professional dispatch (no universal human gate).

ALTER TABLE insurance_policies
    ADD COLUMN IF NOT EXISTS vehicle_registration text;

ALTER TABLE insurance_policies
    ADD COLUMN IF NOT EXISTS vehicle_registration_normalized text;

CREATE INDEX IF NOT EXISTS insurance_policies_vehicle_registration_idx
    ON insurance_policies (business_id, vehicle_registration_normalized)
    WHERE vehicle_registration_normalized IS NOT NULL;

-- Canonicalize known product aliases without rewriting unknown products to hogar.
UPDATE insurance_policies
SET product = CASE
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('hogar', 'vivienda', 'casa') THEN 'hogar'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('vida') THEN 'vida'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('auto', 'coche', 'automovil', 'vehiculo') THEN 'auto'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('moto', 'motocicleta', 'ciclomotor') THEN 'moto'
    ELSE lower(trim(both FROM product))
END
WHERE product IS NOT NULL
  AND product <> CASE
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('hogar', 'vivienda', 'casa') THEN 'hogar'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('vida') THEN 'vida'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('auto', 'coche', 'automovil', 'vehiculo') THEN 'auto'
    WHEN lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
         IN ('moto', 'motocicleta', 'ciclomotor') THEN 'moto'
    ELSE lower(trim(both FROM product))
END;

-- Expand claim lifecycle for automatic professional dispatch (idempotent recreate).
ALTER TABLE insurance_claims DROP CONSTRAINT IF EXISTS insurance_claims_state_check;
ALTER TABLE insurance_claims
    ADD CONSTRAINT insurance_claims_state_check CHECK (state IN (
        'INFORMATION_GATHERING',
        'INCIDENT_INTERPRETED',
        'COVERAGE_CHECK',
        'PHOTOS_REQUESTED',
        'PHOTOS_RECEIVED',
        'INVOICE_REQUESTED',
        'INVOICE_RECEIVED',
        'EVIDENCE_VALIDATION',
        'READY_FOR_AUTO_DISPATCH',
        'DISPATCH_PENDING',
        'DISPATCHED_TO_PROFESSIONAL',
        'READY_FOR_HUMAN_REVIEW',
        'HUMAN_REVIEW',
        'MORE_INFO_REQUIRED',
        'ADDITIONAL_DOCUMENTS_REQUIRED',
        'APPROVED',
        'REJECTED',
        'CLOSED'
    ));
