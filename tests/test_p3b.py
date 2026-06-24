"""P3b-1 (attrs + NamedTuple classification) and P3b-2 (constraint surfacing). Offline, ast-only."""
import ast
from pathlib import Path

from scripts.adapters import python_pytest as adapter
from scripts.core import generate
from scripts.core.models import TargetRef, TypeSpec, FieldSpec, TargetContract, UnitSpec


def _cls(src: str) -> ast.ClassDef:
    return ast.parse(src).body[0]


# ── P3b-1: classification ────────────────────────────────────────────────────
def test_classify_attrs_modern_and_classic():
    assert adapter._classify(_cls("@define\nclass P:\n    x: int\n")) == "attrs"
    assert adapter._classify(_cls("@frozen\nclass P:\n    x: int\n")) == "attrs"
    assert adapter._classify(_cls("@attr.s\nclass P:\n    x = attr.ib()\n")) == "attrs"
    assert adapter._classify(_cls("@attrs.define\nclass P:\n    x: int\n")) == "attrs"


def test_classify_namedtuple_classform():
    assert adapter._classify(_cls("class Pair(NamedTuple):\n    a: int\n    b: str\n")) == "namedtuple"


def test_classify_still_detects_existing_kinds():
    assert adapter._classify(_cls("@dataclass\nclass D:\n    x: int\n")) == "dataclass"
    assert adapter._classify(_cls("class M(BaseModel):\n    x: int\n")) == "pydantic"
    assert adapter._classify(_cls("class E(Enum):\n    A = 1\n")) == "enum"
    assert adapter._classify(_cls("class Plain:\n    x = 1\n")) is None       # unchanged warn-and-skip


# ── P3b-1: old-style attrs field extraction ──────────────────────────────────
def test_extract_fields_oldstyle_attrs_defaults():
    cls = _cls("@attr.s\nclass P:\n    x = attr.ib()\n    y = attr.ib(default=0)\n    _hidden = attr.ib()\n")
    fields = {f.name: f for f in adapter._extract_fields(cls)}
    assert set(fields) == {"x", "y"}                 # private skipped
    assert fields["x"].has_default is False          # required
    assert fields["y"].has_default is True           # has a default


# ── P3b-2: constraint extraction ─────────────────────────────────────────────
def test_constraint_from_pydantic_field():
    cls = _cls("class M(BaseModel):\n    q: int = Field(gt=0, le=100)\n    name: str = Field(min_length=1)\n")
    fields = {f.name: f for f in adapter._extract_fields(cls)}
    assert fields["q"].constraint == "gt=0, le=100"
    assert fields["q"].has_default is False           # constraint-only Field = required
    assert fields["name"].constraint == "min_length=1"


def test_constraint_from_con_annotation_and_annotated():
    c1 = adapter._extract_fields(_cls("class C(BaseModel):\n    n: conint(ge=1, le=9)\n"))[0]
    assert c1.constraint == "ge=1, le=9"
    c2 = adapter._extract_fields(_cls("class A(BaseModel):\n    x: Annotated[int, Field(gt=0)]\n"))[0]
    assert c2.constraint == "gt=0"


# ── P3b-2: constraints reach the prompt ──────────────────────────────────────
def test_contract_block_renders_constraints_and_note():
    contract = TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py"),
        module="m",
        units=[UnitSpec(name="f", signature="(m: M)")],
        types={"M": TypeSpec(name="M", kind="pydantic", module="m",
                             fields=[FieldSpec(name="q", annotation="int", constraint="gt=0, le=100")])},
    )
    block = generate._contract_block(contract)
    assert "q: int [gt=0, le=100]" in block
    assert "constraints" in block and "construct" in block


# ── P3b-1: end-to-end introspection resolves attrs + NamedTuple params ────────
def test_introspect_resolves_attrs_and_namedtuple(tmp_path: Path):
    mod = tmp_path / "geo.py"
    mod.write_text(
        "from attrs import define\n"
        "from typing import NamedTuple\n"
        "@define\n"
        "class Point:\n    x: int\n    y: int = 0\n"
        "class Span(NamedTuple):\n    lo: int\n    hi: int\n"
        "def area(p: Point, s: Span) -> int:\n    return p.x * (s.hi - s.lo)\n",
        encoding="utf-8",
    )
    contract = adapter.introspect(TargetRef(adapter="python_pytest", locator=str(mod), selector="area"))
    assert contract.types["Point"].kind == "attrs"
    assert contract.types["Span"].kind == "namedtuple"
    assert contract.units[0].complex_params == []     # both resolved → constructible, not skipped
