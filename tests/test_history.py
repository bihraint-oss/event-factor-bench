from datetime import UTC, datetime, timedelta, timezone

import pytest

from event_factor_bench.history import (
    ConflictingHistoryPointError,
    NoEligibleHistoryPointError,
    PricePoint,
    StaleHistoryPointError,
    latest_at_or_before,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def point(hours: int, probability: float) -> PricePoint:
    return PricePoint(BASE + timedelta(hours=hours), probability)


def test_latest_at_or_before_is_order_independent_and_allows_exact_cutoff():
    selected = latest_at_or_before(
        [point(3, 0.7), point(-1, 0.2), point(2, 0.6), point(3, 0.7)],
        BASE + timedelta(hours=3),
        max_staleness=timedelta(0),
    )
    assert selected == point(3, 0.7)


def test_latest_at_or_before_ignores_future_points():
    selected = latest_at_or_before(
        [point(4, 0.9), point(1, 0.4)],
        BASE + timedelta(hours=2),
        max_staleness=timedelta(hours=1),
    )
    assert selected == point(1, 0.4)


def test_latest_at_or_before_normalizes_timezones():
    eastern = timezone(timedelta(hours=-5))
    equivalent_cutoff = datetime(2025, 12, 31, 19, tzinfo=eastern)
    selected = latest_at_or_before(
        [PricePoint(equivalent_cutoff, 0.3)],
        BASE,
        max_staleness=timedelta(0),
    )
    assert selected.timestamp == BASE


def test_latest_at_or_before_rejects_stale_or_missing_history():
    with pytest.raises(StaleHistoryPointError):
        latest_at_or_before(
            [point(0, 0.3)],
            BASE + timedelta(hours=2),
            max_staleness=timedelta(hours=1),
        )
    with pytest.raises(NoEligibleHistoryPointError):
        latest_at_or_before([point(1, 0.3)], BASE, max_staleness=timedelta(hours=1))


def test_latest_at_or_before_rejects_conflicting_duplicates():
    with pytest.raises(ConflictingHistoryPointError):
        latest_at_or_before([point(0, 0.2), point(0, 0.3)], BASE, max_staleness=timedelta(0))


def test_history_validates_probabilities_times_and_arguments():
    with pytest.raises(ValueError):
        PricePoint(BASE, 1.1)
    with pytest.raises(TypeError):
        PricePoint(BASE, True)
    with pytest.raises(ValueError):
        PricePoint(datetime(2026, 1, 1), 0.5)
    with pytest.raises(ValueError):
        latest_at_or_before([point(0, 0.5)], BASE, max_staleness=-timedelta(1))
    with pytest.raises(TypeError):
        latest_at_or_before([object()], BASE, max_staleness=timedelta(0))
