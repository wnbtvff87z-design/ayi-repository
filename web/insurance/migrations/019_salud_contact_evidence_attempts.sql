-- Incremental: salud product canonicalization + claim operational contact / evidence attempts.

-- Canonicalize salud aliases without rewriting unrelated products.
UPDATE insurance_policies
SET product = 'salud'
WHERE product IS NOT NULL
  AND lower(translate(trim(both FROM product), 'ÁÉÍÓÚÜÑáéíóúüñ', 'AEIOUUNaeiouun'))
      IN ('salud', 'health', 'sanitario', 'sanitaria');

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS operational_contact jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS evidence_attempts jsonb NOT NULL DEFAULT '{}'::jsonb;
