"""Approach 3 — usage-guided typed-input construction (a duck + b init-class/caller-scan + c builder).

Offline (ast-only introspection + deterministic emission; no LLM). Proves the green≈0 unlock: params
that were 'complex/unsupported' now resolve to a construction strategy and render through the SAME
$type grammar + allow-list (so 'model authors JSON, never code' still holds).
"""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core import generate
from scripts.core.models import FieldSpec, TargetContract, TargetRef, TestScenario, TypeSpec, UnitSpec


def _introspect(tmp_path: Path, src: str, selector: str, **kw) -> TargetContract:
    p = tmp_path / "tgt.py"
    p.write_text(src, encoding="utf-8")
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(p), selector=selector), **kw)


def _scn(unit: str, **inputs) -> TestScenario:
    return TestScenario(id="s", title="t", unit=unit, inputs=inputs,
                        expected="x", assertion="result is not None")


# ── A3(b): plain class → init-class construction (the BFS/DFS unlock) ───────────
_GRAPH = (
    "class Node:\n"
    "    def __init__(self, value, successors=None):\n"
    "        self.value = value\n"
    "        self.successors = successors or []\n"
    "\n"
    "def bfs(start: Node, goal):\n"
    "    queue = [start]\n"
    "    while queue:\n"
    "        n = queue.pop(0)\n"
    "        if n.value == goal:\n"
    "            return True\n"
    "        for s in n.successors:\n"
    "            queue.append(s)\n"
    "    return False\n"
)


def test_initclass_resolves_plain_class(tmp_path: Path):
    c = _introspect(tmp_path, _GRAPH, "bfs")
    assert c.types["Node"].kind == "initclass"
    names = {f.name for f in c.types["Node"].fields}
    assert names == {"value", "successors"}            # __init__ params (sans self)
    # value is required, successors defaulted
    by = {f.name: f.has_default for f in c.types["Node"].fields}
    assert by == {"value": False, "successors": True}
    # the param that used to be 'complex' is now resolved
    assert c.units[0].complex_params == []


def test_initclass_emits_real_constructor(tmp_path: Path):
    c = _introspect(tmp_path, _GRAPH, "bfs")
    scn = _scn("bfs",
               start={"$type": "Node", "args": {"value": 1,
                      "successors": [{"$type": "Node", "args": {"value": 2}}]}},
               goal=2)
    src = adapter.emit(scn, c)
    assert "Node(value=1, successors=[Node(value=2)])" in src
    header = adapter.file_header(c, [scn])
    assert "import Node" in header                      # the REAL class is imported


# ── A3(a): opaque/third-party type → duck-typed SimpleNamespace stand-in ────────
_OPAQUE = (
    "from external_lib import Widget\n"
    "\n"
    "def area(w: Widget):\n"
    "    return w.width * w.height\n"
)


def test_duck_inference_from_body_usage(tmp_path: Path):
    c = _introspect(tmp_path, _OPAQUE, "area")
    assert c.types["Widget"].kind == "duck"
    assert {f.name for f in c.types["Widget"].fields} == {"width", "height"}   # attrs the body reads
    assert c.units[0].complex_params == []             # no longer warn-and-skip


def test_duck_emits_simplenamespace_and_import(tmp_path: Path):
    c = _introspect(tmp_path, _OPAQUE, "area")
    scn = _scn("area", w={"$type": "Widget", "args": {"width": 3, "height": 4}})
    src = adapter.emit(scn, c)
    assert "SimpleNamespace(width=3, height=4)" in src
    header = adapter.file_header(c, [scn])
    assert "from types import SimpleNamespace" in header
    assert "external_lib" not in header                # the opaque type is NOT imported


# ── A3(c): user builder hatch wins outright ────────────────────────────────────
_CFG = (
    "def price(config: RulesConfig):\n"
    "    return config.rate\n"
)


def test_builder_hatch_resolves_and_emits(tmp_path: Path):
    c = _introspect(tmp_path, _CFG, "price",
                    builders={"RulesConfig": "myproj.testkit:make_rules"})
    assert c.types["RulesConfig"].kind == "builder"
    scn = _scn("price", config={"$type": "RulesConfig", "args": {"rate": 5}})
    src = adapter.emit(scn, c)
    assert "make_rules(rate=5)" in src                  # built by the user function
    header = adapter.file_header(c, [scn])
    assert "from myproj.testkit import make_rules" in header


def test_builder_overrides_duck(tmp_path: Path):
    # Without a builder, RulesConfig would be duck-typed (config.rate is read); the builder wins.
    duck = _introspect(tmp_path, _CFG, "price")
    assert duck.types["RulesConfig"].kind == "duck"
    built = _introspect(tmp_path, _CFG, "price", builders={"RulesConfig": "m:make"})
    assert built.types["RulesConfig"].kind == "builder"


# ── A3(b) caller-scan: construction hint surfaced ──────────────────────────────
_WITH_CALLER = (
    "class Node:\n"
    "    def __init__(self, value):\n"
    "        self.value = value\n"
    "\n"
    "def make():\n"
    "    return Node(value=5)\n"
    "\n"
    "def use(n: Node):\n"
    "    return n.value\n"
)


def test_caller_scan_records_construction_hint(tmp_path: Path):
    c = _introspect(tmp_path, _WITH_CALLER, "use")
    assert "value=" in (c.types["Node"].usage_hint or "")


# ── allow-list invariant still holds ─────────────────────────────────────
def test_allow_list_rejects_unknown_type(tmp_path: Path):
    c = _introspect(tmp_path, _OPAQUE, "area")          # only Widget is known
    bad = _scn("area", w={"$type": "os.system", "args": {}})
    with pytest.raises(ValueError):
        adapter.validate_scenario(bad, c)


# ── prompt guidance renders per strategy ───────────────────────────────────────
def test_contract_block_describes_strategies():
    contract = TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py"),
        module="m",
        units=[UnitSpec(name="f", signature="(a, b, c)")],
        types={
            "Node": TypeSpec(name="Node", kind="initclass", module="m",
                             fields=[], usage_hint="Node(value=)"),
            "Widget": TypeSpec(name="Widget", kind="duck", module="",
                               fields=[FieldSpec(name="width", has_default=True)]),
            "Cfg": TypeSpec(name="Cfg", kind="builder", module="", builder="m:make"),
        },
    )
    block = generate._contract_block(contract, include_source=False)
    assert "stand-in Widget(width)" in block
    assert "built-by-builder Cfg" in block
    assert "callers construct it as: Node(value=)" in block
    assert "uncertain" in block                          # H4 guard note for duck stand-ins
