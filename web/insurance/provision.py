"""Administrative provisioning of insurance master data in PostgreSQL (source of truth).

Loads business customer, policy, policy version, authorization and (optionally) a document
registration using the real schema. Idempotent, one transaction, dry-run unless --apply.
No DNI/NIE or name is stored in plaintext (HMAC only, via identity.upsert_customer). The PDF
itself must already be in the bucket at storage.object_key(...); the document worker verifies it.

  python -m insurance.provision --actor ops --business-id INS-BIZ-001 \
    --customer-id CUS-000001 --display-name "Ana Pérez García" --document 12345678Z \
    --policy-id POL-000123 --contract-number 000123 --product hogar \
    --version-id V1 --valid-from 2025-01-01 \
    --document-id DOC-000456 --sha256 <64 hex> --apply
"""
import argparse
import hashlib
import os
import sys
from datetime import date

from insurance import cases as _cases
from insurance import identity, storage
from insurance.documents import ID_RE, RegistrationError, register_existing_object
from insurance.master_sync import ensure_hmac_key, revoke_customer


def provision(conn, *, actor, business_id, customer_id, display_name, document, policy_id, product,
              version_id, valid_from, valid_to=None, contract_number=None, full_name=None,
              document_id=None, sha256=None, given_name=None, first_surname=None):
    for label, v in (('business_id', business_id), ('customer_id', customer_id),
                     ('policy_id', policy_id), ('version_id', version_id)):
        if not isinstance(v, str) or not ID_RE.fullmatch(v):
            raise ValueError(f'invalid {label}')
    if contract_number is not None and not identity.CONTRACT_NUMBER_RE.fullmatch(contract_number):
        raise ValueError('invalid contract_number')
    if bool(document_id) != bool(sha256):
        raise ValueError('--document-id and --sha256 must be given together')
    if not identity.document_hmac(business_id, document) or not identity.name_hmac(
            business_id, full_name or display_name):
        raise ValueError('INSURANCE_CASE_HMAC_KEY (>=32 bytes), a document and name+surname are required')
    ensure_hmac_key(conn, business_id)
    if conn.execute(
            'SELECT 1 FROM insurance_customers WHERE business_id=%s AND document_hmac=%s '
            'AND customer_id<>%s LIMIT 1',
            (business_id, identity.document_hmac(business_id, document), customer_id)).fetchone():
        raise ValueError('duplicate_customer_document')
    if document_id:
        old_document = conn.execute(
            'SELECT policy_id,version_id FROM insurance_documents WHERE business_id=%s AND document_id=%s FOR UPDATE',
            (business_id, document_id)).fetchone()
        if old_document and (old_document['policy_id'], old_document['version_id']) != (policy_id, version_id):
            raise RegistrationError('immutable_document_association')
    old_policy = conn.execute(
        'SELECT customer_id,product,contract_number FROM insurance_policies '
        'WHERE business_id=%s AND policy_id=%s FOR UPDATE', (business_id, policy_id)).fetchone()
    if old_policy and (old_policy['customer_id'], old_policy['product'], old_policy['contract_number']) != \
            (customer_id, product, contract_number):
        revoke_customer(conn, business_id, old_policy['customer_id'])
        revoke_customer(conn, business_id, customer_id)
        conn.execute(
            'UPDATE insurance_authorizations SET revoked_at=now() '
            'WHERE business_id=%s AND policy_id=%s AND revoked_at IS NULL', (business_id, policy_id))
    old_version = conn.execute(
        'SELECT valid_from,valid_to FROM insurance_policy_versions '
        'WHERE business_id=%s AND policy_id=%s AND version_id=%s FOR UPDATE',
        (business_id, policy_id, version_id)).fetchone()
    if old_version and (old_version['valid_from'], old_version['valid_to']) != (valid_from, valid_to):
        revoke_customer(conn, business_id, customer_id)
    identity.upsert_customer(conn, business_id, customer_id, display_name, document, full_name,
                             given_name, first_surname)
    conn.execute(
        'INSERT INTO insurance_policies(business_id,policy_id,customer_id,product,contract_number) '
        'VALUES(%s,%s,%s,%s,%s) ON CONFLICT (business_id,policy_id) DO UPDATE SET '
        'customer_id=EXCLUDED.customer_id,product=EXCLUDED.product,contract_number=EXCLUDED.contract_number',
        (business_id, policy_id, customer_id, product, contract_number))
    conn.execute(
        'INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from,valid_to) '
        'VALUES(%s,%s,%s,%s,%s) ON CONFLICT (business_id,policy_id,version_id) DO UPDATE SET '
        'valid_from=EXCLUDED.valid_from,valid_to=EXCLUDED.valid_to',
        (business_id, policy_id, version_id, valid_from, valid_to))
    if not conn.execute(
            'SELECT 1 FROM insurance_authorizations WHERE business_id=%s AND customer_id=%s AND policy_id=%s '
            'AND revoked_at IS NULL AND (valid_to IS NULL OR valid_to>now()) LIMIT 1',
            (business_id, customer_id, policy_id)).fetchone():
        conn.execute(
            'INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by) '
            'VALUES(%s,%s,%s,%s)', (business_id, customer_id, policy_id, actor))
    result = {'customer_id': customer_id, 'policy_id': policy_id, 'version_id': version_id}
    if document_id:
        result['document'] = register_existing_object(
            conn, actor_id=actor, business_id=business_id, policy_id=policy_id, version_id=version_id,
            document_id=document_id, expected_sha256=sha256)
    conn.execute(
        'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
        "VALUES(%s,%s,'provision',%s,'ok')",
        (actor, business_id, f'{customer_id}/{policy_id}/{version_id}/{document_id or "-"}'[:200]))
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('actor', 'business-id', 'customer-id', 'display-name', 'policy-id', 'product', 'version-id'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--document', default=os.getenv('INSURANCE_PROVISION_DOCUMENT', ''),
                   help='customer DNI/NIE (or env INSURANCE_PROVISION_DOCUMENT, avoids shell history)')
    p.add_argument('--full-name')
    p.add_argument('--given-name', help='registered given name, including compound names')
    p.add_argument('--first-surname', help='complete registered first surname; requires --given-name')
    p.add_argument('--contract-number')
    p.add_argument('--valid-from', required=True, type=date.fromisoformat)
    p.add_argument('--valid-to', type=date.fromisoformat)
    p.add_argument('--document-id')
    p.add_argument('--sha256')
    p.add_argument('--sha256-from-bucket', action='store_true',
                   help='read the PDF from the bucket (needs INSURANCE_BUCKET_* vars) and use its real SHA-256')
    p.add_argument('--apply', action='store_true', help='commit; default is a dry run (rollback)')
    a = p.parse_args(argv)
    if a.sha256_from_bucket:
        if a.sha256 or not a.document_id:
            print('error: use --sha256-from-bucket with --document-id and without --sha256', file=sys.stderr)
            return 2
        key = storage.object_key(a.business_id, a.policy_id, a.version_id, a.document_id)
        try:
            data = storage.read(key)
        except storage.StorageError as exc:
            print(f'error: {exc} ({key})', file=sys.stderr)
            return 1
        if not data.startswith(b'%PDF-'):
            print('error: not_a_pdf', file=sys.stderr)
            return 1
        a.sha256 = hashlib.sha256(data).hexdigest()
        print(f'bucket object {key} size={len(data)} sha256={a.sha256}')
    try:
        with _cases.db() as conn:
            try:
                out = provision(
                    conn, actor=a.actor, business_id=a.business_id, customer_id=a.customer_id,
                    display_name=a.display_name, document=a.document, policy_id=a.policy_id,
                    product=a.product, version_id=a.version_id, valid_from=a.valid_from,
                    valid_to=a.valid_to, contract_number=a.contract_number, full_name=a.full_name,
                    document_id=a.document_id, sha256=a.sha256,
                    given_name=a.given_name, first_surname=a.first_surname)
            except (ValueError, RegistrationError) as exc:
                conn.rollback()
                print(f'error: {exc}', file=sys.stderr)
                return 2
            if a.apply:
                conn.commit()
            else:
                conn.rollback()
            print(('APPLIED ' if a.apply else 'DRY-RUN ') + str(out))
    except _cases.CasePersistenceError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
