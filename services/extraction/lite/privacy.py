"""Reversible masking of personal and payment identifiers before text leaves this server.

Only OCR text is ever sent to a hosted model, and before that this module replaces
identifiers the model does not need with placeholder tokens:

    emails, phone numbers, Malaysian NRIC numbers, payment card numbers and
    bank / customer account numbers

The mapping from token to original value stays in this process. If the model
returns a token as a field value (for example a utility account number), it is
restored locally, so the stored record is complete while the provider never
received the real identifier.

Not masked, because extraction needs them: business names, business addresses,
registration and tax numbers, document numbers, dates and amounts.
"""
from __future__ import annotations

import re

TOKEN = re.compile(r'\[\[(EMAIL|PHONE|NRIC|CARD|ACCOUNT)_(\d+)\]\]')

_EMAIL = re.compile(r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_NRIC = re.compile(r'(?<!\d)\d{6}-\d{2}-\d{4}(?!\d)')
_CARD = re.compile(r'(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])')
_PHONE_CUE = re.compile(r'\b(?:tel(?:ephone|efon)?|phone|h/?p|mobile|fax|faks|whats\s*app|contact|call|talian)\b', re.I)
_PHONE = re.compile(r'(?<![\w.])\+?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?![\w.-])')
_INTERNATIONAL_PHONE = re.compile(r'(?<![\w.])\+\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){1,3}(?![\w.-])')
_ACCOUNT_CUE = re.compile(
    r'\b(?:bank|maybank|cimb|rhb|ambank|hsbc|ocbc|uob|hong\s+leong|public\s+bank|bank\s+islam|affin|alliance|bsn|'
    r'a/c|acc(?:oun)?t\.?|akaun|iban|swift)\b', re.I)
_ACCOUNT = re.compile(r'(?<![\w.])\d(?:[ -]?\d){7,19}(?![\w.-])')


_DATE_LIKE = re.compile(r'\d{4}[ -]\d{1,2}[ -]\d{1,2}|\d{1,2}[ -]\d{1,2}[ -]\d{4}')


def _digits(match, low, high):
    """A digit run of plausible length that is not simply a printed date."""
    text = match.group(0)
    return low <= len(re.sub(r'\D', '', text)) <= high and not _DATE_LIKE.fullmatch(text.strip())


def _luhn(digits):
    total, double = 0, False
    for char in reversed(digits):
        value = int(char)
        if double:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
        double = not double
    return total % 10 == 0


class Masker:
    """Collects masked values so the same value always gets the same token."""

    def __init__(self):
        self.mapping: dict[str, str] = {}
        self._tokens: dict[tuple[str, str], str] = {}

    def _token(self, kind, value):
        key = (kind, value)
        if key not in self._tokens:
            token = f'[[{kind}_{sum(1 for k in self._tokens if k[0] == kind) + 1}]]'
            self._tokens[key] = token
            self.mapping[token] = value
        return self._tokens[key]

    def _replace(self, pattern, kind, text, accept=lambda match: True):
        return pattern.sub(lambda m: self._token(kind, m.group(0)) if accept(m) else m.group(0), text)

    def mask_line(self, line, *, account_context=False):
        line = self._replace(_EMAIL, 'EMAIL', line)
        line = self._replace(_NRIC, 'NRIC', line)
        line = self._replace(_CARD, 'CARD', line,
                             lambda m: 13 <= len(re.sub(r'\D', '', m.group(0))) <= 19
                             and _luhn(re.sub(r'\D', '', m.group(0))))
        line = self._replace(_INTERNATIONAL_PHONE, 'PHONE', line, lambda m: _digits(m, 8, 15))
        if _PHONE_CUE.search(line):
            line = self._replace(_PHONE, 'PHONE', line, lambda m: _digits(m, 7, 15))
        if account_context or _ACCOUNT_CUE.search(line):
            line = self._replace(_ACCOUNT, 'ACCOUNT', line, lambda m: _digits(m, 8, 20))
        return line

    def mask_text(self, text):
        masked, previous_cue = [], False
        for line in text.split('\n'):
            cue = bool(_ACCOUNT_CUE.search(line))
            # A label on one line with its number alone on the next line.
            digits_only = bool(re.fullmatch(r'[\s:#.-]*\d(?:[ -]?\d){7,19}\s*', line))
            masked.append(self.mask_line(line, account_context=previous_cue and digits_only))
            previous_cue = cue
        return '\n'.join(masked)

    def unmask(self, value):
        if not isinstance(value, str):
            return value
        return TOKEN.sub(lambda m: self.mapping.get(m.group(0), m.group(0)), value)

    def summary(self):
        """Counts only; never the masked values."""
        counts: dict[str, int] = {}
        for token in self.mapping:
            kind = TOKEN.fullmatch(token).group(1)
            counts[kind] = counts.get(kind, 0) + 1
        return counts
