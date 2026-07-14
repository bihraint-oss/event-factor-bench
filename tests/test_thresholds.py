import math

import pytest

from event_factor_bench.thresholds import parse_threshold


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12, 12.0),
        (-2.5, -2.5),
        ("$1.5M", 1_500_000.0),
        ("above 2,500 thousand", 2_500_000.0),
        ("≥ -3.25k", -3_250.0),
        ("1e3", 1_000.0),
        ("12.5%", 0.125),
        ("25 bps", 0.0025),
        ("£4 billion+", 4_000_000_000.0),
    ],
)
def test_parse_threshold_supported_forms(raw, expected):
    assert parse_threshold(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "no number", "between 10 and 20", "2025-01-01"])
def test_parse_threshold_rejects_missing_or_ambiguous_numbers(raw):
    with pytest.raises(ValueError):
        parse_threshold(raw)


@pytest.mark.parametrize("raw", [True, False])
def test_parse_threshold_rejects_booleans(raw):
    with pytest.raises(TypeError):
        parse_threshold(raw)


def test_parse_threshold_rejects_nonfinite_and_unsupported_types():
    with pytest.raises(ValueError):
        parse_threshold(math.inf)
    with pytest.raises(TypeError):
        parse_threshold(object())


def test_public_package_import_and_percent_suffix_regression():
    import event_factor_bench

    assert event_factor_bench.parse_threshold("50%") == pytest.approx(0.5)
