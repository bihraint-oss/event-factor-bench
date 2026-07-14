"""Shape-constrained projections for threshold-event probabilities."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray


def project_nonincreasing(
    values: ArrayLike,
    *,
    weights: ArrayLike | None = None,
) -> NDArray[np.float64]:
    """Weighted least-squares projection onto ``x[0] >= ... >= x[n-1]``.

    The implementation is the pool-adjacent-violators algorithm. Positive weights are
    required; an empty input produces an empty output.
    """

    observations = _as_vector(values, "values")
    if weights is None:
        sample_weights = np.ones_like(observations)
    else:
        sample_weights = _as_vector(weights, "weights")
        if sample_weights.shape != observations.shape:
            raise ValueError("weights must have the same shape as values")
        if np.any(sample_weights <= 0.0):
            raise ValueError("weights must be strictly positive")
    if observations.size == 0:
        return observations.copy()

    means: list[float] = []
    block_weights: list[float] = []
    starts: list[int] = []
    ends: list[int] = []

    for index, (value, weight) in enumerate(zip(observations, sample_weights, strict=True)):
        means.append(float(value))
        block_weights.append(float(weight))
        starts.append(index)
        ends.append(index + 1)
        while len(means) >= 2 and means[-2] < means[-1]:
            combined_weight = block_weights[-2] + block_weights[-1]
            combined_mean = (
                means[-2] * block_weights[-2] + means[-1] * block_weights[-1]
            ) / combined_weight
            means[-2:] = [combined_mean]
            block_weights[-2:] = [combined_weight]
            ends[-2:] = [ends[-1]]
            starts.pop()

    projected = np.empty_like(observations)
    for mean, start, end in zip(means, starts, ends, strict=True):
        projected[start:end] = mean
    return projected


def project_threshold_probabilities(
    thresholds: Sequence[float] | ArrayLike,
    probabilities: ArrayLike,
    *,
    weights: ArrayLike | None = None,
) -> NDArray[np.float64]:
    """Project probabilities to decrease as numeric thresholds increase.

    The returned array follows the caller's original order. Duplicate thresholds are
    rejected because their ordering constraint is not identifiable without aggregation.
    """

    threshold_values = _as_vector(thresholds, "thresholds")
    probability_values = _as_vector(probabilities, "probabilities")
    if threshold_values.shape != probability_values.shape:
        raise ValueError("thresholds and probabilities must have the same shape")
    if np.any((probability_values < 0.0) | (probability_values > 1.0)):
        raise ValueError("probabilities must lie in [0, 1]")
    if np.unique(threshold_values).size != threshold_values.size:
        raise ValueError("thresholds must be unique")

    weight_values: NDArray[np.float64] | None = None
    if weights is not None:
        weight_values = _as_vector(weights, "weights")
        if weight_values.shape != threshold_values.shape:
            raise ValueError("weights must have the same shape as thresholds")

    order = np.argsort(threshold_values, kind="stable")
    sorted_weights = None if weight_values is None else weight_values[order]
    sorted_projection = project_nonincreasing(probability_values[order], weights=sorted_weights)
    result = np.empty_like(sorted_projection)
    result[order] = sorted_projection
    return result


def _as_vector(values: ArrayLike, name: str) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array
