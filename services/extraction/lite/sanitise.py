"""Deterministic sanitisation for OCR text before it enters an AI prompt."""
from __future__ import annotations

import re


_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPEATED_RUN = re.compile(r"([^\n\r])\1{31,}")


def sanitise_ocr_text(text: str, *, max_chars: int) -> tuple[str, dict[str, int]]:
    """Remove prompt-breaking characters while preserving readable evidence.

    Counts are safe metadata only; the OCR text itself is never included in the
    sanitisation event or error message.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    counts = {
        "control_characters": 0,
        "zero_width_characters": 0,
        "repeated_runs": 0,
        "truncated_characters": 0,
    }

    def remove_zero_width(match: re.Match[str]) -> str:
        counts["zero_width_characters"] += 1
        return ""

    def replace_control(match: re.Match[str]) -> str:
        counts["control_characters"] += len(match.group(0))
        return " "

    cleaned = _ZERO_WIDTH.sub(remove_zero_width, text)
    cleaned = _CONTROL.sub(replace_control, cleaned)

    def collapse_run(match: re.Match[str]) -> str:
        counts["repeated_runs"] += 1
        return match.group(1) * 8

    cleaned = _REPEATED_RUN.sub(collapse_run, cleaned)
    if len(cleaned) > max_chars:
        counts["truncated_characters"] = len(cleaned) - max_chars
        cleaned = cleaned[:max_chars]
    return cleaned, counts
