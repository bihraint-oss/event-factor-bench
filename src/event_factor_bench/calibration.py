"""Event-balanced Platt and beta calibration with a deterministic Newton solver."""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

CalibrationMethod = Literal["platt", "beta"]


class CalibrationConvergenceError(RuntimeError):
    """Raised when the fixed Newton procedure cannot produce a converged model."""


@dataclass(frozen=True, slots=True)
class LogisticCalibrator:
    """A fitted binary probability calibrator."""

    method: CalibrationMethod
    intercept: float
    coefficients: tuple[float, ...]
    epsilon: float
    l2: float
    iterations: int
    objective: float

    def predict(self, probabilities: ArrayLike) -> NDArray[np.float64]:
        """Calibrate probabilities using the training-time clipping rule."""

        features = _calibration_features(probabilities, self.method, self.epsilon)
        coefficients = np.asarray(self.coefficients, dtype=np.float64)
        if features.shape[1] != coefficients.size:
            raise RuntimeError("stored coefficient count does not match calibration method")
        result = _sigmoid(self.intercept + features @ coefficients)
        return np.clip(result, self.epsilon, 1.0 - self.epsilon)


def event_balanced_weights(event_ids: Sequence[Hashable]) -> NDArray[np.float64]:
    """Return row weights whose totals are equal across events and sum to one."""

    events = list(event_ids)
    if not events:
        raise ValueError("at least one event is required")
    counts: dict[Hashable, int] = {}
    for event_id in events:
        if not isinstance(event_id, Hashable):
            raise TypeError("event IDs must be hashable")
        counts[event_id] = counts.get(event_id, 0) + 1
    event_weight = 1.0 / len(counts)
    weights = np.asarray([event_weight / counts[event_id] for event_id in events])
    weights.setflags(write=False)
    return weights


def fit_calibrator(
    event_ids: Sequence[Hashable],
    y_true: ArrayLike,
    probabilities: ArrayLike,
    *,
    method: CalibrationMethod,
    epsilon: float = 1e-6,
    l2: float = 1e-3,
    tolerance: float = 1e-9,
    max_iterations: int = 100,
) -> LogisticCalibrator:
    """Fit an event-balanced L2 logistic calibrator by damped Newton steps.

    The intercept is not penalized. Feature coefficients use a fixed, zero-centered L2
    penalty. Hyperparameters are explicit so a frozen benchmark cannot tune them on the
    holdout. Failure to meet the gradient tolerance raises instead of returning a partial fit.
    """

    outcomes, raw_probabilities = _validated_training_arrays(y_true, probabilities)
    events = list(event_ids)
    if len(events) != outcomes.size:
        raise ValueError("event_ids, y_true, and probabilities must have equal lengths")
    weights = event_balanced_weights(events)
    _validate_solver_config(epsilon, l2, tolerance, max_iterations)
    features = _calibration_features(raw_probabilities, method, epsilon)
    design = np.column_stack((np.ones(outcomes.size, dtype=np.float64), features))

    parameters = np.zeros(design.shape[1], dtype=np.float64)
    parameters[1:] = 1.0
    penalty_mask = np.ones_like(parameters)
    penalty_mask[0] = 0.0

    for iteration in range(max_iterations + 1):
        objective, gradient, hessian = _objective_gradient_hessian(
            design, outcomes, weights, parameters, l2, penalty_mask
        )
        if float(np.max(np.abs(gradient))) <= tolerance:
            return _model_from_parameters(method, parameters, epsilon, l2, iteration, objective)
        if iteration == max_iterations:
            break
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as error:
            raise CalibrationConvergenceError("Newton Hessian is singular") from error
        directional_decrease = float(np.dot(gradient, step))
        if not math.isfinite(directional_decrease) or directional_decrease <= 0.0:
            raise CalibrationConvergenceError("Newton direction is not a descent direction")

        step_scale = 1.0
        accepted = False
        for _ in range(40):
            candidate = parameters - step_scale * step
            candidate_objective = _objective(design, outcomes, weights, candidate, l2, penalty_mask)
            if candidate_objective <= objective - 1e-4 * step_scale * directional_decrease:
                parameters = candidate
                accepted = True
                break
            step_scale *= 0.5
        if not accepted:
            raise CalibrationConvergenceError("Newton line search failed")

    raise CalibrationConvergenceError(
        f"calibrator did not converge within {max_iterations} iterations"
    )


def fit_platt_calibrator(
    event_ids: Sequence[Hashable],
    y_true: ArrayLike,
    probabilities: ArrayLike,
    **kwargs: float | int,
) -> LogisticCalibrator:
    """Fit a Platt-style calibrator on the clipped market logit."""

    return fit_calibrator(event_ids, y_true, probabilities, method="platt", **kwargs)


def fit_beta_calibrator(
    event_ids: Sequence[Hashable],
    y_true: ArrayLike,
    probabilities: ArrayLike,
    **kwargs: float | int,
) -> LogisticCalibrator:
    """Fit beta calibration on ``log(p)`` and ``-log(1-p)``."""

    return fit_calibrator(event_ids, y_true, probabilities, method="beta", **kwargs)


def _validated_training_arrays(
    y_true: ArrayLike, probabilities: ArrayLike
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    outcomes = np.asarray(y_true, dtype=np.float64)
    forecasts = np.asarray(probabilities, dtype=np.float64)
    if outcomes.ndim != 1 or forecasts.ndim != 1:
        raise ValueError("y_true and probabilities must be one-dimensional")
    if outcomes.size == 0 or outcomes.shape != forecasts.shape:
        raise ValueError("y_true and probabilities must be non-empty and have equal lengths")
    if not np.all(np.isfinite(outcomes)) or not np.all(np.isin(outcomes, (0.0, 1.0))):
        raise ValueError("y_true must contain only binary 0/1 labels")
    if np.unique(outcomes).size != 2:
        raise ValueError("calibration training requires both outcome classes")
    _validate_probabilities(forecasts)
    return outcomes, forecasts


def _calibration_features(
    probabilities: ArrayLike, method: CalibrationMethod, epsilon: float
) -> NDArray[np.float64]:
    if not 0.0 < epsilon < 0.5:
        raise ValueError("epsilon must lie in (0, 0.5)")
    forecasts = np.asarray(probabilities, dtype=np.float64)
    if forecasts.ndim != 1:
        raise ValueError("probabilities must be one-dimensional")
    _validate_probabilities(forecasts)
    clipped = np.clip(forecasts, epsilon, 1.0 - epsilon)
    if method == "platt":
        return (np.log(clipped) - np.log1p(-clipped))[:, None]
    if method == "beta":
        return np.column_stack((np.log(clipped), -np.log1p(-clipped)))
    raise ValueError(f"unknown calibration method: {method}")


def _validate_probabilities(probabilities: NDArray[np.float64]) -> None:
    if not np.all(np.isfinite(probabilities)):
        raise ValueError("probabilities must contain only finite values")
    if np.any((probabilities < 0.0) | (probabilities > 1.0)):
        raise ValueError("probabilities must lie in [0, 1]")


def _validate_solver_config(
    epsilon: float, l2: float, tolerance: float, max_iterations: int
) -> None:
    if not 0.0 < epsilon < 0.5:
        raise ValueError("epsilon must lie in (0, 0.5)")
    if not math.isfinite(l2) or l2 <= 0.0:
        raise ValueError("l2 must be finite and strictly positive")
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("tolerance must be finite and strictly positive")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 0
    ):
        raise ValueError("max_iterations must be a non-negative integer")


def _objective_gradient_hessian(
    design: NDArray[np.float64],
    outcomes: NDArray[np.float64],
    weights: NDArray[np.float64],
    parameters: NDArray[np.float64],
    l2: float,
    penalty_mask: NDArray[np.float64],
) -> tuple[float, NDArray[np.float64], NDArray[np.float64]]:
    logits = design @ parameters
    fitted = _sigmoid(logits)
    objective = _objective_from_logits(logits, outcomes, weights, parameters, l2, penalty_mask)
    gradient = design.T @ (weights * (fitted - outcomes)) + l2 * penalty_mask * parameters
    curvature = weights * fitted * (1.0 - fitted)
    hessian = design.T @ (design * curvature[:, None])
    hessian += np.diag(l2 * penalty_mask)
    return objective, gradient, hessian


def _objective(
    design: NDArray[np.float64],
    outcomes: NDArray[np.float64],
    weights: NDArray[np.float64],
    parameters: NDArray[np.float64],
    l2: float,
    penalty_mask: NDArray[np.float64],
) -> float:
    return _objective_from_logits(
        design @ parameters, outcomes, weights, parameters, l2, penalty_mask
    )


def _objective_from_logits(
    logits: NDArray[np.float64],
    outcomes: NDArray[np.float64],
    weights: NDArray[np.float64],
    parameters: NDArray[np.float64],
    l2: float,
    penalty_mask: NDArray[np.float64],
) -> float:
    negative_log_likelihood = np.sum(weights * (np.logaddexp(0.0, logits) - outcomes * logits))
    penalty = 0.5 * l2 * float(np.dot(penalty_mask * parameters, parameters))
    return float(negative_log_likelihood + penalty)


def _sigmoid(values: NDArray[np.float64]) -> NDArray[np.float64]:
    result = np.empty_like(values, dtype=np.float64)
    positive = values >= 0.0
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    result[~positive] = exponent / (1.0 + exponent)
    return result


def _model_from_parameters(
    method: CalibrationMethod,
    parameters: NDArray[np.float64],
    epsilon: float,
    l2: float,
    iterations: int,
    objective: float,
) -> LogisticCalibrator:
    if not np.all(np.isfinite(parameters)) or not math.isfinite(objective):
        raise CalibrationConvergenceError("calibration produced non-finite parameters")
    return LogisticCalibrator(
        method=method,
        intercept=float(parameters[0]),
        coefficients=tuple(float(value) for value in parameters[1:]),
        epsilon=epsilon,
        l2=l2,
        iterations=iterations,
        objective=objective,
    )
