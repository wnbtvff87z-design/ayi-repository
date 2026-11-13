"""Backward-compatible re-export of the web sector router.

Canonical module: sector_dialog.py (avoids clashing with insurance.dialog when
`web/insurance` is accidentally placed on PYTHONPATH / sys.path).
"""
from sector_dialog import (  # noqa: F401
    BusinessSectorError,
    InsuranceDisabledSectorError,
    consulting_process,
    general_process,
    process,
    restaurant_process,
    sector_of,
)

__all__ = [
    'BusinessSectorError',
    'InsuranceDisabledSectorError',
    'consulting_process',
    'general_process',
    'process',
    'restaurant_process',
    'sector_of',
]
