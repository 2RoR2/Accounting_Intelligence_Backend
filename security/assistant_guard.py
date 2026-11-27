"""Guards for Ask AccordAI (feature F1), beside the extraction guards in ai_guard.py.

This module adds only what the assistant needs on top of the existing controls, and reuses them:

* G3 number binding: the model writes an answer template with ``{{f1}}`` placeholders and the server fills every
  number from facts the database returned. Any digit, number word, link or markup outside a placeholder rejects
  the answer, so a number the model invented (or read from injected text) can never be shown.
* G4/G5 untrusted text: text that came from documents or bank lines goes through ``ai_guard.sanitize_document_text``
  (which uses ``sanitise.sanitise_ocr_text``) and its injection scan before it reaches the model.
* G6 masking: the question and tool results are masked with ``privacy.Masker`` (emails, phones, NRIC, cards and
  account numbers) before they leave the server.
* G7 budgets: per-user and per-company question limits through ``main.rate_limit`` and a daily token cap read from
  the assistant's own audit rows.
* G8 audit: one ``audit_logs`` row per answer with masked, size-capped metadata only. No document text, no secrets.

Nothing here calls a model or writes anything except the audit row.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from security.ai_guard import (
    AIGuardError,
    sanitize_document_text,
    scan_prompt_injection,
)
from services.extraction.lite.privacy import Masker

AUDIT_ACTION = 'AI_ASSIST_ANSWER'
MAX_TEMPLATE_CHARS = 1200
MAX_AUDIT_QUESTION_CHARS = 300
MAX_UNTRUSTED_CHARS = 160
PLACEHOLDER = re.compile(r'\{\{\s*(f\d{1,3})\s*\}\}')
# Spelled-out numbers would let a model state an amount or count without a digit ("one million", "fifteen out of
# twenty-two"). "one" itself is left out: it is too common in plain English ("no one", "one of the bills").
NUMBER_WORDS = re.compile(
    r'\b(hundreds?|thousands?|millions?|billions?|trillions?|dozens?|zero|two|three|four|five|six|seven|eight|nine|'
    r'ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|'
    r'sixty|seventy|eighty|ninety|ribu|juta|bilion|ratus|puluh|belas|dua|tiga|empat|lima|enam|tujuh|lapan|sembilan|'
    r'sepuluh|sebelas|seratus|seribu)\b',
    re.IGNORECASE)
LINK = re.compile(r'(https?:|www\.|\]\()', re.IGNORECASE)
MARKUP = re.compile(r'(```|\*\*|__|^\s*#{1,6}\s|<|>)', re.MULTILINE)
UNITS = frozenset({'MYR', 'count', 'date', 'text'})
# Chat-specific signals on top of ai_guard.PROMPT_INJECTION_PATTERNS (which target extraction): role markers, persona
# switches and plural objects ("send all invoices") that the extraction patterns' word boundaries miss.
ASSISTANT_INJECTION_PATTERNS = (
    re.compile(r'(^|[\s"\'\[(])(system|assistant|developer|user)\s*:', re.IGNORECASE),
    re.compile(r'<\|\s*\w+|\[/?inst\]|###\s*(instruction|system)', re.IGNORECASE),
    re.compile(r'\b(you are now|act as|pretend to be|new instructions|from now on)\b', re.IGNORECASE),
    # Not "transfer"/"pay"/"funds": ordinary bank descriptions ("IBG FUND TRANSFER") must stay visible.
    re.compile(r'\b(send|email|forward|upload|share)\b.{0,60}\b(invoices|documents|records|files|data)\b',
               re.IGNORECASE),
    re.compile(r'\b(say|tell|answer|reply|report)\b.{0,40}\b(profit|revenue|total|balance)\s+(is|was|=)', re.IGNORECASE),
)

__all__ = [
    'AUDIT_ACTION',
    'AIGuardError',
    'bind_numbers',
    'check_question_budget',
    'format_value',
    'mask_text',
    'scan_prompt_injection',
    'tokens_spent_today',
    'untrusted_text',
    'write_audit',
]


def format_value(value: str, unit: str) -> str:
    """How a fact appears inside a rendered answer. Money is RM with thousands separators; text is quoted."""
    if unit == 'MYR':
        try:
            amount = Decimal(value)
        except (InvalidOperation, TypeError):
            return value
        sign = '-' if amount < 0 else ''
        return f'{sign}RM {abs(amount):,.2f}'
    if unit == 'text':
        return f'"{value}"'
    return value


def bind_numbers(template: Any, facts: dict[str, dict]) -> tuple[str, list[str]]:
    """Fill ``{{fN}}`` placeholders from facts. Raise AIGuardError for anything the model must not write itself.

    Returns (rendered answer, fact ids used in order of first use).
    """
    if not isinstance(template, str) or not template.strip():
        raise AIGuardError('AI_ANSWER_EMPTY')
    if len(template) > MAX_TEMPLATE_CHARS:
        raise AIGuardError('AI_ANSWER_TOO_LONG')
    if any(ord(char) < 32 and char != '\n' for char in template):
        raise AIGuardError('AI_ANSWER_CONTROL_CHARACTER')
    outside = PLACEHOLDER.sub(' ', template)
    if '{' in outside or '}' in outside:
        raise AIGuardError('AI_ANSWER_PLACEHOLDER_INVALID')
    # isnumeric covers every script's digits, fractions and superscripts, not only 0-9.
    if any(char.isnumeric() for char in outside):
        raise AIGuardError('AI_ANSWER_DIGIT')
    if NUMBER_WORDS.search(outside):
        raise AIGuardError('AI_ANSWER_NUMBER_WORD')
    if LINK.search(outside):
        raise AIGuardError('AI_ANSWER_LINK')
    if MARKUP.search(outside):
        raise AIGuardError('AI_ANSWER_MARKUP')
    used: list[str] = []

    def fill(match: re.Match[str]) -> str:
        fact_id = match.group(1)
        fact = facts.get(fact_id)
        if fact is None:
            raise AIGuardError('AI_ANSWER_UNKNOWN_FACT')
        if fact_id not in used:
            used.append(fact_id)
        return format_value(fact['value'], fact['unit'])

    rendered = PLACEHOLDER.sub(fill, template)
    return re.sub(r'[ \t]{2,}', ' ', rendered).strip(), used


def mask_text(masker: Masker, text: str) -> str:
    """Mask personal identifiers and account numbers before text is sent to a provider or written to the audit."""
    return masker.mask_text(text) if text else text


def untrusted_text(text: Any, *, max_chars: int = MAX_UNTRUSTED_CHARS) -> tuple[str, list[str]]:
    """Sanitise text that came from a document or bank line (reuses the extraction sanitiser and injection scan).

    Returns (clean single-line text, injection findings). Findings come from ai_guard's scan plus the chat-specific
    ASSISTANT_INJECTION_PATTERNS; the caller decides how to show flagged text.
    """
    if text is None:
        return '', []
    try:
        cleaned, findings = sanitize_document_text(str(text), max_chars=max_chars * 4)
    except AIGuardError:
        return '', ['AI_INPUT_INVALID']
    if not findings and any(pattern.search(cleaned) for pattern in ASSISTANT_INJECTION_PATTERNS):
        findings = ['PROMPT_INJECTION_SIGNAL']
    cleaned = ' '.join(cleaned.split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars - 1].rstrip() + '…'
    return cleaned, findings


def check_question_budget(user: dict, *, user_limit: int, tenant_limit: int, day: str) -> None:
    """G7: raise 429 past the per-user (10 minutes) or per-company (per day) question limit. 0 disables a limit."""
    from main import (
        rate_limit,  # Lazy: main imports the route registry, which imports the assistant.
    )

    if user_limit > 0:
        rate_limit(f"ai-assist-user:{user['id']}", user_limit, 600,
                   message='Ask AccordAI has had too many questions. Please wait a few minutes and try again.',
                   code='AI_ASSIST_RATE_LIMITED')
    if tenant_limit > 0:
        rate_limit(f"ai-assist-tenant:{user['tenant_id']}:{day}", tenant_limit, 86_400,
                   message='Ask AccordAI has reached its daily limit for this company. Try again tomorrow.',
                   code='AI_ASSIST_RATE_LIMITED')


def tokens_spent_today(db, tenant_id) -> int:
    """Model tokens this company's assistant answers used today (from the audit rows; no separate counter table)."""
    row = db.execute("""SELECT COALESCE(SUM(NULLIF(details::jsonb->>'tokens','')::bigint),0) AS tokens
        FROM audit_logs WHERE tenant_id=%s AND action=%s AND created_at >= date_trunc('day', now())""",
                     (tenant_id, AUDIT_ACTION)).fetchone()
    return int(row['tokens'] or 0)


def write_audit(db, user: dict, details: dict) -> str:
    """G8: one row per answer. The caller passes masked metadata only; this caps the size as a backstop."""
    encoded = json.dumps(details, ensure_ascii=False, default=str, sort_keys=True)
    if len(encoded) > 20_000:
        details = {key: details.get(key) for key in ('question', 'outcome', 'fallbackReason', 'tokens', 'model',
                                                     'provider', 'factIds')}
        details['truncated'] = True
        encoded = json.dumps(details, ensure_ascii=False, default=str, sort_keys=True)
    row = db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     (user['tenant_id'], user['id'], user['name'], user['role'], AUDIT_ACTION, 'assistant',
                      encoded)).fetchone()
    return str(row['id'])
