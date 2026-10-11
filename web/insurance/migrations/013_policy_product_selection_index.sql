CREATE INDEX IF NOT EXISTS insurance_policies_product_selection_idx
    ON insurance_policies (
        business_id, customer_id,
        translate(lower(normalize(product,NFC)), 'áéíóúüñ', 'aeiouun')
    );
