"""Read-only access to the private Railway Bucket (S3 API). Used ONLY by insurance_doc_worker;
the Web service must never import-and-call this module."""
import os

MAX_PDF_BYTES = 25 * 1024 * 1024


class StorageError(Exception):
    pass


class ObjectNotFound(StorageError):
    pass


def object_key(business_id, policy_id, version_id, document_id):
    return f'insurance-policies/{business_id}/{policy_id}/{version_id}/{document_id}.pdf'


def _client():
    import boto3
    from botocore.config import Config
    names = ('BUCKET_ENDPOINT', 'BUCKET_REGION', 'BUCKET_ACCESS_KEY_ID', 'BUCKET_SECRET_ACCESS_KEY')
    values = {n: os.getenv('INSURANCE_' + n, '').strip() for n in names}
    if not all(values.values()) or not os.getenv('INSURANCE_BUCKET_NAME', '').strip():
        raise StorageError('Insurance bucket is not configured')
    return boto3.client(
        's3',
        endpoint_url=values['BUCKET_ENDPOINT'],
        region_name=values['BUCKET_REGION'],
        aws_access_key_id=values['BUCKET_ACCESS_KEY_ID'],
        aws_secret_access_key=values['BUCKET_SECRET_ACCESS_KEY'],
        config=Config(retries={'max_attempts': 2}, connect_timeout=5, read_timeout=30),
    )


def head(key, client=None):
    try:
        r = (client or _client()).head_object(Bucket=os.environ['INSURANCE_BUCKET_NAME'].strip(), Key=key)
    except Exception as exc:
        status = getattr(exc, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode')
        if status == 404:
            raise ObjectNotFound('object_missing') from exc
        raise StorageError('bucket_head_failed') from exc
    return {'size': int(r['ContentLength']), 'content_type': r.get('ContentType', '')}


def read(key, client=None):
    meta = head(key, client)
    if meta['size'] > MAX_PDF_BYTES:
        raise StorageError('object_too_large')
    try:
        r = (client or _client()).get_object(Bucket=os.environ['INSURANCE_BUCKET_NAME'].strip(), Key=key)
        data = r['Body'].read(MAX_PDF_BYTES + 1)
    except Exception as exc:
        raise StorageError('bucket_read_failed') from exc
    if len(data) > MAX_PDF_BYTES:
        raise StorageError('object_too_large')
    return data
