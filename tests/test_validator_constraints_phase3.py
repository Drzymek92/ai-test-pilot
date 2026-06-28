"""Phase 3 — validator constant-seeding: lift the valid value set from pydantic
`@field_validator` / `@model_validator` bodies into the field constraints surfaced to the model,
so it stops proposing values that trip the validator (the green-erosion gap on coupon/quote).
ast-only — no validator is ever executed."""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core.generate import _contract_block
from scripts.core.models import TargetRef


_MODELS = '''\
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator


class Coupon(BaseModel):
    code: str
    percent: int

    @field_validator("percent")
    @classmethod
    def _allowed(cls, v: int) -> int:
        if v not in (5, 10, 15, 20):
            raise ValueError("percent must be one of 5/10/15/20")
        return v


class Quote(BaseModel):
    base: int = Field(ge=0)
    tax_rate: Decimal
    rush: bool = False

    @field_validator("tax_rate")
    @classmethod
    def _rate(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("0.30")):
            raise ValueError("tax_rate must be within [0, 0.30]")
        return v

    @model_validator(mode="after")
    def _rush(self) -> "Quote":
        if self.rush and self.base < 100:
            raise ValueError("rush quotes require base >= 100")
        return self


class Plain(BaseModel):
    x: int
'''

_TARGET = '''\
from models import Coupon, Quote, Plain


def use_coupon(c: Coupon) -> int:
    return c.percent


def use_quote(q: Quote) -> int:
    return q.base


def use_plain(p: Plain) -> int:
    return p.x
'''


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return target


def _constraint(contract, type_name, field_name):
    f = next(f for f in contract.types[type_name].fields if f.name == field_name)
    return f.constraint


def _contract(target: Path, selector: str):
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector=selector))


def test_field_validator_membership_surfaced(proj):
    c = _contract(proj, "use_coupon")
    assert _constraint(c, "Coupon", "percent") == "percent must be one of 5/10/15/20"
    assert _constraint(c, "Coupon", "code") is None        # untouched field carries no constraint


def test_field_validator_range_surfaced(proj):
    c = _contract(proj, "use_quote")
    assert _constraint(c, "Quote", "tax_rate") == "tax_rate must be within [0, 0.30]"


def test_model_validator_attaches_to_referenced_fields(proj):
    c = _contract(proj, "use_quote")
    # the cross-field rule references self.rush and self.base → attached to BOTH
    assert "rush quotes require base >= 100" in _constraint(c, "Quote", "rush")
    assert "rush quotes require base >= 100" in _constraint(c, "Quote", "base")


def test_structured_and_validator_constraints_merge(proj):
    """base has both a declarative Field(ge=0) AND the model_validator rule → both surfaced."""
    c = _contract(proj, "use_quote")
    base_c = _constraint(c, "Quote", "base")
    assert "ge=0" in base_c and "rush quotes require base >= 100" in base_c


def test_no_validator_no_constraint(proj):
    c = _contract(proj, "use_plain")
    assert _constraint(c, "Plain", "x") is None


def test_constraints_reach_the_prompt(proj):
    c = _contract(proj, "use_coupon")
    block = _contract_block(c)
    assert "percent must be one of 5/10/15/20" in block
    # the prompt tells the model to satisfy bracketed constraints
    assert "constraints" in block.lower()
