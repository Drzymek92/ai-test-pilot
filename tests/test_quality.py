"""P5 quality gate — smell detection, baseline regression logic, manifest, and orchestration.
All offline (run_pipeline + coverage monkeypatched)."""
from pathlib import Path

from scripts.config import load_config
from scripts.core import quality


_SMELLY = '''
def test_roulette():
    assert a == 1
    assert b == 2

def test_clean():
    assert x == 5, "why"

def test_trivial():
    assert result is not None

def test_dup():
    assert z == 1, "m"
    assert z == 1, "m"
'''


def test_smell_report_counts(tmp_path: Path):
    f = tmp_path / "t.py"
    f.write_text(_SMELLY, encoding="utf-8")
    r = quality.smell_report(f)
    assert r["tests"] == 4
    assert r["assertion_roulette"] == 1      # two unlabeled asserts
    assert r["empty_or_trivial"] == 1        # `assert result is not None`
    assert r["duplicate_assert"] == 1        # repeated assert
    assert r["smells"] == 3 and r["density"] == 0.75


def test_smell_report_clean_exact_assertions_not_flagged(tmp_path: Path):
    # exact-value assertions (the P3a goal) must NOT count as smells (no Magic-Number penalty)
    f = tmp_path / "t.py"
    f.write_text('def test_a():\n    assert result == 50, "exact"\n', encoding="utf-8")
    assert quality.smell_report(f)["smells"] == 0


def test_panel_regressions_direction_aware():
    base = {"coverage": 80.0, "fp_rate": 0.10, "smell_density": 0.2}
    assert quality.panel_regressions(base, {**base, "coverage": 70.0})["regressed"] == ["coverage"]
    assert quality.panel_regressions(base, {**base, "fp_rate": 0.25})["regressed"] == ["fp_rate"]
    assert quality.panel_regressions(base, {**base, "smell_density": 0.5})["regressed"] == ["smell_density"]
    # improvements (coverage up, fp down) are not regressions
    assert quality.panel_regressions(base, {"coverage": 90.0, "fp_rate": 0.05, "smell_density": 0.1})["regressed"] == []


def test_panel_regressions_tolerance():
    base = {"coverage": 80.0}
    assert quality.panel_regressions(base, {"coverage": 79.5}, tol=1.0)["regressed"] == []   # within tol
    assert quality.panel_regressions(base, {"coverage": 78.0}, tol=1.0)["regressed"] == ["coverage"]


def test_load_targets_skips_missing(tmp_path: Path):
    (tmp_path / "there.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    manifest = tmp_path / "m.toml"
    manifest.write_text(
        '[[target]]\nname="ok"\npath="there.py"\nselector="f"\ngrep="there"\n'
        '[[target]]\nname="gone"\npath="missing.py"\nselector="g"\ngrep="missing"\n',
        encoding="utf-8")
    targets = quality.load_targets(manifest, tmp_path)
    assert [t["name"] for t in targets] == ["ok"]


def test_run_quality_orchestration(tmp_path: Path, monkeypatch):
    # in-repo target + a fake pipeline result with a known smelly generated test
    (tmp_path / "sample.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    bench = tmp_path / "benchmark"; bench.mkdir()
    (bench / "quality_targets.toml").write_text(
        '[[target]]\nname="s"\npath="sample.py"\nselector="f"\ngrep="sample"\n', encoding="utf-8")
    gen_test = tmp_path / "gen_test.py"
    gen_test.write_text('def test_a():\n    assert f(1) == 1, "ok"\n', encoding="utf-8")

    class _Rep:
        adapter, test_file = "python_pytest", str(gen_test)
        generated, passed, failed, errored = 4, 3, 1, 0

    import scripts.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "run_pipeline", lambda cfg, ns: _Rep())
    monkeypatch.setattr(quality, "_coverage", lambda *a, **k: 88.0)
    monkeypatch.setattr(quality.ledger_mod, "target_acceptance", lambda *a, **k: None)

    res = quality.run_quality(load_config(), tmp_path)
    assert res["panel"]["coverage"] == 88.0
    assert res["panel"]["pass_rate"] == 0.75 and res["panel"]["fp_rate"] == 0.25
    assert res["gate_pass"] is True and res["baseline_compared"] is False   # no baseline yet
    assert Path(res["report_file"]).is_file()
