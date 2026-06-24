"""Security: the value grammar may only render symbols the tool itself resolved.

`_render_value` interpolates `$type`/`$call`/`$enum` names and kwarg names raw into code
positions, and the materialized file is executed by pytest (and the golden probe). Since the
target's source/docstrings are fed to the prompt by default, a crafted scenario must NOT be able
to author code tokens. `validate_scenario` allow-lists every symbol against the resolved contract
and is enforced (a) at generation time → repair-retry, and (b) as a render-time safety net.
"""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core import generate
from scripts.core.models import TargetRef, TestScenario


_MODELS = '''\
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Status(Enum):
    NEW = "new"
    DONE = "done"


@dataclass
class Line:
    sku: str
    price: Decimal
'''

_TARGET = '''\
from models import Line, Status


def label(line: Line, status: Status) -> str:
    return f"{line.sku}:{status.value}"
'''


@pytest.fixture
def contract(tmp_path: Path):
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector="label"))


def _scn(inputs: dict) -> TestScenario:
    return TestScenario(id="s", title="t", unit="label", expected="x", assertion="result", inputs=inputs)


# ── accepts the legitimate grammar (regression guard) ─────────────────────────
def test_accepts_resolved_symbols(contract):
    s = _scn({
        "line": {"$type": "Line", "args": {"sku": "A", "price": {"$call": "Decimal", "args": ["1.0"]}}},
        "status": {"$enum": "Status.NEW"},
    })
    adapter.validate_scenario(s, contract)              # no raise
    adapter.emit(s, contract)                           # render-time net also passes


# ── rejects un-resolved symbols in every code position ────────────────────────
def test_rejects_unknown_type(contract):
    with pytest.raises(ValueError, match=r"\$type"):
        adapter.validate_scenario(_scn({"line": {"$type": "os.system", "args": {}}}), contract)


def test_rejects_unknown_call(contract):
    with pytest.raises(ValueError, match=r"\$call"):
        adapter.validate_scenario(_scn({"line": {"$call": "eval", "args": ["__import__('os')"]}}), contract)


def test_rejects_unknown_enum(contract):
    with pytest.raises(ValueError, match=r"\$enum"):
        adapter.validate_scenario(_scn({"status": {"$enum": "Evil.MEMBER"}}), contract)


def test_rejects_unknown_enum_member(contract):
    with pytest.raises(ValueError, match=r"member"):
        adapter.validate_scenario(_scn({"status": {"$enum": "Status.DROP_TABLE"}}), contract)


def test_rejects_injected_kwarg_name(contract):
    # a kwarg name is interpolated raw (`k=...`); a non-identifier is a code-injection vector
    bad = {"line": {"$type": "Line", "args": {"sku); import os; (": "x"}}}
    with pytest.raises(ValueError, match=r"argument name"):
        adapter.validate_scenario(_scn(bad), contract)


def test_rejects_nested_unknown_type(contract):
    bad = {"line": {"$type": "Line", "args": {"price": {"$type": "Subprocess", "args": {}}}}}
    with pytest.raises(ValueError, match=r"\$type"):
        adapter.validate_scenario(_scn(bad), contract)


# ── emit (render-time safety net) refuses to materialize an un-allow-listed symbol ──
def test_emit_refuses_unsafe_scenario(contract):
    with pytest.raises(ValueError):
        adapter.emit(_scn({"line": {"$type": "__import__", "args": {}}}), contract)


# ── a bad value-grammar symbol routes through the repair-retry at generation time ──
def test_generation_repairs_unsafe_value_grammar(contract, monkeypatch):
    bad = ('[{"id":"s","title":"t","unit":"label","expected":"x","assertion":"result",'
           '"inputs":{"line":{"$type":"os.system","args":{}}}}]')
    good = ('[{"id":"s","title":"t","unit":"label","expected":"x","assertion":"result",'
            '"inputs":{"status":{"$enum":"Status.NEW"},'
            '"line":{"$type":"Line","args":{"sku":"A","price":{"$call":"Decimal","args":["1.0"]}}}}}]')
    replies = iter([bad, good])
    monkeypatch.setattr(generate, "llm_call", lambda *a, **k: next(replies))
    result = generate.generate_scenarios(contract, adapter=adapter, count=3, repair_retries=1)
    assert len(result.scenarios) == 1
    assert result.scenarios[0].inputs["line"]["$type"] == "Line"
