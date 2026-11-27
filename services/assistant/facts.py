"""Facts: the only place numbers in an answer come from.

Every tool adds facts here. A fact has a per-answer id (f1, f2, ...), a label, a string value with a unit, and the
app link and source it came from. The model only ever refers to a fact by id (security/assistant_guard.bind_numbers).
Links are built here from ids the database returned, never from model text.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from security.assistant_guard import UNITS
from services.assistant.config import MAX_FACTS

CENT = Decimal('0.01')


def area_for(role: str) -> str:
    """The frontend area for a role, as src/lib/constants/routes.ts WORKSPACE_AREA defines it."""
    return 'company' if role == 'local_admin' else 'workspace'


def _value_text(value, unit: str) -> str:
    if unit == 'MYR':
        return str(Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP))
    if unit == 'count':
        return str(int(value or 0))
    if unit == 'date':
        return value.isoformat() if isinstance(value, date) else str(value)
    return '' if value is None else str(value)


@dataclass
class Fact:
    id: str
    label: str
    value: str
    unit: str
    href: str | None
    source: str
    source_href: str | None

    def public(self) -> dict:
        return {'id': self.id, 'label': self.label, 'value': self.value, 'unit': self.unit, 'href': self.href}

    def for_model(self) -> dict:
        return {'id': self.id, 'label': self.label, 'value': self.value, 'unit': self.unit}


class FactBook:
    """All facts gathered for one question. Ids are assigned in order and never reused."""

    def __init__(self, role: str):
        self.area = area_for(role)
        self.items: dict[str, Fact] = {}

    def link(self, page: str, query: dict | None = None, item: str | None = None) -> str:
        path = f'/{self.area}/{page}' + (f'/{item}' if item else '')
        clean = {key: value for key, value in (query or {}).items() if value is not None}
        return path + ('?' + urlencode(clean) if clean else '')

    def add(self, label: str, value, unit: str, *, source: str, href: str | None = None,
            source_href: str | None = None) -> str | None:
        """Add one fact and return its id, or None once the per-question cap is reached."""
        if unit not in UNITS:
            raise ValueError(f'unknown fact unit {unit}')
        if len(self.items) >= MAX_FACTS:
            return None
        fact_id = f'f{len(self.items) + 1}'
        self.items[fact_id] = Fact(fact_id, label[:160], _value_text(value, unit), unit, href, source,
                                   source_href if source_href is not None else href)
        return fact_id

    def lookup(self) -> dict[str, dict]:
        return {fact_id: {'value': fact.value, 'unit': fact.unit} for fact_id, fact in self.items.items()}

    def public(self, ids) -> list[dict]:
        return [self.items[fact_id].public() for fact_id in ids if fact_id in self.items]

    def citations(self, ids) -> list[dict]:
        seen, result = set(), []
        for fact_id in ids:
            fact = self.items.get(fact_id)
            if fact is None or (fact.source, fact.source_href) in seen:
                continue
            seen.add((fact.source, fact.source_href))
            result.append({'label': fact.source, 'href': fact.source_href})
        return result
