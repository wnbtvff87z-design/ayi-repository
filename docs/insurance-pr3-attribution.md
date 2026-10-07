# Insurance: escalation cause, attribution and operator access

Sections 1–8 below are the historical PR3 audit, not the current conversational contract.
The persistent-memory implementation and changed escalation semantics are documented in section 9.

Base: `develop` SHA `2db88ebebfefd103f1520c219dcb15dbaf7c4790` (the `copilot/improve-query-response-flow` branch was cut from it).
Baseline suite: 268 passed / 54 skipped without PostgreSQL; 322 passed with a local PostgreSQL (`INSURANCE_TEST_DATABASE_URL`).

## 1. Why the query escalates

Message "He guardado tu consulta para revisión humana…" is produced only at the end of `insurance/dialog.py::process`, after `create_or_update_case` succeeded in PostgreSQL. Stages traced in `_answer`:

| Stage | Result today |
|---|---|
| Identity: `identity.verified_customer` | Needs a non-revoked, unexpired row in `insurance_identity_verifications` for the HMAC of (business, channel, phone). **No code path in the customer dialogue or the admin API writes that table.** |
| No verified customer | `_answer` skips retrieval and returns `reason=identity_not_verified` → the quoted message. |
| Later stages (authorization, version, document, pages, evidence, LLM) | Only reachable after identity is verified. |

(Historical, before migration 007.) **Demonstrated by code/tests:** unless a verification row exists, the escalation happens at the identity stage, before any policy/document lookup. This is the only stage that can produce it for a caller with no verification row.
**Needs Railway/PostgreSQL to confirm:** whether `insurance_identity_verifications` has any row for the test number; whether customers/policies/authorizations/versions/documents/pages exist for `INS-BIZ-001`/`POL-000123`/`DOC-000456`. Queries (read-only, no PII selected): counts per table filtered by `business_id`, and `SELECT status FROM insurance_documents WHERE document_id='DOC-000456'`, `count(*)` of `insurance_document_pages` with `indexed`.
Retrieval and the LLM prompt were NOT changed (failing stage is upstream of them); they now emit per-stage diagnostics so the next call shows which stage fails.

## 2. Behaviour implemented

* Decision: **no OTP, signed links or extra codes.** A caller is verified by giving name + surnames + DNI/NIE, which must match **exactly one active** `insurance_customers` row of the business resolved from the dialled number (the caller can never change `business_id`). Matching is exact on normalized values (HMAC): spaces/case/accents (á é í ó ú ü; `ñ` kept), hyphens in compound names, dots/spaces/hyphens in the DNI/NIE. No contains/fuzzy/name-only matching; the check letter is normalized but not validated. Zero or several matches do not verify, and the reply is one generic text that never says which datum failed.
* Missing data (name, surnames or DNI/NIE) asks for them and consumes no attempt. Data, and the pending question, are kept in `insurance_conversation_state` (PG) so nothing has to be repeated; data given all in one message (including the policy number) is processed in that turn. Name and surnames must be sent together in one message.
* On success: `insurance_identity_verifications` row (method `name_surname_document`, verified_by `system:exact-match`, business, customer, channel, session, expiry; no full DNI) plus an `insurance_audit_log` row. TTL `INSURANCE_VERIFICATION_TTL_SECONDS` (default 1800 s). Verification is per business and per channel; Voice is bound to the CallSid, WhatsApp to the phone. It is not reused across Voice/WhatsApp.
* Attempts: failed matches per conversation/channel in a window (`INSURANCE_IDENTITY_MAX_ATTEMPTS` default 5, `INSURANCE_IDENTITY_WINDOW_SECONDS` default 900). After the limit, further messages are blocked and a review case (`identity_attempts_exceeded`, `customer_unknown`, no customer link) is created only if PostgreSQL confirms the write. Values entered are never logged.
* After verification: one authorized policy → continue; several → ask for the contract number (exact text, leading zeros kept, no listing before verification); a policy of another customer → `policy_not_authorized`. Then version → ready document → usable pages → evidence → answer with version and page. Missing evidence escalates (`no_evidence`), never "not covered".
* Escalation causes (`diagnostic_code`): `identity_data_missing`, `identity_no_match`, `identity_ambiguous`, `identity_attempts_exceeded`, `policy_number_required`, `policy_not_authorized`, `document_not_ready`, `no_evidence`, `human_interpretation` (finer retrieval code in context `detail`).
* C. Authorize: `insurance_authorizations` (unchanged); retrieval pins business → customer → authorized policy → applicable version.
* Contract number: new `insurance_policies.contract_number` (text, leading zeros preserved, unique per business). `policy_id` is the internal key and is NOT derived from `DOC-…`. Matching is exact on token boundaries (`POL-12` does not match `POL-123`).
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

`INSURANCE_DATABASE_URL`, `INSURANCE_MIGRATION_DATABASE_URL` (apply 006 and 007), `INSURANCE_CASE_HMAC_KEY` (≥32 bytes; also keys DNI/name HMACs: **rotating it invalidates customer lookup keys and verifications**), `INSURANCE_ADMIN_ENABLED=true`, `INSURANCE_ADMIN_TOKEN_KEY`, `INSURANCE_IDENTITY_MAX_ATTEMPTS`, `INSURANCE_IDENTITY_WINDOW_SECONDS`, `INSURANCE_VERIFICATION_TTL_SECONDS`, optional `INSURANCE_AIRTABLE_ATTRIBUTION`, `INSURANCE_HUMAN_SHARED_DETAIL_ENABLED` (default false).

## 7. Repeating the controlled call (needs authorized access; not done here)

1. Apply migrations 006 and 007 with the migration DSN.
2. Provision (ops, controlled script using `identity.upsert_customer`) the customer with DNI/name HMACs, the policy with its `contract_number` (not derived from the object key), version `VER-001`, authorization, and the document row for the existing bucket key; run the document worker so it becomes `ready` with indexed pages. The PDF itself was not accessed or added to the repo.
3. Call from the number of `INS-BIZ-001`, say name, surnames and DNI/NIE of the provisioned customer (and `POL-000123` if several policies); verification is created automatically. Read by voice/WhatsApp and read the `insurance_diag` logs by `correlation_id`.

## 8. Tests

Local with PostgreSQL: `tests/test_insurance_attribution.py` (synthetic data). Mocks: LLM, Airtable HTTP. Not run: any real Railway/Airtable/Bucket/Twilio/PDF test. Restaurant/consultora routing is covered by the existing `test_known_sectors_keep_existing_dialogues`.

## 9. Persistent conversational Insurance (develop after PR #26)

### Base, root cause and alternatives

The task branch starts at `develop` **`ea8285a907db752adc3c225e7a439d9830bee240`**,
the merge of PR #26. The PR targets `develop`; it does not merge or deploy anything.
No Railway data, destination numbers, configuration, Twilio, Relay, reservations, restaurant/
consultora handlers, workers or Cron are modified.

PR #26 introduced memory tables and helper modules but the dialogue still used a five-question
JSON array. The LLM received only the current retrieval query and page excerpts. A broad regex
matched the isolated conjunction `y`, references fell back silently to one question, pre-identity
pending questions required `?`, verification and state lifetimes were coupled, and ordinary
missing evidence created a case without consent.

Chosen architecture: deterministic PostgreSQL-backed tiers, explicit authentication and policy
selection, retention-scoped streaming recall, and one bounded structured LLM package.

| Alternative | Decision |
|---|---|
| Increase `MAX_HISTORY` | Rejected: no persistence, retention, evidence provenance or bounded context |
| Send all turns to the LLM | Rejected: unbounded cost/context and accidental contractual reliance on history |
| Vector database or a separate memory service | Not added: unnecessary dependencies and another consistency/tenant boundary |
| LLM-generated summary on every turn | Rejected: repeated full-history cost, fabricated facts and retry instability |
| Incremental structured summary plus scoped lexical recall | Selected: deterministic provenance, bounded batches, testable ambiguity |

### Identity and policy state

DNI/NIE is exact after removing spaces, dots and hyphens and folding case. Names are ordered,
accent-folded except `ñ`, with complete normalized registered prefixes. `Celia Zorro` and
`Celia Zorro Condes` both verify against `Celia Zorro Condes` only if the exact document selects
one active customer **inside the already resolved business**. `Celia`, `Zorro`, `Celia Condes`,
`Celia Zor` and `Celia Vargas` do not. Duplicate matching customers never select a person.
Replies do not reveal which individual datum failed; ambiguity requests a complete declaration.
Provisioning can supply trusted compound-name boundaries; see `insurance-provisioning.md`.

The durable dialogue state distinguishes customer verification, active policy/version, requested
contract, a policy switch awaiting resolution, the original pending question and its normalized
retrieval representation, reference clarification, event date and pending human review. Numbers
remain text, including leading zeros. A failed switch clears the previous active policy rather
than answering using it. Multiple policies request a contractual number, never a pre-verification
list. Old-topic recall carries its previous policy, version, date and document/page references;
the current authorization and applicable version are checked again before retrieving evidence.

### Memory tiers and isolation

* **A — retained exchanges:** `insurance_conversation_turns`, with business, channel, HMAC
  conversation reference, Voice CallSid session, verified customer (nullable before verification),
  user/assistant role, original/redacted text and normalized query, timestamp, correlation ID,
  confirmed policy/version, decision, reply linkage and document/page/section provenance.
  Identity-only declarations are not retained as conversational text; DNI/NIE is redacted.
  The unique webhook/role key and transaction-level locking prevent duplicate exchanges.
* **B — recent window:** configurable complete exchanges read from PostgreSQL, not a fixed JSON
  array. Only directly relevant recent pairs enter the prompt.
* **C — summary:** incremental structured topics, prior answers, pending questions, user facts,
  active policy/version, event dates, provisional conclusions, open issues and evidence references.
  Each entry carries turn provenance; retention checks remove expired sources even if the summary
  was updated recently. The assistant turn high-water mark makes retries idempotent.
  Migration `009_session_memory.sql` adds `insurance_session_summary` with a session-scoped
  composite primary key and an exchange `event_date` column. It preserves the legacy summary
  table, imports only session-compatible WhatsApp summaries and deliberately does not import
  unscoped legacy Voice summaries. Reapplying it cannot overwrite newer session summaries.
* **D — old recall:** keyset batches scan the entire permitted retained conversation, not just the
  most recent batch. Clear matches restore a question/answer/evidence reference. Competing topics
  ask which one; no match asks the user to specify. `volviendo a lo primero` can retrieve the
  earliest retained question after 30 or more exchanges.

Every memory read is scoped by business, channel, conversation, customer **and session**.
Voice calls do not share authentication or memory across CallSids. WhatsApp continues within its
business/phone HMAC scope. There is no implicit Voice/WhatsApp or cross-customer linkage.

### Exact LLM messages and budget

`dialog.llm_explain` sends `memory.INSTRUCTIONS` as the system message and
`memory.format_prompt(context)` as the user message, with these optional/mandatory blocks:

1. `IDENTIDAD`: only that the client is verified and authorized; no name, DNI or phone.
2. `PÓLIZA ACTIVA`: policy and applicable version.
3. `ACLARACIÓN PENDIENTE`, when relevant.
4. `RESUMEN DE LA CONVERSACIÓN (no contractual)`.
5. `TURNOS ANTIGUOS RECUPERADOS (no contractual)`: prior question, answer and source identifiers.
6. `TURNOS RECIENTES (no contractual)`: selected complete relevant user/assistant exchanges.
7. `PREGUNTA ACTUAL`.
8. `CLÁUSULAS`: current PostgreSQL page text labelled with page, document, version and section.

The system explicitly forbids using memory as contractual evidence, invented coverage, claim
approval/denial and missing citations; insufficient evidence returns `ESCALAR`. Equal page
numbers in different documents are not interchangeable. No original PDF is sent to the LLM.
Prior source identifiers help interpret “¿por qué?”, “¿dónde dice eso?” and reformulations,
but authorization/version/ready-document checks and **current page retrieval** remain mandatory.

The budget includes the system instructions and the exact rendered user message. Removal
priority is recalled memory, summary, old recent pairs, pending clarification and metadata.
Complete current evidence and the current question are never silently sliced to fit: if the
essential package cannot fit, no LLM request is made and the dialogue fails safely. Context
reports record budget, used characters and discarded categories, not confidential prompt text.
Character budgeting is deliberately not represented as exact tokenizer counts.

### Confirmation and source-of-truth boundaries

Routine no-evidence/interpretation outcomes preserve the question/reason and ask:
**“No encontré evidencia suficiente en tu póliza. ¿Quieres que registre la consulta para revisión humana?”**
Only affirmative consent persists a case; declining does not. Greetings, identity checks, policy
selection and clarifiable references never create cases. Approved urgent protocol and technical
failures on actual questions are controlled exceptions. No saved-case confirmation precedes
PostgreSQL persistence. Database failure never yields policy evidence or a fictitious saved case.

PostgreSQL remains the source of truth. The existing case outbox asynchronously projects the
minimal task to Airtable; dialogue memory is not mirrored there. The original PDF remains in
the bucket, SHA-256 checks that original object, and conversation-time retrieval reads only
`insurance_documents`/`insurance_document_pages`. `ready` without usable indexed pages is
not evidence. Document/page FKs are composite business/document keys; document→policy/version
uses the existing composite FK.

### Configuration, lifetimes and operational limits

| Variable | Default | Meaning |
|---|---:|---|
| `INSURANCE_RECENT_TURNS` | 6 | Complete recent exchanges; selected by relevance |
| `INSURANCE_LLM_CONTEXT_CHARS` | 12000 | System plus rendered user message budget |
| `INSURANCE_TURN_RETENTION_DAYS` | 90 | Logical retention of turns and sourced summary facts |
| `INSURANCE_MAX_TURNS_PER_CONVERSATION` | 2000 | Stored user/assistant rows per conversation/session, not policy capacity |
| `INSURANCE_MEMORY_SCAN_LIMIT` | 500 | Recall **batch size**, not historical search horizon |
| `INSURANCE_RECALLED_TURNS` | 2 | Recalled exchanges admitted to the package |
| `INSURANCE_TURN_MAX_CHARS` | 4000 | Persisted turn text bound |
| `INSURANCE_SUMMARY_MAX_TOPICS` | 30 | Summary topics; older retained turns remain searchable |
| `INSURANCE_SUMMARY_MAX_CHARS` | 6000 | Structured summary JSON bound |
| `INSURANCE_STATE_TTL_SECONDS` | 86400 | Dialogue inactivity lifetime, separate from authentication |
| `INSURANCE_STATE_RETENTION_SECONDS` | 604800 | Physical cleanup age for inactive state |
| `INSURANCE_PURGE_BATCH` | 500 | Bounded cleanup batch |
| `INSURANCE_VERIFICATION_TTL_SECONDS` | 1800 | Existing absolute verification expiry, never extended by automatic updates |
| `INSURANCE_IDENTITY_WINDOW_SECONDS` | 900 | Existing failed-attempt window |
| `INSURANCE_IDENTITY_MAX_ATTEMPTS` | 5 | Existing failed-attempt limit |

Values are optional and have validated safe bounds; no Railway variable has been changed.
Verification expiry requires identity again. Reverification of the same customer can resume
retained conversation; a different customer cannot inherit the previous state/evidence.
Inactivity expiration removes dialogue state from use, not retained conversational history.
Retention/cap evicts oldest rows; summary provenance prevents resurrecting them. Memory is
finite and recall of evicted/expired sources requests clarification rather than pretending to
remember. Physical cleanup is bounded and opportunistic on writes; the existing Cron and
workers are unchanged. Idle installations can call `memory.purge_expired` through controlled
maintenance; no new scheduler is introduced. `memory.erase_customer` removes conversational
turns, summaries, states and verifications for the business/customer; case retention remains
the existing, separately governed policy.

### Rollout and rollback

Apply additive migrations with the existing Insurance migration command and authorized DB role
before releasing web code. Check prefix provisioning for legacy customers and ready usable pages.
New functionality remains behind the existing `INSURANCE_ENABLED` switch (default off).
Rollback can disable Insurance or restore the previous web release **without dropping retained
tables or customer data**. Keep additive schema changes; reverting them would destroy provenance.
Restaurant/consultora, Voice/WhatsApp transports, document/outbox workers and Cron are untouched.
No merge, deployment or live data/number/configuration changes are performed by this task.

Local synthetic results demonstrate isolation and bounded candidate handling, **not Railway
production performance**. They do not establish LLM contractual correctness, legal interpretation,
OCR quality of actual PDFs, exact model-token accounting or infinite memory. A full trusted
compound-name boundary cannot be inferred reliably from an ambiguous combined-name string.
