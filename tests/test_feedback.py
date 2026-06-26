"""Approach 1 — feedback-driven regeneration. Offline (no LLM, no cov subprocess).

The coverage probe and the LLM call are both injected, so these exercise the loop logic, the
prompt-block construction, de-duplication/id-uniqueness, and the config/generate wiring without
spending tokens or shelling out.
"""
from pathlib import Path

from scripts.adapters import python_pytest as adapter
from scripts.config import load_config
from scripts.core import feedback, generate
from scripts.core.models import (ScenarioSet, TargetContract, TargetRef, TestScenario, UnitSpec)

_MOD = (
    'def classify(n):\n'
    '    """Return a label for n."""\n'
    '    if n < 0:\n'
    '        return "neg"\n'
    '    if n == 0:\n'
    '        return "zero"\n'
    '    return "pos"\n'
)


def _contract(tmp_path: Path) -> tuple[TargetContract, Path]:
    p = tmp_path / "classify_mod.py"
    p.write_text(_MOD, encoding="utf-8")
    contract = adapter.introspect(TargetRef(adapter="python_pytest", locator=str(p), selector="classify"))
    return contract, p


def _scn(sid: str, **inputs) -> TestScenario:
    return TestScenario(id=sid, title=sid, unit="classify", inputs=inputs or {"n": 1},
                        expected="a label", assertion='isinstance(result, str)')


def _set(*scenarios: TestScenario) -> ScenarioSet:
    return ScenarioSet(target=TargetRef(adapter="python_pytest", locator="classify_mod.py"),
                       scenarios=list(scenarios))


# ── config + generate wiring ───────────────────────────────────────────────────
def test_config_defaults_feedback_off():
    cfg = load_config()
    assert cfg.feedback.enabled is False
    assert cfg.feedback.max_rounds == 1
    assert cfg.feedback.count == 3


def test_generate_includes_feedback_block(monkeypatch):
    seen = {}

    def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return '[{"id":"a","title":"t","unit":"classify","inputs":{"n":1},' \
               '"expected":"pos","assertion":"result == \'pos\'"}]'

    monkeypatch.setattr(generate, "llm_call", fake_llm)
    contract = TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="classify"),
        module="m", units=[UnitSpec(name="classify", signature="(n)")])
    generate.generate_scenarios(contract, prompt_version="v1",
                                feedback_block="\nUNCOVERED_MARKER line 5\n")
    assert "UNCOVERED_MARKER line 5" in seen["prompt"]


# ── prompt block: coverage gap only, never bug/mutant info ──────────────────────
def test_uncovered_block_has_lines_and_existing_inputs():
    block = feedback.uncovered_block(_MOD, [5, 6], [_scn("s1", n=1)], round_no=1)
    assert "round 1" in block
    assert 'return "zero"' in block          # line 6 source surfaced
    assert "classify(n=1)" in block          # existing input listed to avoid duplication
    assert "NEW" in block
    # anti-overfit: no bug/mutant vocabulary leaks into the prompt
    low = block.lower()
    assert "mutant" not in low and "bug" not in low and "kill" not in low


def test_uncovered_block_caps_shown_lines():
    block = feedback.uncovered_block(_MOD, list(range(1, 8)), [], round_no=1, max_lines=2)
    assert "more uncovered line(s) omitted" in block


# ── de-duplication + id/tag handling ───────────────────────────────────────────
def test_dedupe_drops_duplicate_inputs():
    existing = [_scn("s1", n=1)]
    cands = [_scn("dup", n=1), _scn("fresh", n=-1)]
    kept = feedback.dedupe_new(cands, existing, round_no=1)
    assert [k.inputs for k in kept] == [{"n": -1}]
    assert "feedback" in kept[0].tags


def test_dedupe_makes_ids_unique():
    existing = [_scn("s1", n=1)]
    kept = feedback.dedupe_new([_scn("s1", n=0)], existing, round_no=2)   # id collision, new inputs
    assert kept[0].id != "s1" and kept[0].id.startswith("fb2_")


# ── the loop ────────────────────────────────────────────────────────────────────
def test_run_feedback_appends_new_scenario(tmp_path: Path):
    contract, target = _contract(tmp_path)
    sset = _set(_scn("base", n=1))
    calls = {"measure": 0, "gen": 0}

    def fake_measure(t, test, root, grep=None):
        calls["measure"] += 1
        return [5, 6] if calls["measure"] == 1 else []      # gap on first probe, closed after

    def fake_gen(block, count):
        calls["gen"] += 1
        return _set(_scn("edge", n=0))

    summary = feedback.run_feedback(
        adapter=adapter, contract=contract, scenario_set=sset, target_file=target,
        project_root=Path(__file__).resolve().parent.parent, probe_dir=tmp_path / "_fb",
        generate_fn=fake_gen, max_rounds=1, count=3, min_uncovered=1, max_uncovered_shown=25,
        measure_fn=fake_measure)

    assert summary["added"] == 1 and summary["rounds"] == 1
    assert summary["uncovered_before"] == 2 and summary["uncovered_after"] == 0
    assert calls["gen"] == 1
    assert any(s.id == "edge" and "feedback" in s.tags for s in sset.scenarios)
    assert not (tmp_path / "_fb" / "_fb_probe_classify_mod.py").exists()   # probe cleaned up


def test_run_feedback_skips_when_no_gap(tmp_path: Path):
    contract, target = _contract(tmp_path)
    sset = _set(_scn("base", n=1))
    called = {"gen": False}

    summary = feedback.run_feedback(
        adapter=adapter, contract=contract, scenario_set=sset, target_file=target,
        project_root=Path(__file__).resolve().parent.parent, probe_dir=tmp_path / "_fb",
        generate_fn=lambda b, c: called.__setitem__("gen", True) or _set(),
        max_rounds=1, count=3, min_uncovered=1, max_uncovered_shown=25,
        measure_fn=lambda t, test, root, grep=None: [])

    assert summary["added"] == 0 and summary["rounds"] == 0
    assert called["gen"] is False                           # no LLM call when there's no gap
    assert len(sset.scenarios) == 1


def test_run_feedback_dedupes_against_existing(tmp_path: Path):
    contract, target = _contract(tmp_path)
    sset = _set(_scn("base", n=1))

    summary = feedback.run_feedback(
        adapter=adapter, contract=contract, scenario_set=sset, target_file=target,
        project_root=Path(__file__).resolve().parent.parent, probe_dir=tmp_path / "_fb",
        generate_fn=lambda b, c: _set(_scn("dupe", n=1)),   # same inputs as the existing scenario
        max_rounds=1, count=3, min_uncovered=1, max_uncovered_shown=25,
        measure_fn=lambda t, test, root, grep=None: [5, 6])

    assert summary["added"] == 0
    assert len(sset.scenarios) == 1


def test_run_feedback_generation_failure_is_nonfatal(tmp_path: Path):
    contract, target = _contract(tmp_path)
    sset = _set(_scn("base", n=1))

    def boom(block, count):
        raise RuntimeError("LLM exploded")

    summary = feedback.run_feedback(
        adapter=adapter, contract=contract, scenario_set=sset, target_file=target,
        project_root=Path(__file__).resolve().parent.parent, probe_dir=tmp_path / "_fb",
        generate_fn=boom, max_rounds=2, count=3, min_uncovered=1, max_uncovered_shown=25,
        measure_fn=lambda t, test, root, grep=None: [5, 6])

    assert summary["added"] == 0                            # round-0 suite preserved, no crash
    assert len(sset.scenarios) == 1


# ── scoping uncovered lines to the units under test ─────────────────────────────
def test_unit_line_ranges_and_scoping():
    two_fn = (
        "def a(x):\n"          # 1
        "    return x\n"       # 2
        "\n"                   # 3
        "def b(y):\n"          # 4
        "    if y:\n"          # 5
        "        return 1\n"   # 6
        "    return 0\n"       # 7
    )
    ranges = feedback.unit_line_ranges(two_fn, {"b"})
    assert ranges == [(4, 7)]
    # a miss inside a() (line 2) is dropped; misses inside b() (6,7) are kept
    assert feedback._scope_missed([2, 6, 7], ranges) == [6, 7]
    # no ranges resolved → don't over-filter
    assert feedback._scope_missed([2, 6], []) == [2, 6]


# ── covjson parsing + detection namespace ───────────────────────────────────────
def test_last_covjson_parsing():
    out = "noise\nCOVJSON:{\"files\": {\"a.cover\": {\"missed_lines\": [1, 2]}}}\ntrailer"
    payload = feedback._last_covjson(out)
    assert payload["files"]["a.cover"]["missed_lines"] == [1, 2]
    assert feedback._last_covjson("no json here") is None


def test_detection_request_has_feedback_fields():
    """detection builds a typed RunRequest; feedback fields are always present (the contract
    that fixed the namespace-drift bug class)."""
    from scripts.core import detection
    req = detection.gen_request("t.py", "fn", cut_source=True, golden=False, context_path=None)
    assert hasattr(req, "feedback") and hasattr(req, "no_feedback")
    assert req.no_run is True and req.no_cut_source is False


def test_probe_is_hard_bounded_on_pathological_target(tmp_path: Path):
    """Regression for the Chunk-2 hang: a runaway test under `trace` (here an infinite loop, the same
    class as QuixBugs naive `levenshtein`) must be HARD-killed and yield [] — never hang the batch."""
    import time

    from scripts.core.materialize import materialize

    target = tmp_path / "loop_mod.py"
    target.write_text("def spin(n):\n    while True:\n        n += 1\n    return n\n", encoding="utf-8")
    contract = adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector="spin"))
    sset = _set(TestScenario(id="loops", title="loops", unit="spin", inputs={"n": 0},
                             expected="never returns", assertion="result == 0"))
    probe = tmp_path / "probe.py"
    materialize(adapter, contract, sset, probe)

    t0 = time.time()
    missed = feedback.measure_uncovered(target, probe, Path(__file__).resolve().parent.parent,
                                        grep="loop_mod", timeout=3.0)
    elapsed = time.time() - t0
    assert missed == []            # a killed probe yields no coverage signal — feedback just stops
    assert elapsed < 30            # hard-killed at ~3s, not hung
