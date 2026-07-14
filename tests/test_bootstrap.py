import numpy as np
import pytest

from event_factor_bench.bootstrap import paired_event_block_bootstrap


def test_paired_bootstrap_is_deterministic_and_detects_uniform_gain():
    events = [f"e{i}" for i in range(12)]
    blocks = [f"b{i // 3}" for i in range(12)]
    baseline = np.full(12, 0.30)
    model = np.full(12, 0.20)
    first = paired_event_block_bootstrap(events, blocks, baseline, model, n_resamples=500, seed=17)
    second = paired_event_block_bootstrap(events, blocks, baseline, model, n_resamples=500, seed=17)
    assert first.estimate == pytest.approx(0.1)
    assert first.lower == pytest.approx(0.1)
    assert first.upper == pytest.approx(0.1)
    np.testing.assert_array_equal(first.replicates, second.replicates)
    assert not first.replicates.flags.writeable


def test_paired_bootstrap_aggregates_rows_within_event_before_resampling():
    result = paired_event_block_bootstrap(
        event_ids=["a", "a", "b", "c"],
        block_ids=["x", "x", "y", "y"],
        baseline_losses=[0.4, 0.2, 0.5, 0.4],
        model_losses=[0.2, 0.2, 0.4, 0.5],
        n_resamples=100,
        seed=1,
    )
    # Event differences are 0.1, 0.1, -0.1, hence equal-event estimate 1/30.
    assert result.estimate == pytest.approx(1.0 / 30.0)


def test_paired_bootstrap_rejects_event_crossing_blocks():
    with pytest.raises(ValueError, match="more than one block"):
        paired_event_block_bootstrap(
            ["a", "a", "b"],
            ["x", "y", "y"],
            [0.3, 0.3, 0.3],
            [0.2, 0.2, 0.2],
            n_resamples=10,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_resamples": 0},
        {"n_resamples": True},
        {"confidence": 1.0},
        {"seed": -1},
        {"seed": True},
    ],
)
def test_paired_bootstrap_validates_configuration(kwargs):
    with pytest.raises(ValueError):
        paired_event_block_bootstrap(["a", "b"], ["x", "y"], [0.3, 0.3], [0.2, 0.2], **kwargs)


def test_paired_bootstrap_validates_data():
    with pytest.raises(ValueError, match="two time blocks"):
        paired_event_block_bootstrap(["a", "b"], ["x", "x"], [0.3, 0.3], [0.2, 0.2], n_resamples=10)
    with pytest.raises(ValueError, match="equal lengths"):
        paired_event_block_bootstrap(["a", "b"], ["x"], [0.3, 0.3], [0.2, 0.2], n_resamples=10)
    with pytest.raises(ValueError, match="finite"):
        paired_event_block_bootstrap(
            ["a", "b"], ["x", "y"], [0.3, np.nan], [0.2, 0.2], n_resamples=10
        )
    with pytest.raises(ValueError, match="non-negative"):
        paired_event_block_bootstrap(
            ["a", "b"], ["x", "y"], [0.3, -0.1], [0.2, 0.2], n_resamples=10
        )
    with pytest.raises(TypeError, match="hashable"):
        paired_event_block_bootstrap(
            [["a"], "b"], ["x", "y"], [0.3, 0.3], [0.2, 0.2], n_resamples=10
        )
