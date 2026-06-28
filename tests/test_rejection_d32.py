"""Deterministic validator-rejection tests + the validator-weakening mutation operator.

Covers: the guard inverter (membership + range → a provably-invalid value), rejection-scenario
assembly, ValidationError import/allow-list, the validator-weakening mutant, and the headline proof —
a rejection test KILLS a weakened-validator mutant that the valid-only suite MISSES. All offline.
"""
from pathlib import Path

import pytest

from benchmark.mutation import validator_weakening_mutants
from scripts.adapters import python_pytest as adapter
from scripts.core.materialize import materialize
from scripts.core.models import ScenarioSet, TargetRef, TestScenario
from scripts.core.rejection import rejection_scenarios
from scripts.core.runner import run_tests


_MODELS = '''\
from decimal import Decimal

from pydantic import BaseModel, field_validator


class Coupon(BaseModel):
    code: str
    percent: int

    @field_validator("percent")
    @classmethod
    def _allowed(cls, v: int) -> int:
        if v not in (5, 10, 15, 20):
            raise ValueError("percent must be one of 5/10/15/20")
        return v


class Rate(BaseModel):
    value: Decimal

    @field_validator("value")
    @classmethod
    def _range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("0.30")):
            raise ValueError("value must be within [0, 0.30]")
        return v


class Plain(BaseModel):
    x: int
'''

_TARGET = '''\
from models import Coupon, Rate, Plain


def use_coupon(price: int, c: Coupon) -> int:
    return price - price * c.percent // 100


def use_rate(r: Rate) -> str:
    return str(r.value)


def use_plain(p: Plain) -> int:
    return p.x
'''


def _project(tmp_path: Path, models_src: str = _MODELS) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models.py").write_text(models_src, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return target


def _contract(target: Path, selector: str):
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector=selector))


# ── inverter (via introspection: reject_example on FieldSpec) ──────────────────────────────────
def test_membership_reject_example(tmp_path):
    c = _contract(_project(tmp_path), "use_coupon")
    rex = next(f.reject_example for f in c.types["Coupon"].fields if f.name == "percent")
    assert isinstance(rex, int) and rex not in (5, 10, 15, 20)


def test_range_reject_example_is_above_upper_bound(tmp_path):
    c = _contract(_project(tmp_path), "use_rate")
    rex = next(f.reject_example for f in c.types["Rate"].fields if f.name == "value")
    assert rex == {"$call": "Decimal", "args": ["1.3"]}        # 0.30 + 1, rendered as Decimal


def test_no_validator_no_reject_example(tmp_path):
    c = _contract(_project(tmp_path), "use_plain")
    assert all(f.reject_example is None for f in c.types["Plain"].fields)


# ── rejection-scenario assembly ────────────────────────────────────────────────────────────────
def test_rejection_scenario_built_for_membership(tmp_path):
    c = _contract(_project(tmp_path), "use_coupon")
    scns = rejection_scenarios(c)
    assert len(scns) == 1
    s = scns[0]
    assert s.expect_error == "ValidationError" and "rejection" in s.tags
    coupon_args = s.inputs["c"]["args"]
    assert coupon_args["percent"] not in (5, 10, 15, 20)        # the invalid value
    assert coupon_args["code"] == "x"                            # other required field filled valid
    assert s.inputs["price"] == 0                                # other unit param filled valid


def test_materialized_rejection_imports_validation_error(tmp_path):
    c = _contract(_project(tmp_path), "use_coupon")
    scns = rejection_scenarios(c)
    header = adapter.file_header(c, scns)
    assert "from pydantic import ValidationError" in header
    out = tmp_path / "test_rej_gen.py"
    materialize(adapter, c, ScenarioSet(target=c.ref, scenarios=scns), out)
    body = out.read_text(encoding="utf-8")
    assert "pytest.raises(ValidationError)" in body


# ── validator-weakening mutation operator ──────────────────────────────────────────────────────
def test_validator_weakening_produces_one_mutant_per_validator():
    muts = {m.description: m for m in validator_weakening_mutants(_MODELS)}
    assert set(muts) == {"weaken validator Coupon._allowed", "weaken validator Rate._range"}
    # each mutant removes exactly ITS OWN guard (the other validator's raise is untouched)
    assert _MODELS.count("raise ValueError") == 2
    assert "percent must be one of" not in muts["weaken validator Coupon._allowed"].source
    assert "value must be within" in muts["weaken validator Coupon._allowed"].source
    assert muts["weaken validator Rate._range"].source.count("raise ValueError") == 1


# ── the headline proof: rejection test catches what the valid-only suite misses ────────────────
def _run(tmp_path, tag, scenarios, models_src):
    d = tmp_path / tag
    d.mkdir()
    target = _project(d, models_src)
    c = _contract(target, "use_coupon")
    out = d / f"test_{tag}.py"
    ss = ScenarioSet(target=c.ref, scenarios=scenarios)
    materialize(adapter, c, ss, out)
    return {r.scenario_id: r.status for r in run_tests(adapter, out, ss, cwd=d)}


def _killed(correct: dict, mutant: dict) -> bool:
    return any(correct.get(k) == "passed" and mutant.get(k) != "passed" for k in correct)


def test_rejection_kills_weakened_validator_that_valid_suite_misses(tmp_path):
    base = _project(tmp_path / "base")
    contract = _contract(base, "use_coupon")
    rej = rejection_scenarios(contract)
    assert rej, "expected a rejection scenario for Coupon.percent"
    valid = TestScenario(id="valid_10", title="valid", unit="use_coupon", expected="90",
                         assertion="result == 90",
                         inputs={"price": 100,
                                 "c": {"$type": "Coupon", "args": {"code": "x", "percent": 10}}})

    mutant_src = next(m.source for m in validator_weakening_mutants(_MODELS)
                      if m.description == "weaken validator Coupon._allowed")

    rej_correct = _run(tmp_path, "rc", rej, _MODELS)
    rej_mutant = _run(tmp_path, "rm", rej, mutant_src)
    val_correct = _run(tmp_path, "vc", [valid], _MODELS)
    val_mutant = _run(tmp_path, "vm", [valid], mutant_src)

    assert all(s == "passed" for s in rej_correct.values()), rej_correct
    assert all(s == "passed" for s in val_correct.values()), val_correct
    # the delta:
    assert _killed(rej_correct, rej_mutant), "rejection test should KILL the weakened validator"
    assert not _killed(val_correct, val_mutant), "valid-only suite should MISS the weakened validator"
