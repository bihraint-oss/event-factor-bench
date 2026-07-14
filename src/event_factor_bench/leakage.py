"""Fail-closed assertions for point-in-time benchmark construction."""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta


class LeakageError(AssertionError):
    """Raised when benchmark data violates a point-in-time invariant."""


DEFAULT_FORBIDDEN_FEATURES = frozenset(
    {
        "best_ask",
        "best_bid",
        "closed_time",
        "current_description",
        "current_tags",
        "description",
        "final_liquidity",
        "final_volume",
        "label",
        "last_trade_price",
        "liquidity",
        "liquidity_num",
        "outcome_prices",
        "resolved_outcome",
        "tags",
        "target",
        "uma_end_date",
        "volume",
        "volume_num",
        "winner",
    }
)


def assert_feature_times_at_or_before(
    feature_times: Sequence[datetime],
    cutoffs: Sequence[datetime] | datetime,
    *,
    embargo: timedelta = timedelta(0),
) -> None:
    """Assert every feature timestamp is no later than its embargoed cutoff."""

    times = list(feature_times)
    cutoff_values = _broadcast_datetimes(cutoffs, len(times), "cutoffs")
    if embargo < timedelta(0):
        raise ValueError("embargo must be non-negative")
    for index, (feature_time, cutoff) in enumerate(zip(times, cutoff_values, strict=True)):
        feature_utc = _as_utc(feature_time, f"feature_times[{index}]")
        cutoff_utc = _as_utc(cutoff, f"cutoffs[{index}]")
        latest_allowed = cutoff_utc - embargo
        if feature_utc > latest_allowed:
            raise LeakageError(
                f"feature_times[{index}]={feature_utc.isoformat()} exceeds "
                f"embargoed cutoff {latest_allowed.isoformat()}"
            )


def assert_labels_resolved_before(
    resolution_times: Sequence[datetime],
    prediction_cutoffs: Sequence[datetime] | datetime,
    *,
    embargo: timedelta = timedelta(0),
) -> None:
    """Assert training labels were strictly available before prediction cutoffs."""

    resolutions = list(resolution_times)
    cutoffs = _broadcast_datetimes(prediction_cutoffs, len(resolutions), "prediction_cutoffs")
    if embargo < timedelta(0):
        raise ValueError("embargo must be non-negative")
    for index, (resolved_at, cutoff) in enumerate(zip(resolutions, cutoffs, strict=True)):
        resolved_utc = _as_utc(resolved_at, f"resolution_times[{index}]")
        latest_allowed = _as_utc(cutoff, f"prediction_cutoffs[{index}]") - embargo
        if resolved_utc >= latest_allowed:
            raise LeakageError(
                f"resolution_times[{index}]={resolved_utc.isoformat()} was not strictly "
                f"before {latest_allowed.isoformat()}"
            )


def assert_event_splits_disjoint(splits: Mapping[str, Iterable[Hashable]]) -> None:
    """Assert no event group occurs in more than one data split."""

    owner: dict[Hashable, str] = {}
    for split_name, event_ids in splits.items():
        for event_id in event_ids:
            if not isinstance(event_id, Hashable):
                raise TypeError("event IDs must be hashable")
            previous = owner.setdefault(event_id, split_name)
            if previous != split_name:
                raise LeakageError(
                    f"event {event_id!r} appears in both {previous!r} and {split_name!r}"
                )


def assert_feature_schema_safe(
    columns: Iterable[str],
    *,
    forbidden: Iterable[str] = DEFAULT_FORBIDDEN_FEATURES,
) -> None:
    """Reject label and post-outcome fields from a model feature matrix."""

    normalized_forbidden = {_normalize_column(name) for name in forbidden}
    bad_columns: list[str] = []
    for column in columns:
        if not isinstance(column, str):
            raise TypeError("feature column names must be strings")
        if _normalize_column(column) in normalized_forbidden:
            bad_columns.append(column)
    if bad_columns:
        raise LeakageError(f"forbidden feature columns: {sorted(bad_columns)!r}")


def _broadcast_datetimes(
    values: Sequence[datetime] | datetime, expected_length: int, name: str
) -> list[datetime]:
    if isinstance(values, datetime):
        return [values] * expected_length
    result = list(values)
    if len(result) != expected_length:
        raise ValueError(f"{name} must have length {expected_length}")
    return result


def _as_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_column(name: str) -> str:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).replace("-", "_")
    return re.sub(r"_+", "_", snake).lower().strip("_")
