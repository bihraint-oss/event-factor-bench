"""Paired hierarchical bootstrap over time blocks and event clusters."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """A percentile interval for baseline loss minus model loss."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    n_resamples: int
    seed: int
    replicates: NDArray[np.float64]


def paired_event_block_bootstrap(
    event_ids: Sequence[Hashable],
    block_ids: Sequence[Hashable],
    baseline_losses: ArrayLike,
    model_losses: ArrayLike,
    *,
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 0,
) -> BootstrapResult:
    """Bootstrap a paired event-macro loss improvement.

    Row losses are first averaged within event. Each event must belong to exactly one time
    block. A replicate samples blocks with replacement, then samples events with replacement
    within every sampled block. Positive values mean the model has lower loss.
    """

    events = list(event_ids)
    blocks = list(block_ids)
    baseline = np.asarray(baseline_losses, dtype=np.float64)
    model = np.asarray(model_losses, dtype=np.float64)
    _validate_inputs(events, blocks, baseline, model, n_resamples, confidence, seed)

    row_indices: dict[Hashable, list[int]] = {}
    event_blocks: dict[Hashable, Hashable] = {}
    for index, (event_id, block_id) in enumerate(zip(events, blocks, strict=True)):
        row_indices.setdefault(event_id, []).append(index)
        prior_block = event_blocks.setdefault(event_id, block_id)
        if prior_block != block_id:
            raise ValueError(f"event {event_id!r} appears in more than one block")

    event_differences: dict[Hashable, float] = {
        event_id: float(np.mean(baseline[indices]) - np.mean(model[indices]))
        for event_id, indices in row_indices.items()
    }
    events_by_block: dict[Hashable, list[Hashable]] = {}
    for event_id in row_indices:
        events_by_block.setdefault(event_blocks[event_id], []).append(event_id)
    unique_blocks = list(events_by_block)
    if len(unique_blocks) < 2:
        raise ValueError("at least two time blocks are required")

    estimate = float(np.mean(list(event_differences.values())))
    generator = np.random.default_rng(seed)
    replicates = np.empty(n_resamples, dtype=np.float64)
    for replicate_index in range(n_resamples):
        sampled_differences: list[float] = []
        sampled_block_positions = generator.integers(0, len(unique_blocks), len(unique_blocks))
        for block_position in sampled_block_positions:
            block_events = events_by_block[unique_blocks[int(block_position)]]
            sampled_event_positions = generator.integers(0, len(block_events), len(block_events))
            sampled_differences.extend(
                event_differences[block_events[int(position)]]
                for position in sampled_event_positions
            )
        replicates[replicate_index] = float(np.mean(sampled_differences))

    alpha = 1.0 - confidence
    lower, upper = np.quantile(replicates, [alpha / 2.0, 1.0 - alpha / 2.0])
    replicates.setflags(write=False)
    return BootstrapResult(
        estimate=estimate,
        lower=float(lower),
        upper=float(upper),
        confidence=confidence,
        n_resamples=n_resamples,
        seed=seed,
        replicates=replicates,
    )


def _validate_inputs(
    events: list[Hashable],
    blocks: list[Hashable],
    baseline: NDArray[np.float64],
    model: NDArray[np.float64],
    n_resamples: int,
    confidence: float,
    seed: int,
) -> None:
    if not events:
        raise ValueError("at least one observation is required")
    if baseline.ndim != 1 or model.ndim != 1:
        raise ValueError("loss arrays must be one-dimensional")
    if len(events) != len(blocks) or len(events) != baseline.size or baseline.shape != model.shape:
        raise ValueError("event IDs, block IDs, and loss arrays must have equal lengths")
    if not np.all(np.isfinite(baseline)) or not np.all(np.isfinite(model)):
        raise ValueError("loss arrays must contain only finite values")
    if np.any(baseline < 0.0) or np.any(model < 0.0):
        raise ValueError("loss arrays must be non-negative")
    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int) or n_resamples <= 0:
        raise ValueError("n_resamples must be a positive integer")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must lie in (0, 1)")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    for value in [*events, *blocks]:
        if not isinstance(value, Hashable):
            raise TypeError("event and block IDs must be hashable")
