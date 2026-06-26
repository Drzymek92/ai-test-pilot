"""Tests for the bug-detection harness (scripts/core/detection.py) — fully offline.

The LLM/generation path is NOT exercised here; we build a real suite + target by hand and test the
core mechanic (green baseline -> swap buggy source -> re-run -> kill check) against the real pytest
runner, plus the regression-gate + ablation-delta helpers.
"""
from __future__ import annotations

from pathlib import Path

from scripts.core import detection, registry
from scripts.core.models import ScenarioSet, TargetRef, TestScenario

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CORRECT = "def add(a, b):\n    return a + b\n"


def _suite(tmp_path: Path) -> tuple[object, ScenarioSet, Path, Path]:
    """A real target (add.py) + a real generated-style test file that imports + asserts it."""
    target = tmp_path / "add.py"
    target.write_text(_CORRECT, encoding="utf-8")
    scenario = TestScenario(id="k1", title="add 2+3", unit="add", expected="5")
    adapter = registry.get_adapter("python_pytest")
    fname = adapter.test_function_name(scenario)              # test_k1
    test_file = tmp_path / "test_add_gen.py"
    test_file.write_text(
        "import sys\n"
        f"sys.path.insert(0, {tmp_path.as_posix()!r})\n"
        "from add import add\n\n\n"
        f"def {fname}():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    scenario_set = ScenarioSet(target=TargetRef(adapter="python_pytest", locator=str(target)),
                               scenarios=[scenario])
    return adapter, scenario_set, test_file, target


def test_green_baseline_passes_on_correct(tmp_path: Path):
    adapter, ss, test_file, _ = _suite(tmp_path)
    green = detection.green_baseline(adapter, test_file, ss, _PROJECT_ROOT, 15.0)
    assert green == {"k1"}


def test_behaviour_changing_bug_is_killed(tmp_path: Path):
    adapter, ss, test_file, target = _suite(tmp_path)
    green = detection.green_baseline(adapter, test_file, ss, _PROJECT_ROOT, 15.0)
    buggy = "def add(a, b):\n    return a - b\n"             # add(2,3) -> -1, breaks the assert
    killer = detection._is_killed(adapter, test_file, ss, target, _CORRECT, buggy,
                                  _PROJECT_ROOT, green, 15.0)
    assert killer == "k1"
    assert target.read_text(encoding="utf-8") == _CORRECT     # source ALWAYS restored


def test_behaviour_preserving_mutant_survives(tmp_path: Path):
    adapter, ss, test_file, target = _suite(tmp_path)
    green = detection.green_baseline(adapter, test_file, ss, _PROJECT_ROOT, 15.0)
    equivalent = "def add(a, b):\n    return b + a\n"         # commutative — add(2,3) still 5
    killer = detection._is_killed(adapter, test_file, ss, target, _CORRECT, equivalent,
                                  _PROJECT_ROOT, green, 15.0)
    assert killer is None
    assert target.read_text(encoding="utf-8") == _CORRECT


def test_panel_regression_gate_flags_a_kill_rate_drop():
    reg = detection._panel_regressions({"quixbugs_kill_rate": 0.8}, {"quixbugs_kill_rate": 0.6}, tol=0.0)
    assert "quixbugs_kill_rate" in reg["regressed"]
    none = detection._panel_regressions({"quixbugs_kill_rate": 0.6}, {"quixbugs_kill_rate": 0.6}, tol=0.0)
    assert none["regressed"] == []


def test_ablation_deltas_isolate_each_feature():
    mut = {"variants": {
        "naive": {"kill_rate": 0.2}, "cut_source": {"kill_rate": 0.5},
        "golden": {"kill_rate": 0.7}, "full": {"kill_rate": 0.9},
    }}
    d = detection._ablation_deltas(mut)
    assert d["golden_contributes"] == 0.4         # full - cut_source
    assert d["cut_source_contributes"] == 0.2     # full - golden
    assert d["full_over_naive"] == 0.7


def test_panel_builds_from_corpora():
    quix = {"available": True, "kill_rate": 0.75}
    mut = {"variants": {"full": {"kill_rate": 0.5}, "naive": {"kill_rate": None}}}
    panel = detection._panel(quix, mut)
    assert panel["quixbugs_kill_rate"] == 0.75
    assert panel["mutation_kill_rate_full"] == 0.5
    assert "mutation_kill_rate_naive" not in panel   # None metrics dropped
