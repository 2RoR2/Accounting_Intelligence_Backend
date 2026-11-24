"""BUYER-NOT-COMPANY: a purchase document addressed to another company (25 Nov review findings).

A bill from Bayu Seroja to Puncak Tanjung Catering landed in KNT's Inbox and only said "New supplier: add it to
Contacts", which invites adding a stranger as a supplier. This check says what is wrong instead.

It fails only when the buyer is clearly someone else: the document reads another company's name as the buyer, and
neither that name nor a printed buyer TIN or SSM number matches this company. A missing or unreadable buyer, or a
company with no name on its profile, means it does not run (no result at all). It never changes a value.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Optional, Tuple

RULE = 'BUYER-NOT-COMPANY'
BUYER_NAME_KEYS = ('customer',)
BUYER_TIN_KEYS = ('customer_tax_id', 'customer_tin')
BUYER_REGISTRATION_KEYS = ('customer_registration_id', 'customer_registration_number')

# Company-form words carry no identity: "Sdn. Bhd.", "Sendirian Berhad", "Bhd", "(M)", "Enterprise" and so on.
_FORM_WORDS = {'sdn', 'bhd', 'sendirian', 'berhad', 'plt', 'm', 'malaysia', 'co', 'company', 'ltd', 'limited',
               'pte', 'inc', 'corp', 'corporation', 'llp', 'the', 'enterprise', 'enterprises'}
# Close enough to be the same name typed or read slightly differently ("Ketapang Nibong Tradng").
_SAME_NAME = 0.86


def _identifier(value) -> str:
    return re.sub(r'[^0-9A-Za-z]', '', str(value or '')).upper()


def name_core(name) -> str:
    """Lowercase letters and digits only, with company-form words and punctuation removed."""
    words = re.sub(r'[^0-9a-z]+', ' ', str(name or '').casefold()).split()
    return ' '.join(word for word in words if word not in _FORM_WORDS)


def _acronym(core: str) -> str:
    return ''.join(word[0] for word in core.split())


def names_match(buyer, company) -> bool:
    """Lenient: case, punctuation and "Sdn. Bhd." variants do not matter; an acronym or a contained name counts."""
    a, b = name_core(buyer), name_core(company)
    if not a or not b:
        return False
    if a == b or a.replace(' ', '') == b.replace(' ', ''):
        return True
    if f' {a} ' in f' {b} ' or f' {b} ' in f' {a} ':
        return True
    # "KNT" for "Ketapang Nibong Trading", or "KNT Ketapang Nibong Trading".
    if len(b.split()) >= 2 and _acronym(b) in a.split():
        return True
    return SequenceMatcher(None, a, b).ratio() >= _SAME_NAME


def readable_name(value) -> bool:
    """A buyer name worth comparing: at least three letters in one word. "#&", "1" or "N/A" do not count."""
    core = name_core(value)
    return bool(core) and core not in {'n a', 'na', 'nil', 'none', 'cash', 'walk in'} and \
        any(sum(ch.isalpha() for ch in word) >= 3 for word in core.split())


def _first(values: Dict[str, Any], keys) -> Optional[str]:
    return next((str(values[key]).strip() for key in keys if values.get(key) not in (None, '')), None)


def addressee_check(identity: Optional[Dict[str, Any]], values: Dict[str, Any], category: Optional[str]
                    ) -> Tuple[Optional[dict], Optional[dict]]:
    """(error, check) for one purchase document. Both None when the check does not run."""
    if category != 'accounts_payable' or not identity:
        return None, None
    company = (identity.get('name') or '').strip()
    own_tin, own_registration = _identifier(identity.get('tin')), _identifier(identity.get('registrationNumber'))
    if not company and not own_tin and not own_registration:
        return None, None
    buyer = _first(values, BUYER_NAME_KEYS)
    buyer_tin = _identifier(_first(values, BUYER_TIN_KEYS))
    buyer_registration = _identifier(_first(values, BUYER_REGISTRATION_KEYS))
    passed = {'rule': RULE, 'result': 'PASS', 'checkStatus': 'CHECKED', 'required': False, 'field': 'customer'}
    if own_tin and buyer_tin == own_tin:
        return None, {**passed, 'message': "The buyer's TIN is this company's TIN."}
    if own_registration and len(own_registration) >= 6 and buyer_registration == own_registration:
        return None, {**passed, 'message': "The buyer's registration number is this company's."}
    if not buyer or not readable_name(buyer):
        return None, None  # Nothing readable to compare: it does not run.
    if company and names_match(buyer, company):
        return None, {**passed, 'message': 'Addressed to this company.'}
    if not company:
        return None, None  # A name cannot be judged against a company that has none on its profile.
    return {'code': RULE, 'field': 'customer', 'severity': 'high', 'actual_value': buyer, 'expected_value': company,
            'message': f"This document is addressed to {buyer}, not {company}{'' if company.endswith('.') else '.'} "
                       "If it isn't yours, set it aside."}, None


def apply_addressee(result: dict, identity: Optional[Dict[str, Any]], values: Dict[str, Any], category: Optional[str]) -> dict:
    """Adds the finding to a validation result. When it fails, "New supplier" (EX-008) is withdrawn for this
    document: a stranger's supplier should not be offered for Contacts."""
    error, check = addressee_check(identity, values, category)
    if check:
        result['checks'].append(check)
    if not error:
        return result
    result['errors'] = [item for item in result['errors'] if item.get('code') != 'EX-008']
    result['checks'] = [item for item in result['checks'] if item.get('rule') != 'EX-008']
    result['errors'].append(error)
    result['checks'].append({'rule': 'EX-008', 'result': 'NOT_APPLICABLE', 'checkStatus': 'NOT_APPLICABLE', 'required': False,
                             'message': 'Supplier matching is skipped: this document is addressed to another company.'})
    if isinstance(result.get('vendor_match'), dict):
        result['vendor_match'] = {**result['vendor_match'], 'status': 'NOT_CHECKED'}
    return result
