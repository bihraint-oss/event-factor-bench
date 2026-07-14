"""Event-balanced proper scoring rules."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

LossName = Literal["brier", "log"]


def event_macro_brier(
    event_ids: Sequence[Hashable], y_true: ArrayLike, probabilities: ArrayLike
) -> float:
    """Return Brier loss with equal weight for each event."""

    return float(np.mean(event_level_losses(event_ids, y_true, probabilities, loss="brier")))


def event_macro_log_loss(
    event_ids: Sequence[Hashable],
    y_true: ArrayLike,
    probabilities: ArrayLike,
    *,
    epsilon: float = 1e-6,
) -> float:
    """Return clipped binary log loss with equal weight for each event."""

    return float(
        np.mean(
            event_level_losses(
                event_ids,
                y_true,
                probabilities,
                loss="log",
                epsilon=epsilon,
            )
        )
    )


def event_level_losses(
    event_ids: Sequence[Hashable],
    y_true: ArrayLike,
    probabilities: ArrayLike,
    *,
    loss: LossName,
    epsilon: float = 1e-6,
) -> NDArray[np.float64]:
    """Return within-event mean losses in first-seen event order."""

    events, outcomes, forecasts = _validated_inputs(event_ids, y_true, probabilities)
    if loss == "brier":
        row_losses = np.square(forecasts - outcomes)
    elif loss == "log":
        if not 0.0 < epsilon < 0.5:
            raise ValueError("epsilon must lie in (0, 0.5)")
        clipped = np.clip(forecasts, epsilon, 1.0 - epsilon)
        row_losses = -(outcomes * np.log(clipped) + (1.0 - outcomes) * np.log1p(-clipped))
    else:
        raise ValueError(f"unknown loss: {loss}")

    indices: dict[Hashable, list[int]] = {}
    for index, event_id in enumerate(events):
        indices.setdefault(event_id, []).append(index)
    return np.asarray(
        [float(np.mean(row_losses[event_indices])) for event_indices in indices.values()],
        dtype=np.float64,
    )


def _validated_inputs(
    event_ids: Sequence[Hashable], y_true: ArrayLike, probabilities: ArrayLike
) -> tuple[list[Hashable], NDArray[np.float64], NDArray[np.float64]]:
    events = list(event_ids)
    outcomes = np.asarray(y_true, dtype=np.float64)
    forecasts = np.asarray(probabilities, dtype=np.float64)
    if outcomes.ndim != 1 or forecasts.ndim != 1:
        raise ValueError("y_true and probabilities must be one-dimensional")
    if not events:
        raise ValueError("at least one observation is required")
    if len(events) != outcomes.size or outcomes.shape != forecasts.shape:
        raise ValueError("event_ids, y_true, and probabilities must have equal lengths")
    for event_id in events:
        if not isinstance(event_id, Hashable):
            raise TypeError("event IDs must be hashable")
    if not np.all(np.isfinite(outcomes)) or not np.all(np.isin(outcomes, (0.0, 1.0))):
        raise ValueError("y_true must contain only binary 0/1 labels")
    if not np.all(np.isfinite(forecasts)) or np.any((forecasts < 0.0) | (forecasts > 1.0)):
        raise ValueError("probabilities must be finite and lie in [0, 1]")
    return events, outcomes, forecasts
