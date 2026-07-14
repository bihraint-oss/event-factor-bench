"""Mathematical core for EventFactorBench."""

from event_factor_bench.bootstrap import BootstrapResult, paired_event_block_bootstrap
from event_factor_bench.calibration import (
    CalibrationConvergenceError,
    LogisticCalibrator,
    event_balanced_weights,
    fit_beta_calibrator,
    fit_calibrator,
    fit_platt_calibrator,
)
from event_factor_bench.history import (
    ConflictingHistoryPointError,
    NoEligibleHistoryPointError,
    PricePoint,
    StaleHistoryPointError,
    latest_at_or_before,
)
from event_factor_bench.leakage import (
    DEFAULT_FORBIDDEN_FEATURES,
    LeakageError,
    assert_event_splits_disjoint,
    assert_feature_schema_safe,
    assert_feature_times_at_or_before,
    assert_labels_resolved_before,
)
from event_factor_bench.metrics import event_macro_brier, event_macro_log_loss
from event_factor_bench.projection import (
    project_nonincreasing,
    project_threshold_probabilities,
)
from event_factor_bench.thresholds import parse_threshold

__all__ = [
    "DEFAULT_FORBIDDEN_FEATURES",
    "BootstrapResult",
    "CalibrationConvergenceError",
    "ConflictingHistoryPointError",
    "LeakageError",
    "LogisticCalibrator",
    "NoEligibleHistoryPointError",
    "PricePoint",
    "StaleHistoryPointError",
    "assert_event_splits_disjoint",
    "assert_feature_schema_safe",
    "assert_feature_times_at_or_before",
    "assert_labels_resolved_before",
    "event_balanced_weights",
    "event_macro_brier",
    "event_macro_log_loss",
    "fit_beta_calibrator",
    "fit_calibrator",
    "fit_platt_calibrator",
    "latest_at_or_before",
    "paired_event_block_bootstrap",
    "parse_threshold",
    "project_nonincreasing",
    "project_threshold_probabilities",
]
