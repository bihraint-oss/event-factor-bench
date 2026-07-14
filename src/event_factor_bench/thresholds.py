"""Deterministic parsing of numeric event thresholds."""

from __future__ import annotations

import math
import re
from numbers import Real

_NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?")
_UNIT = re.compile(
    r"^\s*(?P<unit>thousand|million|billion|trillion|percentage|percent|bps?|k|m|b|t|%)"
    r"(?=\s|\+|$)",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "k": 1_000.0,
    "thousand": 1_000.0,
    "m": 1_000_000.0,
    "million": 1_000_000.0,
    "b": 1_000_000_000.0,
    "billion": 1_000_000_000.0,
    "t": 1_000_000_000_000.0,
    "trillion": 1_000_000_000_000.0,
    "%": 0.01,
    "percent": 0.01,
    "percentage": 0.01,
    "bp": 0.0001,
    "bps": 0.0001,
}


def parse_threshold(raw: str | Real) -> float:
    """Parse one finite threshold into a float.

    Strings may contain one numeric token, currency/operator text, comma separators,
    scientific notation, and a magnitude/percentage suffix. More than one numeric token
    is rejected deliberately so dates and ranges are never silently misread.

    Percentages are returned as proportions (``"12.5%" -> 0.125``), and basis points as
    proportions (``"25 bps" -> 0.0025``).
    """

    if isinstance(raw, bool):
        raise TypeError("boolean values are not thresholds")
    if isinstance(raw, Real):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("threshold must be finite")
        return value
    if not isinstance(raw, str):
        raise TypeError("threshold must be a string or real number")

    text = raw.strip()
    if not text:
        raise ValueError("threshold string is empty")

    matches = list(_NUMBER.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one numeric token, found {len(matches)}")

    match = matches[0]
    value = float(match.group(0).replace(",", ""))
    unit_match = _UNIT.match(text[match.end() :])
    if unit_match:
        value *= _MULTIPLIERS[unit_match.group("unit").lower()]
    if not math.isfinite(value):
        raise ValueError("threshold must be finite")
    return value
