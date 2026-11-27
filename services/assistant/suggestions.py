"""Starter questions per page. Each one is answered by the fallback router too (tests/unit/assistant), so a
suggestion works even when the model is unavailable."""
from __future__ import annotations

GENERAL = [
    'Which bills are due next week?',
    'How much profit did we make this month?',
    'Who are our top suppliers this month?',
    'How many documents are waiting for review?',
]

PAGES = {
    'today': ['Which bills are due next week?', 'Which invoices are overdue?',
              'How many documents are waiting for review?', 'How far is the month-end close?'],
    'dashboard': GENERAL,
    'reports': ['Why were expenses higher this month than last month?', 'How much profit did we make this month?',
                'What are our total assets and liabilities?', 'What did we spend the most on this month?'],
    'documents': ['How many documents are waiting for review?', 'Which documents need fixing?',
                  'Why is this document flagged?', 'Find documents that mention delivery'],
    'exceptions': ['Which documents need fixing?', 'How many documents are waiting for approval?',
                   'Why is this document flagged?', 'How many documents are waiting for review?'],
    'records': ['Which bills are due next week?', 'Which invoices are overdue?',
                'Who are our top customers this month?', 'Find records for Pauh Builders'],
    'reconciliation': ['How many bank lines are unmatched?', 'What is the oldest unmatched bank line?',
                       'How far is the month-end close?', 'What are our total assets and liabilities?'],
    'purchases': ['Which bills are due next week?', 'Who are our top suppliers this month?',
                  'What did we spend the most on this month?', 'Which bills are overdue?'],
    'sales': ['Which invoices are overdue?', 'Who are our top customers this month?',
              'Which invoices are due in the next two weeks?', 'How much did we sell this month?'],
    'money-out': ['Which bills are due next week?', 'Which bills are overdue?',
                  'Who are our top suppliers this month?', 'How many bank lines are unmatched?'],
    'money-in': ['Which invoices are overdue?', 'Who are our top customers this month?',
                 'How many bank lines are unmatched?', 'How much did we sell this month?'],
    'approvals': ['How many documents are waiting for approval?', 'Which documents need fixing?',
                  'Why is this document flagged?', 'How far is the month-end close?'],
}


def suggestions_for(page: str | None, *, exclude: str | None = None, limit: int = 4) -> list[str]:
    items = PAGES.get((page or '').strip().lower(), GENERAL)
    skip = (exclude or '').strip().casefold()
    return [item for item in items if item.casefold() != skip][:limit]
