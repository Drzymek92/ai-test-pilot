"""Tests for benchmark/stats.py — Wilson confidence intervals (A/M4)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.stats import fmt_rate_ci, wilson_ci


def test_n_zero_returns_none():
    assert wilson_ci(0, 0) is None
    assert fmt_rate_ci(0, 0) == "— (n=0)"


def test_known_wilson_value_16_of_20():
    # 16/20 = 0.80; the 95% Wilson interval is ~[0.584, 0.919].
    low, high = wilson_ci(16, 20)
    assert low == pytest.approx(0.584, abs=0.005)
    assert high == pytest.approx(0.919, abs=0.005)
    assert low < 0.80 < high


def test_interval_within_unit_range_and_ordered():
    for k, n in [(0, 5), (5, 5), (1, 200), (199, 200), (50, 100)]:
        low, high = wilson_ci(k, n)
        assert 0.0 <= low <= high <= 1.0


def test_extremes_stay_in_bounds():
    # All-killed / none-killed must not produce out-of-range bounds (the Wald failure mode).
    assert wilson_ci(5, 5)[1] <= 1.0
    assert wilson_ci(0, 5)[0] >= 0.0


def test_larger_n_tightens_the_interval():
    lo_small, hi_small = wilson_ci(8, 10)
    lo_big, hi_big = wilson_ci(80, 100)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_out_of_range_raises():
    with pytest.raises(ValueError):
        wilson_ci(6, 5)


def test_fmt_includes_point_and_bounds():
    s = fmt_rate_ci(16, 20)
    assert s.startswith("0.800 [") and "–" in s and s.endswith("]")
