# Insurance provisioning (PostgreSQL is the source of truth)

`web/insurance/provision.py` loads customer, policy (+`contract_number`), policy version, authorization and the
document registration in ONE transaction, idempotently. Dry run by default; `--apply` commits. No Airtable.
Load order follows the FKs (migrations 004–007). The PDF is only *registered* (`pending_verification`);
`insurance_doc_worker.py` is the only process that reads the bucket and verifies SHA-256.

## Values that cannot be derived from the repo (must be supplied by the owner)
| Value | Where it is required | Where to get it |
|---|---|---|
| `--product` | `insurance_policies.product` NOT NULL | the policy's product (e.g. hogar/auto/vida), from the contract |
| `--valid-from` (and `--valid-to` if it ends) | `insurance_policy_versions.valid_from` NOT NULL | start date on the policy's Condiciones Particulares; retrieval only uses versions in force at the fact date |
| `--actor` | `insurance_authorizations.granted_by`, audit log, `documents.registered_by` | the operator's name/id |
| Full name | `name_hmac` needs name + surname; the caller must say it identically (accents/ñ matter, order kept) | `--display-name`/`--full-name` exactly as the caller will say it (a bare "Sandra Vargas" only matches that) |
| SHA-256 | `insurance_documents.sha256` | computed from the real bucket object (below); never typed by hand |

## Where to run
Run in the **insurance-docs-worker** service: it has `INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY`
(must be identical to web's; add it if missing) and the `INSURANCE_BUCKET_*` variables. Web must not hold bucket
credentials. `INSURANCE_DATABASE_URL` must have INSERT/UPDATE on insurance_customers/policies/policy_versions/
authorizations/documents/audit_log and USAGE on identity sequences; if it is read-only for those, use a writer role.
Use `railway ssh` (inside the private network) rather than `railway run` (runs locally; internal hosts don't resolve).

## 1. Real SHA-256 of the PDF (read-only, no DB access)
```
railway ssh --service insurance-docs-worker -- sh -c 'cd /app/web 2>/dev/null || cd web; python - <<PY
import hashlib
from insurance import storage
d = storage.read("insurance-policies/INS-BIZ-001/POL-000123/VER-001/DOC-000456.pdf")
print(d[:5], len(d), hashlib.sha256(d).hexdigest())
PY'
```
Expected: `b'%PDF-' <bytes> <64 hex>`. Errors: `object_missing` (key wrong/bucket id wrong), `bucket_head_failed`.
Alternatively let the tool do it: `--sha256-from-bucket` (below) prints and uses the same value.

## 2. Dry run (rolled back)
```
railway ssh --service insurance-docs-worker -- sh -c 'cd /app/web 2>/dev/null || cd web; INSURANCE_PROVISION_DOCUMENT=51959566J python -m insurance.provision \
  --actor <OPERADOR> --business-id INS-BIZ-001 --customer-id <CUSTOMER_ID> --display-name "Sandra Vargas" \
  --policy-id POL-000123 --contract-number "058342561/00000" --product <PRODUCTO> \
  --version-id VER-001 --valid-from <AAAA-MM-DD> \
  --document-id DOC-000456 --sha256-from-bucket'
```
`<CUSTOMER_ID>` is a new internal key of your choosing (`[A-Za-z0-9_-]`, e.g. `CUS-000001`).
Expected: `DRY-RUN {... 'status': 'pending_verification' ...}`.

## 3. Apply: same command plus `--apply` → `APPLIED {...}`.

## 4. Order after provisioning
1. (`insurance-migrate-job` is already done: 001–007; do not re-run for provisioning.)
2. `insurance-docs-worker` running (polls every 15 s) → document becomes `ready` and pages indexed.
3. `web` with `INSURANCE_ENABLED=true` and the number's Business config resolving to `INS-BIZ-001`/sector seguros.
4. `insurance-outbox-worker` only for escalations/cases to Airtable; not needed for the document flow.

## 5. Validation SQL
```sql
SELECT customer_id,active,document_hmac IS NOT NULL AS has_doc,name_hmac IS NOT NULL AS has_name
  FROM insurance_customers WHERE business_id='INS-BIZ-001';
SELECT policy_id,customer_id,contract_number,product FROM insurance_policies WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT version_id,valid_from,valid_to FROM insurance_policy_versions WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT authorization_id,customer_id,granted_by,revoked_at FROM insurance_authorizations WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT document_id,status,attempts,last_error,size_bytes,verified_at FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456';
SELECT d.status AS document_status, d.last_error,
       count(p.*) AS pages_total,
       count(*) FILTER (WHERE p.indexed AND length(btrim(p.body))>0) AS usable_pages
  FROM insurance_documents d LEFT JOIN insurance_document_pages p USING (business_id,document_id)
 WHERE d.business_id='INS-BIZ-001' AND d.document_id='DOC-000456' GROUP BY d.status,d.last_error;
```
Statuses: `pending_verification`→`verifying`→`ready` | `needs_review` (illegible/failed pages) | `hash_mismatch` |
`object_missing` | `invalid_object` | `failed`. Recover a failed one by re-running the provision with the right sha.

## 6. Single diagnostic query
```sql
SELECT EXISTS(SELECT 1 FROM insurance_customers WHERE business_id='INS-BIZ-001' AND active) AS customer_found,
       EXISTS(SELECT 1 FROM insurance_policies WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123') AS policy_found,
       EXISTS(SELECT 1 FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456') AS document_found,
       EXISTS(SELECT 1 FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456' AND status='ready') AS document_ready,
       (SELECT count(*) FROM insurance_document_pages WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456' AND indexed AND length(btrim(body))>0) AS pages_indexed;
```

## Risks / inconsistencies found
- Contract numbers with `/` (`058342561/00000`) were truncated by the caller-declaration parser and then
  failed `policy_not_matched` in retrieval. Fixed here (identity.py, retrieval.py) with tests.
- No new Railway variable is required by the tool; `INSURANCE_CASE_HMAC_KEY` must be the SAME on web and the
  provisioning service, otherwise identity never matches (HMACs differ).
- Name matching is exact (accents folded except ñ, order kept): store the name the caller will say.
- Real data in PostgreSQL requires the separate insurance DB/role (docs/insurance-pr1-audit.md); verify it.
- Airtable is not used by this flow (only the case outbox); bucket key must match
  `insurance-policies/<business>/<policy>/<version>/<document>.pdf` exactly (case sensitive).
- `INSURANCE_ENABLED` defaults to false; the web test call is only routed to insurance when true.
