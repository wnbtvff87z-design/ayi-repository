"""Closed-by-default identity gate. An incoming phone only names a conversation.

A customer is "verified" only if an external verifier / authenticated admin wrote an
unexpired, unrevoked row in insurance_identity_verifications for this business and
conversation. The customer-facing dialogue has no way to create that row.
"""
import hashlib
import hmac
import os
import re

from insurance.cases import MIN_KEY_BYTES


def conversation_ref(business_id, channel, phone):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(key.encode()) < MIN_KEY_BYTES or not digits:
        return None
    return hmac.new(key.encode(), f'conv:{business_id}:{channel}:{digits}'.encode(),
                    hashlib.sha256).hexdigest()


def verified_customer(conn, business_id, channel, phone):
    ref = conversation_ref(business_id, channel, phone)
    if not ref:
        return None
    row = conn.execute(
        'SELECT customer_id FROM insurance_identity_verifications WHERE business_id=%s '
        'AND conversation_ref=%s AND revoked_at IS NULL AND expires_at>now() '
        'ORDER BY verification_id DESC LIMIT 1', (business_id, ref)).fetchone()
    return row['customer_id'] if row else None
