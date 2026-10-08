"""Bounded plain-text explanations through the OpenAI Chat Completions SDK."""
import math
import logging
import os
import json
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


def _client(*, api_key, timeout):
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=0)


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
    an OpenAI-compatible endpoint. Timeout defaults to 15 seconds (1..120), and
    INSURANCE_LLM_MAX_TOKENS defaults to 512 (64..4096). Retries are disabled.
    """
    _suppress_provider_logs()
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not model or not api_key:
        raise LLMError('llm_not_configured')
    _validate_endpoint()
    timeout = _setting('INSURANCE_LLM_TIMEOUT_SECONDS', 15, 1, 120, float)
    max_tokens = _setting('INSURANCE_LLM_MAX_TOKENS', 512, 64, 4096, int)
    context = _context(question, evidence)
    try:
        with _client(api_key=api_key, timeout=timeout) as client:
            response = client.chat.completions.create(
                model=model, temperature=0, max_tokens=max_tokens,
                messages=[{'role': 'system', 'content': memory.INSTRUCTIONS},
                          {'role': 'user', 'content': memory.format_prompt(context)}])
        if not response.choices or len(response.choices) != 1:
            raise LLMError('llm_invalid_response')
        choice = response.choices[0]
        if choice.message.refusal or choice.finish_reason == 'content_filter':
            raise LLMError('llm_refusal')
        if choice.finish_reason != 'stop' or choice.message.role != 'assistant':
            raise LLMError('llm_invalid_response')
        text = choice.message.content
        if not isinstance(text, str) or not text.strip() or len(text) > max_tokens * 16:
            raise LLMError('llm_invalid_response')
        return text.strip()
    except LLMError:
        raise
    except openai.APITimeoutError:
        raise LLMError('llm_timeout') from None
    except openai.RateLimitError:
        raise LLMError('llm_rate_limited') from None
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        raise LLMError('llm_auth_failed') from None
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
    timeout = _setting('INSURANCE_LLM_TIMEOUT_SECONDS', 15, 1, 120, float)
    try:
        with _client(api_key=api_key, timeout=timeout) as client:
            response = client.chat.completions.create(
                model=model, temperature=0, max_tokens=256,
                response_format={'type': 'json_object'}, messages=messages)
        if not response.choices or len(response.choices) != 1:
            raise LLMError('llm_invalid_response')
        choice = response.choices[0]
        if choice.message.refusal or choice.finish_reason == 'content_filter':
            raise LLMError('llm_refusal')
        if choice.finish_reason != 'stop' or choice.message.role != 'assistant':
            raise LLMError('llm_invalid_response')
        raw = choice.message.content
        if not isinstance(raw, str) or len(raw) > 4096:
            raise LLMError('llm_invalid_response')
        return json.loads(raw)
    except LLMError:
        raise
    except openai.APITimeoutError:
        raise LLMError('llm_timeout') from None
    except openai.RateLimitError:
        raise LLMError('llm_rate_limited') from None
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        raise LLMError('llm_auth_failed') from None
    except openai.APIStatusError:
        raise LLMError('llm_error') from None
    except openai.APIConnectionError:
        raise LLMError('llm_error') from None
    except Exception:
        raise LLMError('llm_invalid_response') from None
