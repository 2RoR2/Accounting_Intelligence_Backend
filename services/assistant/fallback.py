"""Deterministic answers when the model is unavailable, over budget or rejected (so the demo never shows an error).

route_question maps the question to one tool with arguments by keywords; render_table turns facts into a short
answer the server writes itself ("Here are the figures from the profit and loss report:" plus one line per fact).
Every value shown still comes from a tool, so the fallback obeys the same number rule as the model path.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from security.assistant_guard import format_value
from services.assistant.facts import FactBook
from services.assistant.tools import TOOLS, Args, Tool

MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
MONTHS['sept'] = 9
MONTH_WORD = re.compile(r'\b(' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + r')\b', re.IGNORECASE)
STOP_WORDS = {'a', 'an', 'the', 'find', 'search', 'look', 'for', 'show', 'me', 'where', 'is', 'are', 'any', 'all',
              'documents', 'document', 'records', 'record', 'invoice', 'invoices', 'bill', 'bills', 'that', 'which',
              'mention', 'mentions', 'mentioning', 'says', 'say', 'with', 'text', 'about', 'latest', 'last', 'our',
              'we', 'from', 'of', 'in', 'on', 'please', 'can', 'you', 'containing', 'contain', 'contains', 'printed',
              'word', 'words', 'line', 'lines', 'description', 'up', 'to', 'by', 'get', 'list'}
FALLBACK_LINES = 8


def _month_start(day: date, back: int = 0) -> date:
    year, month = day.year, day.month - back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _month_end(first: date) -> date:
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _named_months(text: str, today: date) -> list[date]:
    """Month names in the question as first days, in the order written (a month after today means last year)."""
    result = []
    for match in MONTH_WORD.finditer(text):
        number = MONTHS[match.group(1).lower()]
        year = today.year if number <= today.month else today.year - 1
        first = date(year, number, 1)
        if first not in result:
            result.append(first)
    return result


def period(text: str, today: date) -> tuple[date, date]:
    """The date range a question means: a named month, last month, this year, or this month (the default)."""
    lowered = text.lower()
    months = _named_months(text, today)
    if months:
        return months[0], _month_end(months[0])
    if re.search(r'\blast month\b|\bprevious month\b', lowered):
        first = _month_start(today, 1)
        return first, _month_end(first)
    if re.search(r'\b(this|the) year\b|\byear to date\b|\bytd\b', lowered):
        return date(today.year, 1, 1), today
    if re.search(r'\blast year\b', lowered):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    first = _month_start(today)
    return first, _month_end(first)


def compare_months(text: str, today: date) -> tuple[str, str]:
    """(earlier/base month, later/compared month) as YYYY-MM. "X than Y" compares Y (base) with X."""
    months = _named_months(text, today)
    if len(months) >= 2:
        first, second = months[0], months[1]
        a, b = (second, first) if re.search(r'\bthan\b', text, re.IGNORECASE) else (first, second)
    elif len(months) == 1:
        b = months[0]
        a = _month_start(b - timedelta(days=1))
    else:
        b = _month_start(today)
        a = _month_start(today, 1)
    return a.strftime('%Y-%m'), b.strftime('%Y-%m')


def _days(text: str) -> int:
    lowered = text.lower()
    found = re.search(r'\b(?:next|in|within)\s+(\d{1,2})\s+days?\b', lowered)
    if found:
        return max(1, min(60, int(found.group(1))))
    if re.search(r'\b(two|2) weeks\b|\bfortnight\b', lowered):
        return 14
    if re.search(r'\bthis month\b|\bmonth end\b', lowered):
        return 30
    return 7


def _search_terms(text: str) -> str | None:
    quoted = re.search(r'["“‘\']([^"”’\']{2,80})["”’\']', text)
    if quoted:
        return quoted.group(1).strip()
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9&.'/-]*", text) if w.lower() not in STOP_WORDS]
    terms = ' '.join(words)[:80].strip()
    return terms if len(terms) >= 2 else None


def _contact_name(text: str) -> str | None:
    patterns = (r'\b(?:does|do|did)\s+(.+?)\s+owe\b', r'\bowed?\s+(?:to us\s+)?by\s+(.+?)(?:\?|$|\s+as of\b)',
                r'\bwe\s+owe\s+(.+?)(?:\?|$|\s+as of\b)', r'\b(?:balance|outstanding)\s+(?:for|with|of)\s+(.+?)(?:\?|$)',
                r'\bowe\s+(?:to\s+)?(.+?)(?:\?|$)')
    for pattern in patterns:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            name = re.sub(r'^(the|our|us)\s+', '', found.group(1).strip(' .,?'), flags=re.IGNORECASE)
            if len(name) >= 2 and name.lower() not in {'us', 'we', 'them', 'anyone', 'people', 'suppliers', 'customers'}:
                return name[:80]
    return None


def route_question(question: str, context: dict, today: date) -> tuple[Tool, Args] | None:
    """Keyword router: the one tool (with arguments) that best fits the question, or None."""
    text = ' '.join((question or '').split())
    lowered = text.lower()

    def pick(name: str, **arguments) -> tuple[Tool, Args]:
        tool = TOOLS[name]
        return tool, tool.args.model_validate(arguments)

    document_id = (context or {}).get('documentId')
    if document_id and re.search(r'\b(why|explain|flag\w*|wrong|check\w*|problem|issue|exception|blocked|this document)\b',
                                 lowered):
        return pick('explain_document', document_id=str(document_id))
    if re.search(r'\b(compare|compared|versus|vs\.?|higher|lower|increase\w*|decrease\w*|rise|rose|drop\w*|went up|'
                 r'went down|more than|less than)\b', lowered) and re.search(
                     r'\b(expense\w*|cost\w*|spend\w*|spent|income|sales|revenue|profit)\b', lowered):
        a, b = compare_months(text, today)
        return pick('compare_periods', period_a=a, period_b=b)
    name = _contact_name(text)
    if name and re.search(r'\bowe[sd]?\b|\boutstanding\b|\bbalance\b', lowered):
        side = 'we_owe' if re.search(r'\bwe\s+owe\b', lowered) else 'owed_to_us' if re.search(
            r'\b(?:does|do|did)\s+.+\s+owe\b|\bowed?\s+(?:to us\s+)?by\b', lowered) else 'either'
        return pick('contact_balance', contact=name, side=side)
    if re.search(r'\b(due|overdue|late|unpaid|outstanding|owe|owed|ageing|aging|to pay|to collect|collect)\b', lowered):
        receivable = re.search(r'\b(invoices?|customers?|receivables?|collect|owed to us|owe us)\b', lowered)
        overdue = bool(re.search(r'\b(overdue|late|unpaid|outstanding|ageing|aging)\b', lowered))
        return pick('due_soon', kind='invoices' if receivable else 'bills', days=_days(text), include_overdue=overdue)
    if re.search(r'\b(top|biggest|largest|main|most)\b.*\b(suppliers?|vendors?|customers?|clients?)\b', lowered) or \
            re.search(r'\bwho\b.*\b(buy|bought|spend|spent|sell|sold|pays?|paid)\b', lowered):
        start, end = period(text, today)
        customers = re.search(r'\b(customers?|clients?|sell|sold|sales)\b', lowered)
        return pick('top_contacts', kind='customers' if customers else 'suppliers', from_date=start, to_date=end)
    if re.search(r'\b(bank|cash) balance\b|\bhow much cash\b|\bbalance sheet\b|\bassets?\b|\bliabilit\w*\b|'
                 r'\bequity\b|\bnet worth\b', lowered):
        return pick('balance_sheet', as_of=today)
    if re.search(r'\b(profit|loss|p&l|p and l|income|revenue|sales|sell|sold|expenses?|costs?|spend|spent|earn\w*|'
                 r'made|make|margin)\b', lowered):
        start, end = period(text, today)
        return pick('profit_and_loss', from_date=start, to_date=end)
    if re.search(r'\b(bank|unmatched|reconcil\w*|statement lines?)\b', lowered):
        return pick('bank_unmatched')
    if re.search(r'\b(month[- ]?end|close the month|closing|close)\b', lowered):
        return pick('month_end_status')
    if re.search(r'\b(find|search|look for|where is|show me|mention\w*|says|contain\w*)\b', lowered):
        terms = _search_terms(text)
        if terms:
            if re.search(r'\b(mention\w*|says|text|printed|contain\w*|word|line|description)\b', lowered):
                return pick('search_document_text', query=terms)
            return pick('search_records', query=terms)
    if re.search(r'\b(documents?|reviews?|waiting|approv\w*|exceptions?|queue|to do|pending|uploads?|fixing|flagged)\b',
                 lowered):
        return pick('document_queue')
    return None


def render_table(tool_titles: list[str], ids: list[str], facts: FactBook, notes: list[str]) -> str:
    """The server-written answer for a fallback: a heading line, then one line per fact (values from the DB)."""
    if not ids:
        return ' '.join(notes) if notes else 'I found nothing in the books for that question.'
    title = ' and '.join(dict.fromkeys(tool_titles)) or 'the books'
    lines = [f'Here are the figures from {title}:']
    for fact_id in ids[:FALLBACK_LINES]:
        fact = facts.items[fact_id]
        lines.append(f'- {fact.label}: {format_value(fact.value, fact.unit)}')
    if len(ids) > FALLBACK_LINES:
        lines.append('More figures are listed below.')
    lines.extend(notes[:2])
    return '\n'.join(lines)
