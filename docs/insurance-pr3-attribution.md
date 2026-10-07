# Insurance: escalation cause, attribution and operator access

Base: `develop` SHA `2db88ebebfefd103f1520c219dcb15dbaf7c4790` (the `copilot/improve-query-response-flow` branch was cut from it).
Baseline suite: 268 passed / 54 skipped without PostgreSQL; 322 passed with a local PostgreSQL (`INSURANCE_TEST_DATABASE_URL`).

## 1. Why the query escalates

Message "He guardado tu consulta para revisión humana…" is produced only at the end of `insurance/dialog.py::process`, after `create_or_update_case` succeeded in PostgreSQL. Stages traced in `_answer`:

| Stage | Result today |
|---|---|
| Identity: `identity.verified_customer` | Needs a non-revoked, unexpired row in `insurance_identity_verifications` for the HMAC of (business, channel, phone). **No code path in the customer dialogue or the admin API writes that table.** |
| No verified customer | `_answer` skips retrieval and returns `reason=identity_not_verified` → the quoted message. |
| Later stages (authorization, version, document, pages, evidence, LLM) | Only reachable after identity is verified. |

**Demonstrated by code/tests:** unless a verification row exists, the escalation happens at the identity stage, before any policy/document lookup. This is the only stage that can produce it for a caller with no verification row.
**Needs Railway/PostgreSQL to confirm:** whether `insurance_identity_verifications` has any row for the test number; whether customers/policies/authorizations/versions/documents/pages exist for `INS-BIZ-001`/`POL-000123`/`DOC-000456`. Queries (read-only, no PII selected): counts per table filtered by `business_id`, and `SELECT status FROM insurance_documents WHERE document_id='DOC-000456'`, `count(*)` of `insurance_document_pages` with `indexed`.
Retrieval and the LLM prompt were NOT changed (failing stage is upstream of them); they now emit per-stage diagnostics so the next call shows which stage fails.

## 2. Behaviour implemented

* A. Locate (`identity.locate_candidate`): exact HMAC of DNI/NIE (+ optional exact normalized name HMAC) → *candidate*. DNI with an incompatible name is not linked. Plaintext DNI is never stored (HMAC + last 3 chars).
* B. Verify: unchanged and closed. **Missing: an approved verification mechanism** (e.g. one-time code to a phone registered by the insurer, callback, or authenticated portal) that writes `insurance_identity_verifications`. DNI + name + policy number never verify.
* C. Authorize: `insurance_authorizations` (unchanged); retrieval pins business → customer → authorized policy → applicable version.
* Contract number: new `insurance_policies.contract_number` (text, leading zeros preserved, unique per business). `policy_id` is the internal key and is NOT derived from `DOC-…`. Matching is exact on token boundaries (`POL-12` does not match `POL-123`).
* Verified + several policies + no number → asks for the number (lists nothing). Unverified → case with candidate lead; asks for DNI/name only as a locating hint.
* Retrieval reason codes: `no_authorized_policy`, `version_not_applicable`, `policy_not_matched`, `multiple_policies`, `document_not_registered`, `document_not_ready`, `ready_without_usable_pages`, `no_matching_pages`, `ok`. No-match is never "not covered".
* Diagnostics: log `insurance_diag correlation_id stage identity_state reason_code authorization_status document_status usable_pages retrieval_status evidence_count llm_result decision` (whitelist; no PII). `correlation_id` = hash of business/channel/message id, also stored in case context; `diagnostic_code` stored per question.
* Customer message only claims saving after PostgreSQL confirmed; it never says a person has seen it (Airtable sync is async). PG failure → "No se ha creado un caso".

## 3. Case attribution (PostgreSQL, migration `006_attribution_and_human_access.sql`)

`insurance_cases.customer_id` is set only from a verified identity; `attribution_state` ∈ `verified_authorized`, `candidate_pending_identity`, `customer_unknown`, `policy_pending_confirmation`. Verified and unverified threads never merge. `insurance_case_claims` holds the unverified declaration per question (HMAC, masked tail, declared name, declared contract number, candidate id, match status). All questions stay in `insurance_case_questions`.

## 4. Operator API

`GET /insurance/admin/cases/<case_id>`: individual bearer token (`insurance_admin_users`, HMAC), `can_read_cases=true` required (default false), scoped to the operator's `business_id` (other business → 404), every read written to `insurance_audit_log` before the data is returned (audit failure → 503, no data). Shows customer display name (verified only), verification state, confirmed policy/contract number, all questions, reason/urgency/next action, evidence, unverified claims with candidate display name. No DNI, no phone.
The shared-key detail `GET /internal/insurance/cases/<id>` is **blocked by default** (`INSURANCE_HUMAN_SHARED_DETAIL_ENABLED=true` re-enables it; not fit for real data). `POST …/resolve` still uses the shared key: **blocker** for real data until it moves to individual tokens.

## 5. Airtable mirror (PostgreSQL → Airtable only)

Decision: **no `Insurance Customers` / `Insurance Policies` tables.** Airtable has no per-record authorization here, the operator can resolve identity and policy in the audited PG API, and mirroring DNI/name/contract number would spread PII. Instead the existing `Insurance Cases` row gets one extra field.

Contract to review before changing Airtable (not verified against the real base):

| Table | Field | Type | Options | Source (PostgreSQL) | Direction | Visibility |
|---|---|---|---|---|---|---|
| Insurance Cases | Attribution State | Single select | `Identidad verificada`, `Candidato, identidad pendiente`, `Cliente desconocido`, `Póliza pendiente de confirmar` | `insurance_cases.attribution_state` via outbox payload | PG → Airtable only | Operators |
| Insurance Cases | Case ID, Customer Reference (HMAC), Product Type, Urgency, Status, Reason Summary, Task Summary, Next Action, Revision | unchanged | unchanged | unchanged | PG → Airtable | unchanged |

The field is sent only if `INSURANCE_AIRTABLE_ATTRIBUTION=true` (create the column first, otherwise Airtable rejects the write). The operator opens the case in the PG API using the `Case ID` (no key in links). Edits in Airtable never touch PostgreSQL, verification, authorization or resolution. `Insurance Human Tasks` and restaurant tables untouched. Outbox already gives idempotent upserts (key `Case ID`), retries/backoff and terminal failure handling; cases are never deleted so no delete projection is needed. If you later need linked tables, add them as projections keyed by stable IDs with the same outbox.

## 6. Configuration

`INSURANCE_DATABASE_URL`, `INSURANCE_MIGRATION_DATABASE_URL` (apply 006), `INSURANCE_CASE_HMAC_KEY` (≥32 bytes; also keys DNI/name HMACs: **rotating it invalidates customer lookup keys and verifications**), `INSURANCE_ADMIN_ENABLED=true`, `INSURANCE_ADMIN_TOKEN_KEY`, optional `INSURANCE_AIRTABLE_ATTRIBUTION`, `INSURANCE_HUMAN_SHARED_DETAIL_ENABLED` (default false).

## 7. Repeating the controlled call (needs authorized access; not done here)

1. Apply migration 006 with the migration DSN.
2. Provision (ops, controlled script using `identity.upsert_customer`) the customer with DNI/name HMACs, the policy with its `contract_number` (not derived from the object key), version `VER-001`, authorization, and the document row for the existing bucket key; run the document worker so it becomes `ready` with indexed pages. The PDF itself was not accessed or added to the repo.
3. Write an `insurance_identity_verifications` row via the approved verifier (currently none exists), then call by voice/WhatsApp and read the `insurance_diag` logs by `correlation_id`.

## 8. Tests

Local with PostgreSQL: `tests/test_insurance_attribution.py` (synthetic data). Mocks: LLM, Airtable HTTP. Not run: any real Railway/Airtable/Bucket/Twilio/PDF test. Restaurant/consultora routing is covered by the existing `test_known_sectors_keep_existing_dialogues`.
