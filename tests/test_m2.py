"""M2 — triage (deterministic rules), DuckDB ledger, propose tuning. All offline."""
from datetime import datetime
from pathlib import Path

from scripts.core import ledger, tuning
from scripts.core.models import (
    RunRecord, RunResult, ScenarioSet, TargetContract, TargetRef, TestScenario, UnitSpec,
)
from scripts.core.triage import triage


def _ctx(units=None):
    ref = TargetRef(adapter="python_pytest", locator="m.py")
    return ScenarioSet, TargetContract(ref=ref, module="m", units=units or [UnitSpec(name="f")])


def _ss(scenarios):
    return ScenarioSet(target=TargetRef(adapter="python_pytest", locator="m.py"), scenarios=scenarios)


# ── triage (deterministic, no LLM) ───────────────────────────────────────────
def test_triage_filenotfound_is_bad_scenario():
    _, c = _ctx()
    s = TestScenario(id="a", title="t", unit="f", expected="x")
    r = RunResult(scenario_id="a", status="error", signal="FileNotFoundError", captured="...")
    v = triage([r], _ss([s]), c, llm_for_ambiguous=False)[0]
    assert v.verdict == "bad_scenario" and v.source == "deterministic"


def test_triage_dict_attribute_is_bad_scenario():
    _, c = _ctx()
    s = TestScenario(id="a", title="t", unit="f", expected="x")
    r = RunResult(scenario_id="a", status="failed", signal="AttributeError",
                  captured="AttributeError: 'dict' object has no attribute 'commission'")
    assert triage([r], _ss([s]), c, llm_for_ambiguous=False)[0].verdict == "bad_scenario"


def test_triage_characterization_assertion_is_real_bug():
    _, c = _ctx()
    s = TestScenario(id="a", title="t", unit="f", expected="x", tags=["characterization"])
    r = RunResult(scenario_id="a", status="failed", signal="AssertionError", captured="...")
    v = triage([r], _ss([s]), c, llm_for_ambiguous=False)[0]
    assert v.verdict == "real_bug"


def test_triage_import_is_env_issue():
    _, c = _ctx()
    s = TestScenario(id="a", title="t", unit="f", expected="x")
    r = RunResult(scenario_id="a", status="error", signal="not_collected", captured="...")
    assert triage([r], _ss([s]), c, llm_for_ambiguous=False)[0].verdict == "env_issue"


def test_triage_ambiguous_without_llm_defaults():
    _, c = _ctx()
    s = TestScenario(id="a", title="t", unit="f", expected="x")           # no characterization tag
    r = RunResult(scenario_id="a", status="failed", signal="AssertionError", captured="...")
    v = triage([r], _ss([s]), c, llm_for_ambiguous=False)[0]
    assert v.verdict == "bad_scenario" and v.confidence < 0.5


# ── ledger (DuckDB) ──────────────────────────────────────────────────────────
def _rec(run_id, pv, generated=4, **kw):
    return RunRecord(run_id=run_id, ts=datetime.now(), adapter="python_pytest", target="m.py",
                     model="x", prompt_version=pv, generated=generated, passed=generated, failed=0, **kw)


def test_ledger_append_and_backfill(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    ledger.append(_rec("r1", "v1"), db)
    assert ledger.backfill_acceptance("r1", 3, db) is True
    assert ledger.backfill_acceptance("missing", 1, db) is False
    stats = ledger.prompt_version_stats("python_pytest", db)
    assert stats and stats[0][0] == "v1" and abs(stats[0][1] - 0.75) < 1e-9   # 3/4


def test_ledger_prompt_version_ranking(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    ledger.append(_rec("a", "v1"), db); ledger.backfill_acceptance("a", 2, db)   # 0.5
    ledger.append(_rec("b", "v2"), db); ledger.backfill_acceptance("b", 4, db)   # 1.0
    ranked = ledger.prompt_version_stats("python_pytest", db)
    assert ranked[0][0] == "v2"            # best first


# ── tuning (propose) ─────────────────────────────────────────────────────────
def test_select_prompt_version_auto_falls_back(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    v, note = tuning.select_prompt_version("python_pytest", "auto", ledger_path=db, min_runs=5)
    assert v == "v1" and "no accepted history" in note


def test_select_prompt_version_passthrough(tmp_path: Path):
    v, note = tuning.select_prompt_version("python_pytest", "v1", ledger_path=tmp_path / "x.duckdb", min_runs=5)
    assert v == "v1" and note is None


def test_suggestions_flag_real_bug_and_bad_scenario(tmp_path: Path):
    notes = tuning.suggestions(
        adapter="python_pytest", target="m.py", prompt_version="v1",
        triage_counts={"real_bug": 1, "bad_scenario": 3}, ledger_path=tmp_path / "x.duckdb",
        min_runs=5, escalate_below=0.5)
    joined = " ".join(notes)
    assert "real_bug" in joined and "bad_scenario" in joined
