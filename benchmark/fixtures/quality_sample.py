"""In-repo curated quality target — known-good, deterministic, no sibling-project dependency.

Exercises the P3a/P3b paths: a no-docstring function (CUT source assertions), a constrained
pydantic model, and a NamedTuple. Pure + side-effect-free so the quality gate can run it anywhere
(including the portfolio copy where sibling projects are absent).
"""
from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel, Field


class Money(NamedTuple):
    amount: int
    currency: str


class LineItem(BaseModel):
    qty: int = Field(gt=0, le=1000)
    unit_price: int = Field(ge=0)


def line_total(item: LineItem, fee: Money) -> int:
    return item.qty * item.unit_price + fee.amount


def clamp_qty(value: int, lo: int = 0, hi: int = 100) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def split_codes(raw: str) -> list[str]:
    return [c.strip().upper() for c in raw.split(",") if c.strip()]
