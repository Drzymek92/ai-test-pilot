"""Tests for equivalent-mutant detection (benchmark/equivalence.py) — offline differential testing."""
from __future__ import annotations

from pathlib import Path

from benchmark import equivalence

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_literal_filter():
    assert equivalence._is_literal({"a": [1, 2], "b": "x"})
    assert not equivalence._is_literal({"$type": "T", "args": {}})
    assert not equivalence._is_literal([{"$call": "Decimal", "args": ["1"]}])


def test_fuzz_inputs_same_shape_and_bounded():
    samples = equivalence.fuzz_inputs({"x": 3, "xs": [1, 2, 3]}, cap=12)
    assert {"x": 3, "xs": [1, 2, 3]} in samples          # base included
    assert len(samples) <= 12
    assert all(isinstance(s["x"], int) and isinstance(s["xs"], list) for s in samples)


def test_equivalent_mutant_detected(tmp_path: Path):
    correct = "def f(x):\n    return x - 0\n"
    mutant = "def f(x):\n    return x + 0\n"            # x-0 == x+0 for all x → equivalent
    samples = [{"x": v} for v in (0, 1, -1, 7, 100)]
    verdict = equivalence.is_equivalent(correct, mutant, "f", samples,
                                        work_dir=tmp_path, project_root=_PROJECT_ROOT)
    assert verdict == "equivalent"


def test_distinct_mutant_detected(tmp_path: Path):
    correct = "def f(x):\n    return x + 1\n"
    mutant = "def f(x):\n    return x - 1\n"            # differs everywhere → distinct
    samples = [{"x": v} for v in (0, 1, -1, 7, 100)]
    verdict = equivalence.is_equivalent(correct, mutant, "f", samples,
                                        work_dir=tmp_path, project_root=_PROJECT_ROOT)
    assert verdict == "distinct"


def test_no_samples_is_unknown(tmp_path: Path):
    verdict = equivalence.is_equivalent("def f():\n    return 1\n", "def f():\n    return 2\n",
                                        "f", [], work_dir=tmp_path, project_root=_PROJECT_ROOT)
    assert verdict == "unknown"
