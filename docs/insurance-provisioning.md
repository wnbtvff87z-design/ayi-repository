# Insurance provisioning (PostgreSQL is the source of truth)

No script existed to populate `insurance_*` tables: `identity.upsert_customer` (web/insurance/identity.py)
was an "ops/tests" helper only, and the admin API (`/insurance/admin/documents/register`) only registers
documents for policies/versions/authorizations that already exist. `web/insurance/provision.py` loads all of it.

Load order / FKs: customer -> policy (`contract_number` = number said by the caller, text) -> policy_version
-> authorization (required by document registration) -> document (`pending_verification`; the worker
verifies the PDF at `insurance-policies/<business>/<policy>/<version>/<document>.pdf`).

`business_id` has no PG table: it is the `Business_ID` resolved from the dialled number (Business config),
and must equal the value used here (e.g. `INS-BIZ-001`).

Prereqs: `python -m insurance.migrate` applied; `INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY` (>=32 bytes,
same as the web service); PDF uploaded to the bucket; `sha256sum file.pdf`.

```
railway run --service <web-service> -- sh -c 'cd web && INSURANCE_PROVISION_DOCUMENT=12345678Z python -m insurance.provision \
  --actor ops --business-id INS-BIZ-001 --customer-id CUS-000001 --display-name "Ana Pérez López" \
  --policy-id POL-000123 --contract-number 000123 --product hogar \
  --version-id V1 --valid-from 2025-01-01 \
  --document-id DOC-000456 --sha256 <64-hex> --apply'
```

Omit `--apply` for a dry run (rolled back). Idempotent. Then run the document worker
(`insurance_doc_worker.py`) so DOC-000456 becomes `ready` and pages are indexed.
