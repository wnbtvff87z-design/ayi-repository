"""Bounded plain-text explanations through the OpenAI Chat Completions SDK."""
import math
import logging
import os
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

import openai

from insurance import memory


class LLMError(RuntimeError):
    """A public, content-free failure classification for dialogue diagnostics."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _setting(name, default, minimum, maximum, cast):
    try:
        value = cast(os.getenv(name, str(default)))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError
        return value
    except (ValueError, OverflowError):
        raise LLMError('llm_not_configured') from None


DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_BUDGET_SECONDS = 12
MIN_RETRY_SECONDS = 2
REWRITE_MAX_TERMS = 12
REWRITE_INSTRUCTIONS = (
    'Reescribes consultas de clientes sobre su seguro para buscar en el texto de la póliza. '
    'Recibes un JSON con "palabras" de la consulta. Devuelve solo JSON {"terms": [...]} con hasta '
    f'{REWRITE_MAX_TERMS} palabras sueltas en español: corrige errores de tipeo y añade sinónimos '
    'o términos contractuales equivalentes (por ejemplo, vidrio y cristal, fuego e incendio). '
    'No añadas nombres, números, coberturas que no estén relacionadas ni explicaciones.')


def _client(*, api_key, timeout):
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=0)


def _retryable(exc):
    return isinstance(exc, (openai.APIConnectionError, openai.InternalServerError))


def _status_code(exc):
    """Classify only provider machine codes; never expose exception text or bodies."""
    if getattr(exc, 'code', None) in (
            'context_length_exceeded', 'context_window_exceeded', 'max_context_length_exceeded'):
        return 'llm_context_limit'
    return 'llm_error'


def _content(response, max_chars):
    if not response.choices or len(response.choices) != 1:
        raise LLMError('llm_invalid_response')
    choice = response.choices[0]
    if choice.message.refusal or choice.finish_reason == 'content_filter':
        raise LLMError('llm_refusal')
    if choice.finish_reason != 'stop' or choice.message.role != 'assistant':
        raise LLMError('llm_invalid_response')
    text = choice.message.content
    if text is None or isinstance(text, str) and not text.strip():
        raise LLMError('llm_empty_response')
    if not isinstance(text, str) or len(text) > max_chars:
        raise LLMError('llm_invalid_response')
    return text.strip()


def _create(api_key, timeout, retry=True, **request):
    """One SDK request (SDK retries stay disabled) plus at most one own retry for transient
    timeouts, connection errors or 5xx, only while INSURANCE_LLM_BUDGET_SECONDS allows it."""
    budget = _setting('INSURANCE_LLM_BUDGET_SECONDS', DEFAULT_BUDGET_SECONDS, 1, 240, float)
    started = time.monotonic()
    try:
        with _client(api_key=api_key, timeout=timeout) as client:
            return client.chat.completions.create(**request)
    except Exception as exc:
        remaining = budget - (time.monotonic() - started)
        if not retry or not _retryable(exc) or remaining < MIN_RETRY_SECONDS:
            raise
    with _client(api_key=api_key, timeout=min(timeout, remaining)) as client:
        return client.chat.completions.create(**request)


def _validate_endpoint():
    if 'OPENAI_BASE_URL' not in os.environ:
        return
    try:
        endpoint = urlsplit(os.environ['OPENAI_BASE_URL'])
        if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError
        endpoint.port
    except ValueError:
        raise LLMError('llm_not_configured') from None


def _suppress_provider_logs():
    # SDK/transport DEBUG logs contain prompts and INFO logs expose endpoint paths.
    # Keep suppression permanent: temporarily restoring levels races concurrent requests.
    namespaces = ('openai', 'httpx', 'httpcore')
    for namespace in namespaces:
        logging.getLogger(namespace).setLevel(logging.WARNING)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and any(
                name.startswith(namespace + '.') for namespace in namespaces):
            logger.setLevel(logging.WARNING)


def _context(question, evidence):
    if not isinstance(question, dict):
        return memory.build_context(question=question, evidence=evidence)
    # Only the supported package fields travel to the provider; raw state/identity does not.
    return memory.build_context(
        question=question.get('question', ''), evidence=evidence,
        policy=question.get('policy') or None,
        pending=question.get('pending'),
        recent_turns=({'role': turn['role'], 'content': turn['text']}
                      for turn in question.get('recent', ())),
        summary_text=question.get('summary', ''),
        recalled=question.get('recalled', ()), intent=question.get('intent'),
        version=None, identity_line=bool(question.get('identity')))


def explain(question, evidence):
    """Return grounded text or exactly ESCALAR; raise only safe classified provider errors.

    Requires INSURANCE_LLM_MODEL and OPENAI_API_KEY. OPENAI_BASE_URL optionally selects
    an OpenAI-compatible endpoint. Timeout defaults to 8 seconds (1..120), and
    INSURANCE_LLM_MAX_TOKENS defaults to 512 (64..4096). SDK retries are disabled; one own
    retry runs only for transient failures within INSURANCE_LLM_BUDGET_SECONDS (default 12).
    """
    _suppress_provider_logs()
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not model or not api_key:
        raise LLMError('llm_not_configured')
    _validate_endpoint()
    timeout = _setting('INSURANCE_LLM_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS, 1, 120, float)
    max_tokens = _setting('INSURANCE_LLM_MAX_TOKENS', 512, 64, 4096, int)
    context = _context(question, evidence)
    try:
        response = _create(
            api_key, timeout, model=model, temperature=0, max_tokens=max_tokens,
            messages=[{'role': 'system', 'content': memory.INSTRUCTIONS},
                      {'role': 'user', 'content': memory.format_prompt(context)}])
        return _content(response, max_tokens * 16)
    except LLMError:
        raise
    except openai.APITimeoutError:
        raise LLMError('llm_timeout') from None
    except openai.RateLimitError:
        raise LLMError('llm_rate_limited') from None
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        raise LLMError('llm_auth_failed') from None
    except openai.APIConnectionError:
        raise LLMError('llm_network_error') from None
    except openai.APIStatusError as exc:
        raise LLMError(_status_code(exc)) from None
    except openai.APIResponseValidationError:
        raise LLMError('llm_invalid_response') from None
    except (ValueError, TypeError, AttributeError, IndexError):
        raise LLMError('llm_invalid_response') from None
    except Exception:
        raise LLMError('llm_error') from None


def interpret(messages):
    """Return a JSON proposal, never a tool call or an authorization decision."""
    _suppress_provider_logs()
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not model or not api_key:
        raise LLMError('llm_not_configured')
    _validate_endpoint()
    timeout = _setting('INSURANCE_LLM_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS, 1, 120, float)
    try:
        response = _create(
            api_key, timeout, model=model, temperature=0, max_tokens=256,
            response_format={'type': 'json_object'}, messages=messages)
        return json.loads(_content(response, 4096))
    except LLMError:
        raise
    except openai.APITimeoutError:
        raise LLMError('llm_timeout') from None
    except openai.RateLimitError:
        raise LLMError('llm_rate_limited') from None
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        raise LLMError('llm_auth_failed') from None
    except openai.APIStatusError as exc:
        raise LLMError(_status_code(exc)) from None
    except openai.APIConnectionError:
        raise LLMError('llm_network_error') from None
    except Exception:
        raise LLMError('llm_invalid_response') from None


def _fold(word):
    word = unicodedata.normalize('NFD', str(word).casefold())
    return ''.join(c for c in word if unicodedata.category(c) != 'Mn')


def rewrite(words):
    """Return up to REWRITE_MAX_TERMS search terms (typo fixes and synonyms) for the given
    question words, or an empty list when unconfigured or on any failure. The caller sends
    only folded content words: never names, document numbers or other identifiers."""
    words = [w for w in words if re.fullmatch(r'[a-z]{3,24}', w)][:24]
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not words or not model or not api_key:
        return []
    try:
        _suppress_provider_logs()
        _validate_endpoint()
        timeout = min(_setting('INSURANCE_LLM_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS, 1, 120, float), 4)
        response = _create(
            api_key, timeout, retry=False, model=model, temperature=0, max_tokens=128,
            response_format={'type': 'json_object'},
            messages=[{'role': 'system', 'content': REWRITE_INSTRUCTIONS},
                      {'role': 'user', 'content': json.dumps({'palabras': words}, ensure_ascii=False)}])
        choice = response.choices[0]
        if choice.finish_reason != 'stop' or not isinstance(choice.message.content, str):
            return []
        terms = json.loads(choice.message.content[:2048]).get('terms')
        if not isinstance(terms, list):
            return []
        folded = []
        for term in terms:
            for word in re.findall(r'[a-z]{3,24}', _fold(term)) if isinstance(term, str) else ():
                if word not in folded:
                    folded.append(word)
        return folded[:REWRITE_MAX_TERMS]
    except Exception:
        return []
