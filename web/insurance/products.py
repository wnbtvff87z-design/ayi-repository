"""Canonical insurance product codes and vehicle-registration helpers.

Normalization lives here so master sync, dialog, retrieval and cases share one mapping.
Unknown products are never rewritten to a known product (e.g. hogar).
"""
from __future__ import annotations

import re
import unicodedata

KNOWN_PRODUCTS = frozenset({'hogar', 'vida', 'auto', 'moto', 'salud'})

# Display / Airtable aliases → canonical codes used in PostgreSQL and business logic.
PRODUCT_ALIASES = {
    'hogar': 'hogar',
    'vivienda': 'hogar',
    'casa': 'hogar',
    'vida': 'vida',
    'auto': 'auto',
    'coche': 'auto',
    'automovil': 'auto',
    'automóvil': 'auto',
    'vehiculo': 'auto',
    'vehículo': 'auto',
    'moto': 'moto',
    'motocicleta': 'moto',
    'ciclomotor': 'moto',
    'salud': 'salud',
    'health': 'salud',
    'sanitario': 'salud',
    'sanitaria': 'salud',
}

VEHICLE_PRODUCTS = frozenset({'auto', 'moto'})
HOME_PRODUCT = 'hogar'

_PLATE_SEP_RE = re.compile(r'[\s\-./]+')


class UnknownProductError(ValueError):
    """Raised when a product value cannot be mapped to a known canonical code."""


def _fold_key(value: str) -> str:
    text = unicodedata.normalize('NFC', (value or '').strip())
    text = ''.join(
        ch for ch in unicodedata.normalize('NFD', text)
        if unicodedata.category(ch) != 'Mn'
    )
    return text.casefold().strip()


def normalize_product(value, *, allow_unknown=False):
    """Return canonical product code, or raise UnknownProductError.

    When allow_unknown=True, returns the folded raw string for unrecognized values
    (never maps them to hogar/auto/etc.). Empty/None still raises.
    """
    if value is None or not isinstance(value, str) or not value.strip():
        raise UnknownProductError('empty_product')
    if len(value.strip()) > 80:
        raise UnknownProductError('invalid_product')
    key = _fold_key(value)
    # Also try without spaces for compound aliases
    compact = key.replace(' ', '')
    canonical = PRODUCT_ALIASES.get(key) or PRODUCT_ALIASES.get(compact)
    if canonical:
        return canonical
    if allow_unknown:
        return key
    raise UnknownProductError('unknown_product')


def normalize_product_or_none(value):
    try:
        return normalize_product(value)
    except UnknownProductError:
        return None


def is_home_product(value) -> bool:
    return normalize_product_or_none(value) == HOME_PRODUCT


def is_vehicle_product(value) -> bool:
    return normalize_product_or_none(value) in VEHICLE_PRODUCTS


def product_label(value) -> str:
    code = normalize_product_or_none(value) or (str(value).strip() if value else '')
    return {
        'hogar': 'hogar',
        'vida': 'vida',
        'auto': 'automóvil',
        'moto': 'motocicleta',
        'salud': 'salud',
    }.get(code, code or 'producto')


def normalize_vehicle_registration(value):
    """Normalize a plate for comparison without mutating contractual originals.

    Returns (original_stripped_or_None, normalized_or_None).
    Normalization: trim, uppercase, collapse separators/spaces. Jurisdiction-agnostic.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, None
    original = value.strip()
    if not original:
        return None, None
    # Keep a readable original (collapse internal whitespace only).
    original_clean = re.sub(r'\s+', ' ', original).upper()
    normalized = _PLATE_SEP_RE.sub('', original_clean)
    normalized = unicodedata.normalize('NFC', normalized)
    if not normalized:
        return None, None
    return original_clean, normalized


def registrations_match(left, right) -> bool:
    _, a = normalize_vehicle_registration(left)
    _, b = normalize_vehicle_registration(right)
    if not a or not b:
        return False
    return a == b
