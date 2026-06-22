"""M5 — `auto` tuning: ledger best-accepted-runs query + accepted-scenario few-shot. All offline."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from scripts.core import generate, ledger, tuning
from scripts.core.models import (
    RunRecord, ScenarioSet, TargetContract, TargetRef, TestScenario, UnitSpec,
)


def _rec(run_id, target="m.py", generated=4, ts=None, **kw):
    return RunRecord(run_id=run_id, ts=ts or datetime.now(), adapter="python_pytest", target=target,
                     model="x", prompt_version="v1", generated=generated, passed=generated, failed=0, **kw)


def _ss(scenarios, target="m.py"):
    return ScenarioSet(target=TargetRef(adapter="python_pytest", locator=target), scenarios=scenarios)


def _scn(sid, unit="add", tags=("happy_path",)):
    return TestScenario(id=sid, title=f"t {sid}", unit=unit, inputs={"a": 1, "b": 2},
                        expected="3", assertion="result == 3", tags=list(tags))


def _persist(out_base: Path, run_id: str, ss: ScenarioSet) -> None:
    d = out_base / "scenarios"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"scenarios_{run_id}.json").write_text(ss.model_dump_json(indent=2), encoding="utf-8")


# ── ledger.best_accepted_runs ────────────────────────────────────────────────
def test_best_accepted_runs_filters_and_orders(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    base = datetime(2026, 6, 1, 12, 0, 0)
    ledger.append(_rec("low", ts=base), db);  ledger.backfill_acceptance("low", 1, db)   # 0.25
    ledger.append(_rec("mid", ts=base + timedelta(hours=1)), db); ledger.backfill_acceptance("mid", 3, db)  # 0.75
    ledger.append(_rec("hi", ts=base + timedelta(hours=2)), db);  ledger.backfill_acceptance("hi", 4, db)   # 1.0
    runs = ledger.best_accepted_runs("python_pytest", "m.py", db, min_rate=0.6, limit=3)
    ids = [r[0] for r in runs]
    assert ids == ["hi", "mid"]            # 0.25 excluded; best-first


def test_best_accepted_runs_empty_when_no_ledger(tmp_path: Path):
    assert ledger.best_accepted_runs("python_pytest", "m.py", tmp_path / "absent.duckdb") == []


def test_best_accepted_runs_scopes_to_target(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    ledger.append(_rec("a", target="other.py"), db); ledger.backfill_acceptance("a", 4, db)
    assert ledger.best_accepted_runs("python_pytest", "m.py", db) == []


# ── tuning.fewshot_block ─────────────────────────────────────────────────────
def test_fewshot_block_none_without_history(tmp_path: Path):
    block, note = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=tmp_path / "x.duckdb",
        out_base=tmp_path, min_rate=0.6, max_examples=3, max_chars=1500)
    assert block == "" and note is None


def test_fewshot_block_builds_from_accepted_run(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    out_base = tmp_path / "out"
    ledger.append(_rec("hi"), db); ledger.backfill_acceptance("hi", 4, db)   # 1.0 accepted
    _persist(out_base, "hi", _ss([_scn("happy"), _scn("edge", tags=["edge"])]))

    block, note = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=db, out_base=out_base,
        min_rate=0.6, max_examples=3, max_chars=1500)
    assert "Accepted exemplars" in block
    assert "happy" in block and "edge" in block
    assert "rationale" not in block         # prose stripped from exemplars
    assert note and "2 accepted exemplar" in note and "hi" in note


def test_fewshot_block_respects_max_examples(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    out_base = tmp_path / "out"
    ledger.append(_rec("hi", generated=5), db); ledger.backfill_acceptance("hi", 5, db)
    _persist(out_base, "hi", _ss([_scn(f"s{i}") for i in range(5)]))

    block, note = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=db, out_base=out_base,
        min_rate=0.6, max_examples=2, max_chars=5000)
    assert note and "2 accepted exemplar" in note


def test_fewshot_block_prefers_confident_over_uncertain(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    out_base = tmp_path / "out"
    ledger.append(_rec("hi", generated=3), db); ledger.backfill_acceptance("hi", 3, db)
    _persist(out_base, "hi", _ss([
        _scn("guess", tags=["uncertain"]), _scn("solid", tags=["happy_path"])]))

    block, _ = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=db, out_base=out_base,
        min_rate=0.6, max_examples=1, max_chars=5000)
    assert "solid" in block and "guess" not in block   # confident chosen first


def test_fewshot_block_char_budget_keeps_at_least_one(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    out_base = tmp_path / "out"
    ledger.append(_rec("hi", generated=3), db); ledger.backfill_acceptance("hi", 3, db)
    _persist(out_base, "hi", _ss([_scn(f"s{i}") for i in range(3)]))

    block, note = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=db, out_base=out_base,
        min_rate=0.6, max_examples=3, max_chars=1)   # absurdly tight
    assert note and "1 accepted exemplar" in note     # never drops below one


def test_fewshot_block_survives_missing_scenario_file(tmp_path: Path):
    db = tmp_path / "runs.duckdb"
    out_base = tmp_path / "out"
    ledger.append(_rec("hi"), db); ledger.backfill_acceptance("hi", 4, db)
    # no scenarios_hi.json persisted → must degrade to no block, not raise
    block, note = tuning.fewshot_block(
        adapter="python_pytest", target="m.py", ledger_path=db, out_base=out_base,
        min_rate=0.6, max_examples=3, max_chars=1500)
    assert block == "" and note is None


# ── generation threads the few-shot block into the prompt ────────────────────
def test_generate_scenarios_includes_fewshot_block(monkeypatch):
    captured = {}

    def fake_llm(prompt, **k):
        captured["prompt"] = prompt
        return json.dumps([{"id": "a", "title": "t", "unit": "add",
                            "inputs": {"a": 1, "b": 2}, "expected": "3", "assertion": "result == 3"}])

    monkeypatch.setattr(generate, "llm_call", fake_llm)
    contract = TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="add"),
        module="m", units=[UnitSpec(name="add", signature="(a, b)")])
    generate.generate_scenarios(contract, fewshot_block="## Accepted exemplars (auto-tuning)\nMARKER\n")
    assert "MARKER" in captured["prompt"]
