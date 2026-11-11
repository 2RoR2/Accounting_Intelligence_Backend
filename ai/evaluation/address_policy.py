"""Address separator equivalence shared by every evaluator. Standard library only.

The same rule is applied to the label and to the prediction, and it is reported
separately from strict exact match. It never changes stored predictions, source
text or evidence; extraction keeps the printed line breaks.

Rule: NFKC, whitespace collapse and case-folding (the original text rules), plus
commas and line breaks between address components count as one separator. Commas
between digits ("1,200") are preserved; all other punctuation, digits, word
boundaries and component order must still match. No fuzzy matching.
"""
from __future__ import annotations

import re
import unicodedata

ADDRESS_POLICY = "address-comma-line-separators/1"
ADDRESS_FIELDS = frozenset({"supplier_address", "customer_address", "provider_address", "merchant_address",
                            "billing_address", "shipping_address", "service_address"})
_SEPARATOR_COMMA = re.compile(r"(?<!\d),|,(?!\d)")


def normalise_address(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("MISSING_ADDRESS")
    text = " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()
    return " ".join(_SEPARATOR_COMMA.sub(" ", text).split())


def same_address(expected, actual) -> bool:
    try:
        return normalise_address(expected) == normalise_address(actual)
    except ValueError:
        return False
