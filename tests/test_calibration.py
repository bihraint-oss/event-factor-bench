import numpy as np
import pytest

from event_factor_bench.calibration import (
    CalibrationConvergenceError,
    LogisticCalibrator,
    event_balanced_weights,
    fit_beta_calibrator,
    fit_calibrator,
    fit_platt_calibrator,
)


def test_event_balanced_weights_give_every_event_equal_total_weight():
    weights = event_balanced_weights(["large", "large", "large", "small"])
    np.testing.assert_allclose(weights, [1 / 6, 1 / 6, 1 / 6, 1 / 2])
    assert np.sum(weights) == pytest.approx(1.0)
    assert not weights.flags.writeable


@pytest.mark.parametrize("fit", [fit_platt_calibrator, fit_beta_calibrator])
def test_calibrators_fit_finite_ordered_probabilities(fit):
    probabilities = np.asarray([0.05, 0.15, 0.25, 0.40, 0.60, 0.75, 0.85, 0.95])
    outcomes = np.asarray([0, 0, 0, 0, 1, 1, 1, 1])
    events = [f"e{index}" for index in range(probabilities.size)]
    model = fit(events, outcomes, probabilities, l2=1e-2)
    calibrated = model.predict(probabilities)
    assert np.all(np.isfinite(calibrated))
    assert np.all((calibrated > 0.0) & (calibrated < 1.0))
    assert np.all(np.diff(calibrated) > 0.0)
    assert model.iterations > 0
    assert np.isfinite(model.objective)


def test_beta_calibration_clips_endpoint_inputs_consistently():
    model = fit_beta_calibrator(
        ["a", "b", "c", "d"],
        [0, 0, 1, 1],
        [0.0, 0.2, 0.8, 1.0],
        epsilon=1e-4,
        l2=0.1,
    )
    predictions = model.predict([0.0, 1.0])
    assert np.all(np.isfinite(predictions))
    assert np.all(predictions >= 1e-4)
    assert np.all(predictions <= 1.0 - 1e-4)
    assert len(model.coefficients) == 2


def test_duplicate_rows_inside_one_event_do_not_change_fit():
    original = fit_platt_calibrator(
        ["a", "b", "c", "d"], [0, 0, 1, 1], [0.1, 0.3, 0.7, 0.9], l2=0.05
    )
    duplicated = fit_platt_calibrator(
        ["a", "a", "a", "b", "c", "d"],
        [0, 0, 0, 0, 1, 1],
        [0.1, 0.1, 0.1, 0.3, 0.7, 0.9],
        l2=0.05,
    )
    assert duplicated.intercept == pytest.approx(original.intercept, abs=1e-10)
    assert duplicated.coefficients == pytest.approx(original.coefficients, abs=1e-10)


def test_fixed_l2_shrinks_no_signal_platt_slope_to_zero():
    model = fit_platt_calibrator(["a", "b", "c", "d"], [0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5], l2=0.5)
    assert model.intercept == pytest.approx(0.0, abs=1e-10)
    assert model.coefficients[0] == pytest.approx(0.0, abs=1e-10)
    np.testing.assert_allclose(model.predict([0.1, 0.9]), [0.5, 0.5], atol=1e-10)


def test_fit_fails_closed_when_iteration_budget_is_exhausted():
    with pytest.raises(CalibrationConvergenceError, match="did not converge"):
        fit_platt_calibrator(
            ["a", "b", "c", "d"],
            [0, 0, 1, 1],
            [0.1, 0.2, 0.8, 0.9],
            max_iterations=0,
            tolerance=1e-15,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"epsilon": 0.0},
        {"epsilon": 0.5},
        {"l2": 0.0},
        {"l2": float("inf")},
        {"tolerance": 0.0},
        {"max_iterations": -1},
        {"max_iterations": True},
    ],
)
def test_fit_validates_solver_configuration(kwargs):
    with pytest.raises(ValueError):
        fit_platt_calibrator(["a", "b"], [0, 1], [0.2, 0.8], **kwargs)


@pytest.mark.parametrize(
    ("events", "outcomes", "probabilities"),
    [
        ([], [], []),
        (["a"], [0, 1], [0.2, 0.8]),
        (["a", "b"], [0, 0], [0.2, 0.8]),
        (["a", "b"], [0, 2], [0.2, 0.8]),
        (["a", "b"], [0, 1], [-0.1, 0.8]),
        (["a", "b"], [0, 1], [0.2, np.nan]),
        (["a", "b"], [[0], [1]], [0.2, 0.8]),
    ],
)
def test_fit_validates_training_data(events, outcomes, probabilities):
    with pytest.raises(ValueError):
        fit_platt_calibrator(events, outcomes, probabilities)


def test_fit_rejects_unhashable_events_and_unknown_method():
    with pytest.raises(TypeError):
        fit_platt_calibrator([["a"], "b"], [0, 1], [0.2, 0.8])
    with pytest.raises(ValueError, match="unknown calibration method"):
        fit_calibrator(["a", "b"], [0, 1], [0.2, 0.8], method="unknown")


def test_model_predict_validates_probabilities_and_stored_dimension():
    model = fit_platt_calibrator(["a", "b", "c", "d"], [0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9])
    with pytest.raises(ValueError):
        model.predict([1.1])
    malformed = LogisticCalibrator("platt", 0.0, (1.0, 2.0), 1e-6, 0.1, 1, 0.5)
    with pytest.raises(RuntimeError, match="coefficient count"):
        malformed.predict([0.5])
