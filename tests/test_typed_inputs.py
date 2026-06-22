"""Typed-input construction (Phase 1): recursive type resolution, value rendering,
import collection, and project-context loading — all deterministic/offline."""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core.context import context_excerpt, find_project_context, load_context
from scripts.core.models import ScenarioSet, TargetRef, TestScenario


# A small project tree: a dataclass + a pydantic-ish model + an enum, used by a target fn.
_MODELS = '''\
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, Field


class Status(Enum):
    NEW = "new"
    DONE = "done"


@dataclass
class Line:
    sku: str
    qty: int
    price: Decimal


@dataclass
class Order:
    status: Status
    lines: list[Line]


class Settings(BaseModel):
    rate: float = 0.1
    tags: list[str] = Field(default_factory=list)
'''

_TARGET = '''\
from models import Order, Settings


def total(order: Order, settings: Settings) -> float:
    return float(sum(l.qty for l in order.lines)) * settings.rate
'''


@pytest.fixture
def typed_project(tmp_path: Path) -> Path:
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return target


def _contract(target: Path):
    ref = TargetRef(adapter="python_pytest", locator=str(target), selector="total")
    return adapter.introspect(ref)


def test_resolves_nested_dataclass_pydantic_enum(typed_project):
    c = _contract(typed_project)
    assert set(c.types) == {"Order", "Line", "Settings", "Status"}
    assert c.types["Order"].kind == "dataclass"
    assert c.types["Settings"].kind == "pydantic"
    assert c.types["Status"].kind == "enum"
    assert c.types["Status"].enum_members == ["NEW", "DONE"]
    # pydantic fields are all defaulted; dataclass Line fields are required
    assert all(f.has_default for f in c.types["Settings"].fields)
    assert not any(f.has_default for f in c.types["Line"].fields)
    # resolved → not flagged as unsupported
    assert c.units[0].complex_params == []


def test_render_value_grammar():
    rv = adapter._render_value
    assert rv({"$type": "Settings", "args": {}}) == "Settings()"
    assert rv({"$call": "Decimal", "args": ["9.99"]}) == "Decimal('9.99')"
    assert rv({"$enum": "Status.DONE"}) == "Status.DONE"
    nested = rv({"$type": "Order", "args": {
        "status": {"$enum": "Status.NEW"},
        "lines": [{"$type": "Line", "args": {"sku": "A", "qty": 2,
                                             "price": {"$call": "Decimal", "args": ["5.00"]}}}]}})
    assert nested == ("Order(status=Status.NEW, lines=[Line(sku='A', qty=2, "
                      "price=Decimal('5.00'))])")


def test_file_header_collects_constructor_imports(typed_project):
    c = _contract(typed_project)
    s = TestScenario(id="t", title="t", unit="total", expected="x", assertion="result >= 0",
                     inputs={"order": {"$type": "Order", "args": {
                         "status": {"$enum": "Status.NEW"},
                         "lines": [{"$type": "Line", "args": {
                             "sku": "A", "qty": 1, "price": {"$call": "Decimal", "args": ["1.0"]}}}]}},
                             "settings": {"$type": "Settings", "args": {}}})
    header = adapter.file_header(c, [s])
    assert "from decimal import Decimal" in header
    assert "from models import Line, Order, Settings, Status" in header or all(
        n in header for n in ("Order", "Line", "Settings", "Status"))


def test_end_to_end_offline_construction(typed_project, tmp_path):
    """Materialize + run a typed scenario with no LLM — proves the rendered test executes."""
    from scripts.core.materialize import materialize
    from scripts.core.runner import run_tests

    c = _contract(typed_project)
    s = TestScenario(id="calc", title="total", unit="total", expected="2.0",
                     assertion="result == 2.0",
                     inputs={"order": {"$type": "Order", "args": {
                         "status": {"$enum": "Status.NEW"},
                         "lines": [{"$type": "Line", "args": {
                             "sku": "A", "qty": 20, "price": {"$call": "Decimal", "args": ["1.0"]}}}]}},
                             "settings": {"$type": "Settings", "args": {"rate": 0.1}}})
    ss = ScenarioSet(target=c.ref, scenarios=[s])
    out = tmp_path / "test_calc_gen.py"
    materialize(adapter, c, ss, out)
    results = run_tests(adapter, out, ss, cwd=tmp_path)
    assert results[0].status == "passed", results[0].captured


# ── project context ──────────────────────────────────────────────────────────
def test_find_and_excerpt_context(tmp_path: Path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "project.md").write_text("# Purpose\nDomain rules here.", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    target = tmp_path / "scripts" / "x.py"
    target.write_text("def f(): pass", encoding="utf-8")
    found = find_project_context(target)
    assert found is not None and found.name == "project.md"     # agent/project.md preferred
    block, src = load_context(target, max_chars=2000)
    assert "Domain rules here." in block and src == found


def test_context_excerpt_bounds(tmp_path: Path):
    p = tmp_path / "README.md"
    p.write_text("x\n" * 5000, encoding="utf-8")
    out = context_excerpt(p, max_chars=100)
    assert len(out) <= 140 and "truncated" in out
