import numpy as np
import pytest

from event_factor_bench.projection import (
    project_nonincreasing,
    project_threshold_probabilities,
)


def test_pav_known_unweighted_solution():
    projected = project_nonincreasing([0.8, 0.2, 0.6, 0.1])
    np.testing.assert_allclose(projected, [0.8, 0.4, 0.4, 0.1])


def test_pav_known_weighted_solution():
    projected = project_nonincreasing([0.9, 0.2, 0.8], weights=[1.0, 3.0, 1.0])
    np.testing.assert_allclose(projected, [0.9, 0.35, 0.35])


def test_pav_is_monotone_idempotent_and_preserves_weighted_sum():
    rng = np.random.default_rng(42)
    for _ in range(50):
        values = rng.uniform(-1.0, 2.0, size=25)
        weights = rng.uniform(0.1, 3.0, size=25)
        projected = project_nonincreasing(values, weights=weights)
        assert np.all(np.diff(projected) <= 1e-12)
        np.testing.assert_allclose(
            project_nonincreasing(projected, weights=weights), projected, atol=1e-12
        )
        assert np.dot(weights, projected) == pytest.approx(np.dot(weights, values))


def test_pav_does_not_move_an_already_feasible_vector():
    values = np.asarray([1.0, 0.8, 0.8, 0.2])
    np.testing.assert_array_equal(project_nonincreasing(values), values)


def test_threshold_projection_sorts_then_restores_original_order():
    projected = project_threshold_probabilities(
        thresholds=[30.0, 10.0, 20.0], probabilities=[0.5, 0.7, 0.8]
    )
    # Sorted by threshold: probabilities [0.7, 0.8, 0.5] -> [0.75, 0.75, 0.5].
    np.testing.assert_allclose(projected, [0.5, 0.75, 0.75])


def test_projection_validates_shapes_values_and_weights():
    assert project_nonincreasing([]).size == 0
    with pytest.raises(ValueError):
        project_nonincreasing([], weights=[1.0])
    with pytest.raises(ValueError):
        project_nonincreasing([[1.0]])
    with pytest.raises(ValueError):
        project_nonincreasing([1.0, np.nan])
    with pytest.raises(ValueError):
        project_nonincreasing([1.0, 0.0], weights=[1.0])
    with pytest.raises(ValueError):
        project_nonincreasing([1.0], weights=[0.0])
    with pytest.raises(ValueError):
        project_threshold_probabilities([1.0, 1.0], [0.4, 0.5])
    with pytest.raises(ValueError):
        project_threshold_probabilities([1.0], [1.1])
    with pytest.raises(ValueError):
        project_threshold_probabilities([1.0, 2.0], [0.4])
