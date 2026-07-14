"""Point-in-time selection from historical probability observations."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class HistorySelectionError(ValueError):
    """Base class for strict point-in-time selection failures."""


class NoEligibleHistoryPointError(HistorySelectionError):
    """Raised when no observation exists at or before the cutoff."""


class StaleHistoryPointError(HistorySelectionError):
    """Raised when the newest eligible observation is too old."""


class ConflictingHistoryPointError(HistorySelectionError):
    """Raised when one timestamp maps to different probabilities."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One timestamped market reference probability."""

    timestamp: datetime
    probability: float

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "point timestamp")
        if isinstance(self.probability, bool):
            raise TypeError("probability must not be boolean")
        probability = float(self.probability)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be finite and in [0, 1]")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        object.__setattr__(self, "probability", probability)


def latest_at_or_before(
    points: Iterable[PricePoint],
    cutoff: datetime,
    *,
    max_staleness: timedelta,
) -> PricePoint:
    """Return the newest point no later than ``cutoff``.

    Future observations are ignored. The selected observation must be no older than
    ``max_staleness``. Conflicting values at the selected timestamp are rejected instead
    of being resolved by input order.
    """

    _require_aware(cutoff, "cutoff")
    cutoff_utc = cutoff.astimezone(UTC)
    if max_staleness < timedelta(0):
        raise ValueError("max_staleness must be non-negative")

    eligible: dict[datetime, float] = {}
    for point in points:
        if not isinstance(point, PricePoint):
            raise TypeError("points must contain PricePoint instances")
        if point.timestamp <= cutoff_utc:
            previous = eligible.get(point.timestamp)
            if previous is not None and previous != point.probability:
                raise ConflictingHistoryPointError(
                    f"conflicting probabilities at {point.timestamp.isoformat()}"
                )
            eligible[point.timestamp] = point.probability

    if not eligible:
        raise NoEligibleHistoryPointError("no history point exists at or before cutoff")

    timestamp = max(eligible)
    staleness = cutoff_utc - timestamp
    if staleness > max_staleness:
        raise StaleHistoryPointError(
            f"latest point is stale by {staleness}; maximum is {max_staleness}"
        )
    return PricePoint(timestamp, eligible[timestamp])


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
