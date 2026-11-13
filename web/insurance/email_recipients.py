"""Easy recipient map for human agents and service professionals.

Resolution order for professional emails:
1. NOTIFICATION_OVERRIDE_EMAIL (forces every notification to one test mailbox)
2. SERVICE_<TYPE>_EMAIL env (SERVICE_FONTANERIA_EMAIL, …)
3. PostgreSQL insurance_professionals (non-example domains)
4. This JSON file (or INSURANCE_EMAIL_RECIPIENTS_PATH) — easy fallback
5. SERVICE_DEFAULT_EMAIL / file service_default_email

Human agent:
1. HUMAN_AGENT_EMAIL env
2. File human_agent_email
"""
from __future__ import annotations

import json
import logging
import os
import unicodedata
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_SERVICE_ENV = {
    'fontanería': 'SERVICE_FONTANERIA_EMAIL',
    'cristalería': 'SERVICE_CRISTALERIA_EMAIL',
    'pintura': 'SERVICE_PINTURA_EMAIL',
    'mobiliario': 'SERVICE_MOBILIARIO_EMAIL',
    'asistencia': 'SERVICE_ASISTENCIA_EMAIL',
}

_DEFAULT_PATH = Path(__file__).with_name('email_recipients.json')


def _fold(value: str) -> str:
    text = unicodedata.normalize('NFC', (value or '').strip())
    text = ''.join(
        ch for ch in unicodedata.normalize('NFD', text)
        if unicodedata.category(ch) != 'Mn'
    )
    return text.casefold().strip()


def recipients_path() -> Path:
    custom = (os.getenv('INSURANCE_EMAIL_RECIPIENTS_PATH') or '').strip()
    if custom:
        return Path(custom)
    return _DEFAULT_PATH


@lru_cache(maxsize=4)
def _load_cached(path_str: str, mtime: float | None):
    path = Path(path_str)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        log.warning('Could not read email recipients file %s: %s', path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def load_recipients(*, force_reload: bool = False) -> dict:
    path = recipients_path()
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None
    if force_reload:
        _load_cached.cache_clear()
    return dict(_load_cached(str(path), mtime))


def human_agent_email() -> str | None:
    env = (os.getenv('HUMAN_AGENT_EMAIL') or '').strip()
    if env:
        return env
    file_val = (load_recipients().get('human_agent_email') or '').strip()
    return file_val or None


def service_default_email() -> str | None:
    env = (os.getenv('SERVICE_DEFAULT_EMAIL') or '').strip()
    if env:
        return env
    file_val = (load_recipients().get('service_default_email') or '').strip()
    return file_val or None


def professional_from_file(service_type: str) -> tuple[str | None, dict | None]:
    data = load_recipients()
    professionals = data.get('professionals') or {}
    if not isinstance(professionals, dict):
        return None, None
    key = _fold(service_type)
    entry = None
    for raw_key, value in professionals.items():
        if _fold(str(raw_key)) == key:
            entry = value
            break
    if not isinstance(entry, dict):
        return None, None
    email = (entry.get('email') or '').strip()
    if not email:
        return None, None
    prof = {
        'name': (entry.get('name') or f'Profesional de {service_type}').strip(),
        'email': email,
        'phone': entry.get('phone'),
        'service_type': service_type,
    }
    return email, prof


def service_env_key(service_type: str) -> str | None:
    return _SERVICE_ENV.get(service_type) or _SERVICE_ENV.get(_fold(service_type))
