"""Questions refused before any model call or database query.

These are the owner rules made deterministic, so they hold even if a model would comply: the assistant never acts,
never calculates tax, never looks outside the session's company, never shows its instructions and never writes SQL.
Refusal messages contain no digits and name the page where a person can do the thing.
"""
from __future__ import annotations

import re

ACTION_PAGES = {
    'post': 'Records, with Post', 'approve': 'Approvals', 'reject': 'Approvals', 'pay': 'Money out',
    'email': "the document's Send button", 'send': "the document's Send button", 'export': 'Reports, with Export',
    'delete': "the record's own page", 'remove': "the record's own page", 'void': "the document's own page",
    'transfer': 'Money out', 'submit': 'Documents, with Submit', 'match': 'Reconcile', 'reconcile': 'Reconcile',
    'change': "the record's own page", 'update': "the record's own page", 'edit': "the record's own page",
    'create': 'Sales or Purchases, with New', 'issue': 'Sales, with Issue', 'upload': 'Upload',
}
_VERBS = '|'.join(sorted(ACTION_PAGES, key=len, reverse=True))
_LEAD = r'^(?:(?:hey|hi|ok|okay|ai|accordai|assistant|bot)\b[\s:,!-]*)*(?:please\s+|pls\s+|now\s+|just\s+)*'
# "Change in expenses..." or "Post-dated cheques?" are questions about the books, not commands.
IMPERATIVE = re.compile(_LEAD + rf'(?P<verb>{_VERBS})\b(?!-)(?!\s+(?:in|of|on|from|between|over|since|for|with)\b)',
                        re.IGNORECASE)
YOU_REQUEST = re.compile(r'\b(?:can|could|would|will|pls|please)\s+(?:you|u)\s+(?:please\s+)?(?:go ahead and\s+)?'
                         rf'(?P<verb>{_VERBS})\b'
                         rf'|\b(?:go ahead and|i want you to|i need you to|you should|you must)\s+(?P<verb2>{_VERBS})\b',
                         re.IGNORECASE)
OBJECT_COMMAND = re.compile(rf'\b(?P<verb>{_VERBS})\s+(?:this|that|these|those|it|them|all|every|the|my|our)\b', re.IGNORECASE)
QUESTION_START = re.compile(r'^\s*(?:what|which|who|whom|when|where|why|how|is|are|was|were|do|does|did|can we|'
                            r'should we|could we|have we|has|list|show)\b', re.IGNORECASE)
SQL = re.compile(r'\bsql\b|\bselect\s+\*|\b(?:drop\s+table|insert\s+into|delete\s+from|update\s+\w+\s+set)\b|'
                 r'\b(?:run|write|execute)\s+(?:a\s+|this\s+)?(?:query|script|code|command)\b', re.IGNORECASE)
PROMPT_LEAK = re.compile(r'\b(system prompt|your (?:instructions|prompt|rules|configuration)|developer message|'
                         r'initial prompt|hidden prompt|api[ _-]?keys?|secret keys?|access token|passwords?|'
                         r'environment variables?|ignore (?:all |your |the )?(?:previous|prior|above) instructions)\b',
                         re.IGNORECASE)
TAX_ADVICE = re.compile(r'\b(?:calculate|compute|work out|estimate)\b.{0,30}\b(?:tax|sst|gst)\b'
                        r'|\btax advice\b|\btax rate\b|\bsst rate\b'
                        r'|\b(?:what|which|how much)\b.{0,40}\btax\b.{0,40}\b(?:charge|should|must|owe|file|claim|apply)\b'
                        r'|\bshould\b.{0,40}\b(?:charge|claim|pay)\b.{0,20}\b(?:tax|sst|gst)\b', re.IGNORECASE)
OTHER_COMPANY = re.compile(r'\b(?:other|another|sister|all|every|different|competitor\'?s?)\s+'
                           r'(?:compan(?:y|ies)|tenants?|clients?|workspaces?|businesses|firms?)\b'
                           r'|\btenant\s*(?:id|_id)\b|\bcross[- ]tenant\b', re.IGNORECASE)

MESSAGES = {
    'PROMPT': 'I cannot share my instructions or any keys. I can answer questions about your books.',
    'SQL': 'I cannot run or write database queries. Ask me about a report, a contact or a document instead.',
    'TAX': ('AccordAI never calculates tax or gives tax advice. The tax summary report shows only the tax recorded '
            'on posted documents; please ask your tax adviser.'),
    'OTHER_COMPANY': 'I can only see this company\'s books, so I cannot answer about other companies.',
}


def refusal_for(question: str) -> tuple[str, str] | None:
    """(code, message) when the question must be refused without any model call, otherwise None."""
    text = ' '.join((question or '').split())
    if PROMPT_LEAK.search(text):
        return 'PROMPT', MESSAGES['PROMPT']
    if SQL.search(text):
        return 'SQL', MESSAGES['SQL']
    if OTHER_COMPANY.search(text):
        return 'OTHER_COMPANY', MESSAGES['OTHER_COMPANY']
    if TAX_ADVICE.search(text):
        return 'TAX', MESSAGES['TAX']
    match = YOU_REQUEST.search(text) or IMPERATIVE.search(text)
    if not match and not QUESTION_START.search(text):
        match = OBJECT_COMMAND.search(text)
    if match:
        verb = (match.group('verb') or match.groupdict().get('verb2') or '').lower()
        page = ACTION_PAGES.get(verb, 'the app')
        return 'ACTION', (f'I can only read your books, so I cannot {verb} anything. '
                          f'A person does that in {page}.')
    return None
