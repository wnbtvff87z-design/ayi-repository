-- P0: human dispatch authorization + configurable evidence rules (no duplicate claim tables).

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS dispatch_authorization jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS human_review_notes jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE insurance_claims
    ADD COLUMN IF NOT EXISTS review_package jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS insurance_evidence_rules (
    rule_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    business_id text NOT NULL DEFAULT '*',
    product text NOT NULL DEFAULT 'hogar',
    incident_category text NOT NULL DEFAULT '*',
    repair_stage text NOT NULL DEFAULT '*',
    proof_category text NOT NULL,
    is_mandatory boolean NOT NULL DEFAULT true,
    demonstrates text NOT NULL,
    acceptable_document_types jsonb NOT NULL DEFAULT '[]'::jsonb,
    acceptable_alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
    validity_criteria text,
    insufficiency_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    needs_human_review boolean NOT NULL DEFAULT false,
    request_prompt text,
    active boolean NOT NULL DEFAULT true,
    priority integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (business_id, product, incident_category, repair_stage, proof_category)
);

CREATE INDEX IF NOT EXISTS insurance_evidence_rules_lookup_idx
    ON insurance_evidence_rules (product, incident_category, repair_stage)
    WHERE active;

-- Seed default hogar rules (idempotent via ON CONFLICT).
INSERT INTO insurance_evidence_rules (
    business_id, product, incident_category, repair_stage, proof_category,
    is_mandatory, demonstrates, acceptable_document_types, acceptable_alternatives,
    validity_criteria, insufficiency_reasons, needs_human_review, request_prompt, priority
) VALUES
(
    '*', 'hogar', '*', '*', 'foto_video_dano',
    true,
    'Existencia y descripción del daño, identidad del bien y estado actual',
    '["image/jpeg","image/png","video/mp4","video/quicktime"]'::jsonb,
    '["vídeo_demostrativo_10-20s","fotografía_con_contexto_y_entorno"]'::jsonb,
    'La imagen/vídeo debe mostrar el daño declarado con claridad suficiente y perspectiva usable.',
    '["borrosa","irrelevante","documento_en_lugar_de_media","sin_bytes_analizados"]'::jsonb,
    false,
    'Envía fotografías o un vídeo corto (10-20 s) donde se aprecie claramente el daño.',
    10
),
(
    '*', 'hogar', '*', 'trabajo_realizado', 'factura_reparacion',
    true,
    'Coste de reparación reclamado y realización efectiva del trabajo',
    '["application/pdf","image/jpeg","image/png"]'::jsonb,
    '["factura_desglosada_de_reparación_con_datos_del_proveedor"]'::jsonb,
    'Debe ser una factura de reparación (no presupuesto ni ticket de compra genérico) con emisor, fecha, conceptos e importe.',
    '["presupuesto","ilegible","sin_importe","no_corresponde_al_incidente"]'::jsonb,
    false,
    'Adjunta la factura de la reparación ya realizada.',
    20
),
(
    '*', 'hogar', '*', 'bien_sustituido', 'factura_reposicion',
    true,
    'Coste de sustitución o reposición del bien',
    '["application/pdf","image/jpeg","image/png"]'::jsonb,
    '["ticket_o_factura_de_compra_del_nuevo_bien_reemplazado"]'::jsonb,
    'Debe acreditar la compra/reposición del bien afectado con emisor, fecha e importe.',
    '["presupuesto","ilegible","no_corresponde_al_bien"]'::jsonb,
    false,
    'Adjunta la factura o ticket de compra/reposición del bien.',
    20
),
(
    '*', 'hogar', '*', 'propuesta_reparacion', 'presupuesto',
    true,
    'Coste de reparación estimado antes de ejecutar el trabajo',
    '["application/pdf","image/jpeg","image/png"]'::jsonb,
    '["presupuesto_formal_emitido_por_empresa_de_reparaciones"]'::jsonb,
    'Debe identificarse como presupuesto (no factura de trabajo ya cobrado) con conceptos e importe.',
    '["factura_de_reparacion_confundida","ilegible","sin_importe"]'::jsonb,
    false,
    'Adjunta el presupuesto formal de la reparación propuesta.',
    20
),
(
    '*', 'hogar', 'cristalería', '*', 'factura_compra_original',
    false,
    'Adquisición, propiedad, valor original, antigüedad o modelo del bien',
    '["application/pdf","image/jpeg","image/png"]'::jsonb,
    '["fotografía_con_marcado_o_dimensiones","declaración_de_dimensiones_y_tipo_de_cristal"]'::jsonb,
    'Factura de compra original del bien o alternativa documental revisable por un humano.',
    '["presupuesto","ilegible","no_corresponde_al_bien"]'::jsonb,
    true,
    'Si dispones de la factura de compra original del bien, adjúntala.',
    30
),
(
    '*', 'hogar', 'pintura', '*', 'comprobacion_tecnica',
    false,
    'Comprobación técnica de secado/humedad antes o después de pintar',
    '["informe_tecnico","verificacion_pericial"]'::jsonb,
    '["comprobación_y_medición_in_situ_por_el_profesional_asignado"]'::jsonb,
    'No se puede dar por seca/reparada una pared solo porque exista una foto o una factura previa.',
    '["foto_insuficiente_para_secado","falta_informe_tecnico"]'::jsonb,
    true,
    'La condición de secado/humedad quedará pendiente de comprobación técnica.',
    40
),
(
    '*', 'hogar', 'fontanería', '*', 'comprobacion_tecnica',
    false,
    'Localización del origen de la fuga u otra comprobación técnica',
    '["informe_tecnico","verificacion_pericial"]'::jsonb,
    '["vídeo_de_agua_en_curso","informe_de_localización_de_fuga"]'::jsonb,
    'La causa/origen de la fuga puede requerir verificación in situ.',
    '["origen_no_acreditado"]'::jsonb,
    true,
    'Puede ser necesaria una comprobación técnica del origen de la fuga.',
    40
)
ON CONFLICT (business_id, product, incident_category, repair_stage, proof_category)
DO UPDATE SET
    is_mandatory = EXCLUDED.is_mandatory,
    demonstrates = EXCLUDED.demonstrates,
    acceptable_document_types = EXCLUDED.acceptable_document_types,
    acceptable_alternatives = EXCLUDED.acceptable_alternatives,
    validity_criteria = EXCLUDED.validity_criteria,
    insufficiency_reasons = EXCLUDED.insufficiency_reasons,
    needs_human_review = EXCLUDED.needs_human_review,
    request_prompt = EXCLUDED.request_prompt,
    priority = EXCLUDED.priority,
    active = true;
