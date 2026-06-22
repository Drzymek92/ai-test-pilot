"""Golden / characterization mode — capture (subprocess) + apply, offline."""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core.golden import apply_goldens, capture_goldens
from scripts.core.models import ScenarioSet, TargetRef, TestScenario


def _contract(target: Path, selector: str):
    ref = TargetRef(adapter="python_pytest", locator=str(target), selector=selector)
    return adapter.introspect(ref)


def _ss(ref, scenarios):
    return ScenarioSet(target=ref, scenarios=scenarios)


def test_golden_locks_stable_result(tmp_path: Path):
    target = tmp_path / "calc.py"
    target.write_text("def double(n: int) -> int:\n    return n * 2\n", encoding="utf-8")
    c = _contract(target, "double")
    s = TestScenario(id="d", title="double", unit="double", inputs={"n": 21},
                     expected="42", assertion="result is not None")
    ss = _ss(c.ref, [s])

    caps = capture_goldens(adapter, c, ss, cwd=tmp_path, out_dir=tmp_path)
    assert caps["d"]["ok"] and caps["d"]["repr"] == "42"

    locked = apply_goldens(ss, caps)
    assert locked == 1
    assert s.assertion == "repr(result) == '42'"
    assert "characterization" in s.tags


def test_golden_skips_unstable_repr(tmp_path: Path):
    target = tmp_path / "obj.py"
    target.write_text("class C:\n    pass\n\ndef make() -> C:\n    return C()\n", encoding="utf-8")
    c = _contract(target, "make")
    s = TestScenario(id="m", title="make", unit="make", inputs={},
                     expected="an object", assertion="result is not None")
    ss = _ss(c.ref, [s])

    caps = capture_goldens(adapter, c, ss, cwd=tmp_path, out_dir=tmp_path)
    apply_goldens(ss, caps)
    assert s.assertion == "result is not None"          # default repr (… at 0x…) → not locked
    assert "characterization" not in s.tags


def test_golden_clock_guard(tmp_path: Path):
    target = tmp_path / "clk.py"
    target.write_text(
        "from datetime import datetime\n\n"
        "def age(now: datetime = None) -> int:\n"
        "    now = now or datetime.now()\n"
        "    return now.year\n",
        encoding="utf-8",
    )
    c = _contract(target, "age")
    assert c.units[0].reads_clock is True
    pinned = TestScenario(id="p", title="pinned", unit="age",
                          inputs={"now": {"$call": "datetime", "args": [2026, 1, 1]}},
                          expected="2026", assertion="result == 2026")
    unpinned = TestScenario(id="u", title="unpinned", unit="age", inputs={},
                            expected="a year", assertion="result > 0")
    ss = _ss(c.ref, [pinned, unpinned])
    caps = capture_goldens(adapter, c, ss, cwd=tmp_path, out_dir=tmp_path)
    apply_goldens(ss, caps)
    assert pinned.assertion == "repr(result) == '2026'"      # time pinned → locked
    assert unpinned.assertion == "result > 0"                # clock unpinned → NOT locked


def test_golden_skips_error_and_tmp_file_scenarios(tmp_path: Path):
    target = tmp_path / "calc.py"
    target.write_text("def double(n: int) -> int:\n    return n * 2\n", encoding="utf-8")
    c = _contract(target, "double")
    err = TestScenario(id="e", title="e", unit="double", inputs={"n": 1},
                       expected="raises", expect_error="ValueError")
    ss = _ss(c.ref, [err])
    caps = capture_goldens(adapter, c, ss, cwd=tmp_path, out_dir=tmp_path)
    assert caps == {}                                   # expect_error scenario is not probed
