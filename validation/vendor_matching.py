import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

FUZZY_MATCH_THRESHOLD = 0.80


def normalize_vendor_name(name: str) -> str:
    """
    Normalise a vendor name for comparison.

    This does not decide whether two vendors are the same.
    It only makes formatting differences easier to compare.
    """
    if not name:
        return ""

    name = name.lower().strip()

    # Replace common punctuation with spaces
    name = re.sub(r"[.,&'()/\-]+", " ", name)

    # Collapse repeated whitespace
    name = re.sub(r"\s+", " ", name)

    return name.strip()

def calculate_vendor_similarity(name_a: str, name_b: str) -> float:
    """
    Calculate a similarity score between two vendor names.

    Returns a value between 0.0 and 1.0.
    """
    normalized_a = normalize_vendor_name(name_a)
    normalized_b = normalize_vendor_name(name_b)

    if not normalized_a or not normalized_b:
        return 0.0

    return SequenceMatcher(None, normalized_a, normalized_b).ratio()


def find_vendor_match(
    extracted_vendor_name: str,
    known_vendors: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Match an extracted vendor against known vendors.

    Outcomes:
        ACCEPT          - exact normalised match
        REVIEW_REQUIRED - similar candidate found, but not exact
        NO_MATCH        - no suitable candidate found
    """
    extracted_normalized = normalize_vendor_name(extracted_vendor_name)

    if not extracted_normalized:
        return {
            "status": "NO_MATCH",
            "vendor_id": None,
            "vendor_name": None,
            "confidence": 0.0,
        }

    best_match = None
    best_score = 0.0

    for vendor in known_vendors:
        vendor_name = vendor.get("vendor_name", "")
        vendor_normalized = normalize_vendor_name(vendor_name)

        if not vendor_normalized:
            continue

        # Exact normalised match: safe to accept.
        if extracted_normalized == vendor_normalized:
            return {
                "status": "ACCEPT",
                "vendor_id": vendor.get("id"),
                "vendor_name": vendor_name,
                "confidence": 1.0,
            }

        score = calculate_vendor_similarity(
            extracted_vendor_name,
            vendor_name,
        )

        if score > best_score:
            best_score = score
            best_match = vendor

    # Similar enough to be a candidate, but not safe to auto-accept.
    if best_match is not None and best_score >= FUZZY_MATCH_THRESHOLD:
        return {
            "status": "REVIEW_REQUIRED",
            "vendor_id": best_match.get("id"),
            "vendor_name": best_match.get("vendor_name"),
            "confidence": round(best_score, 3),
        }

    return {
        "status": "NO_MATCH",
        "vendor_id": None,
        "vendor_name": None,
        "confidence": round(best_score, 3),
    }