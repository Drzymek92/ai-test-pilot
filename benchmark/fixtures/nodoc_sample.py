"""No-docstring, hard-to-predict-from-source corpus — the golden-mode stress test.

Unlike the simple extractors (where the LLM nails exact assertions by reading the source), these
functions are pure but their EXACT output is genuinely error-prone to compute by eye (position
weighting, Horner evaluation, the Luhn doubling rule). That gap is exactly where golden /
characterization mode should earn its keep: it RUNS the function and locks the REAL result, so the
assertion is exact even when the model would otherwise mispredict (failing the green baseline) or
under-assert. No docstrings on purpose, so without golden the no-docstring guard applies.

By design every loop is BOUNDED (for-loops / arithmetic, no `while`): a mutation changes the computed
VALUE, never termination. That keeps the signal about ASSERTION STRENGTH (the golden question) rather
than infinite-loop mutants that any variant kills by timeout. All pure + deterministic so golden can
probe them safely.
"""
from __future__ import annotations


def _luhn_checksum(digits):
    total = 0
    for idx, d in enumerate(reversed(digits)):
        if idx % 2 == 0:
            total += d
        else:
            doubled = d * 2
            total += doubled - 9 if doubled > 9 else doubled
    return (10 - total % 10) % 10


def _weighted_checksum(nums):
    total = 0
    for i, n in enumerate(nums):
        total += (i + 1) * n
    return total % 97


def _poly_eval(coeffs, x):
    acc = 0
    for c in coeffs:
        acc = acc * x + c
    return acc


def _alternating_sum(nums):
    total = 0
    sign = 1
    for n in nums:
        total += sign * n
        sign = -sign
    return total


def _grade_points(scores):
    points = 0
    for s in scores:
        if s >= 90:
            points += 4
        elif s >= 80:
            points += 3
        elif s >= 70:
            points += 2
        elif s >= 60:
            points += 1
    return points
