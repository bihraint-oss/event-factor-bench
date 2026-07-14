import math

import numpy as np
import pytest

from event_factor_bench.metrics import (
    event_level_losses,
    event_macro_brier,
    event_macro_log_loss,
)


def test_event_macro_brier_weights_events_equally():
    events = ["large", "large", "large", "small"]
    outcomes = [1, 1, 1, 0]
    probabilities = [1.0, 1.0, 1.0, 1.0]
    assert event_macro_brier(events, outcomes, probabilities) == pytest.approx(0.5)
    assert np.mean((np.asarray(probabilities) - outcomes) ** 2) == pytest.approx(0.25)


def test_event_macro_log_loss_clips_both_extremes_symmetrically():
    loss = event_macro_log_loss(["a", "b"], [1, 0], [0.0, 1.0], epsilon=1e-3)
    assert loss == pytest.approx(-math.log(1e-3))


def test_event_level_losses_preserve_first_seen_event_order():
    losses = event_level_losses(["b", "a", "b"], [1, 0, 0], [0.5, 0.0, 0.0], loss="brier")
    np.testing.assert_allclose(losses, [(0.25 + 0.0) / 2.0, 0.0])


@pytest.mark.parametrize(
    ("events", "outcomes", "probabilities"),
    [
        ([], [], []),
        (["a"], [0, 1], [0.5]),
        (["a"], [2], [0.5]),
        (["a"], [1], [1.1]),
        (["a"], [[1]], [0.5]),
    ],
)
def test_metrics_reject_invalid_inputs(events, outcomes, probabilities):
    with pytest.raises(ValueError):
        event_macro_brier(events, outcomes, probabilities)


def test_metrics_reject_invalid_log_configuration_and_unknown_loss():
    with pytest.raises(ValueError):
        event_macro_log_loss(["a"], [1], [0.5], epsilon=0.5)
    with pytest.raises(ValueError):
        event_level_losses(["a"], [1], [0.5], loss="other")
    with pytest.raises(TypeError):
        event_macro_brier([["unhashable"]], [1], [0.5])
