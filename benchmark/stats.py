"""Small statistics helpers for the reliability evals (A/M4).

Kill rates are binomial proportions over a finite mutant set, so a bare point estimate (e.g. "0.80")
overstates precision at the subset sizes we run (n=20 → ±~0.17). We report a **Wilson score interval**,
which is well-behaved for small n and near-0/1 proportions (unlike the normal approximation). Stdlib
only — no scipy/numpy dependency.
"""
from __future__ import annotations

import math

# 95% two-sided normal quantile (z_{0.975}); the default confidence level for all reported intervals.
Z_95 = 1.959963984540054


def wilson_ci(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion `successes/n`.

    Returns (low, high) rounded to 3 dp, clamped to [0, 1]; `None` when `n == 0` (no estimate).
    Wilson is chosen over the normal (Wald) approximation because it stays inside [0,1] and keeps
    sensible coverage for small n and proportions near 0 or 1 — exactly the kill-rate regime here.
    """
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} out of range for n={n}")
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (round(low, 3), round(high, 3))


def fmt_rate_ci(successes: int, n: int) -> str:
    """Human string: ``0.800 [0.584–0.919]`` (or ``— (n=0)`` when there's nothing to estimate)."""
    if n <= 0:
        return "— (n=0)"
    ci = wilson_ci(successes, n)
    return f"{successes / n:.3f} [{ci[0]:.3f}–{ci[1]:.3f}]"
