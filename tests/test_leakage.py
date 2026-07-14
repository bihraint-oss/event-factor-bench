from datetime import UTC, datetime, timedelta

import pytest

from event_factor_bench.leakage import (
    LeakageError,
    assert_event_splits_disjoint,
    assert_feature_schema_safe,
    assert_feature_times_at_or_before,
    assert_labels_resolved_before,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def test_feature_times_allow_cutoff_equality_without_embargo():
    assert_feature_times_at_or_before([BASE - timedelta(hours=1), BASE], BASE)


def test_feature_times_enforce_embargo_and_reject_future_data():
    with pytest.raises(LeakageError):
        assert_feature_times_at_or_before([BASE], BASE, embargo=timedelta(seconds=1))
    with pytest.raises(LeakageError):
        assert_feature_times_at_or_before([BASE + timedelta(microseconds=1)], BASE)


def test_labels_must_be_strictly_resolved_before_cutoff():
    assert_labels_resolved_before([BASE - timedelta(seconds=1)], BASE)
    with pytest.raises(LeakageError):
        assert_labels_resolved_before([BASE], BASE)
    with pytest.raises(LeakageError):
        assert_labels_resolved_before([BASE - timedelta(hours=1)], BASE, embargo=timedelta(hours=2))


def test_event_splits_must_be_disjoint():
    assert_event_splits_disjoint({"train": ["a", "b"], "test": ["c"]})
    with pytest.raises(LeakageError, match="both"):
        assert_event_splits_disjoint({"train": ["a"], "test": ["a"]})
    with pytest.raises(TypeError):
        assert_event_splits_disjoint({"train": [["unhashable"]]})


@pytest.mark.parametrize(
    "column",
    [
        "outcomePrices",
        "OUTCOME_PRICES",
        "final-volume",
        "liquidityNum",
        "closedTime",
        "winner",
    ],
)
def test_feature_schema_rejects_post_outcome_fields(column):
    with pytest.raises(LeakageError, match="forbidden"):
        assert_feature_schema_safe(["price_momentum", column])


def test_feature_schema_allows_point_in_time_features_and_custom_forbidden_set():
    assert_feature_schema_safe(["price_at_cut", "momentum_24h", "staleness_seconds"])
    with pytest.raises(LeakageError):
        assert_feature_schema_safe(["secret"], forbidden={"secret"})
    with pytest.raises(TypeError):
        assert_feature_schema_safe([1])


def test_time_assertions_validate_lengths_awareness_and_embargo():
    with pytest.raises(ValueError, match="length"):
        assert_feature_times_at_or_before([BASE], [])
    with pytest.raises(ValueError, match="timezone-aware"):
        assert_feature_times_at_or_before([datetime(2026, 1, 1)], BASE)
    with pytest.raises(ValueError, match="non-negative"):
        assert_feature_times_at_or_before([BASE], BASE, embargo=-timedelta(1))
    with pytest.raises(ValueError, match="non-negative"):
        assert_labels_resolved_before([BASE], BASE, embargo=-timedelta(1))
