"""Phase-0 substrate — the typed-input LONG TAIL that is still warn-and-skip / green~0 today.

Manufactured to MEASURE the gap that Phases 1-3 of `design/IMPROVEMENT_APPROACHES.md` will close.
Unlike `benchmark/fixtures/a3_typed_sample.py` (plain classes A3 ALREADY constructs via `__init__`),
every param type below is a shape `LIMITATIONS.md` lists as deliberately NOT constructed correctly
today, so the generated suite produces no usable green baseline (green~0) and can catch no bug:

  * Coupon   -> custom pydantic ``@field_validator`` with a NARROW valid set the model won't hit
                blindly (the tool builds the real model but doesn't surface the validator) -> Phase 3
  * Quote    -> nested pydantic + Decimal + a cross-field ``@model_validator`` (the commission
                green~0 analog: validator-heavy config) -> Phase 2
  * Payment  -> ``Union[Cash, Card]`` of several project types -> Phase 1
  * Account  -> factory-built, opaque no-arg ``__init__`` (real construction is ``open_account``)
                -> Phase 1

Functions are arithmetic / comparison / boolean-heavy so the AST mutation operators seed catchable
bugs: the construction gap (NOT assertion strength) is what keeps the kill rate ~0 here today.
Dependency-light (pydantic + stdlib) + deterministic so it slots into the detection corpus via
`benchmark/detection_targets_typed.toml`. This is a SEPARATE corpus — its numbers must NOT be folded
into `detection_baseline.json` / `EVAL_DETECTION.md`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Union

from pydantic import BaseModel, field_validator, model_validator


# --- T1: custom @field_validator (narrow valid set) ------------------------------ Phase 3
class Coupon(BaseModel):
    code: str
    percent: int

    @field_validator("percent")
    @classmethod
    def _allowed(cls, v: int) -> int:
        if v not in (5, 10, 15, 20):
            raise ValueError("percent must be one of 5 / 10 / 15 / 20")
        return v


def discounted_price(price: int, coupon: Coupon) -> int:
    """Price after applying the coupon's whole-percent discount (rounded down)."""
    return price - price * coupon.percent // 100


# --- T2: nested pydantic + Decimal + cross-field @model_validator ----------------- Phase 2
class Quote(BaseModel):
    base: int
    tax_rate: Decimal          # e.g. Decimal("0.23"); valid only within [0, 0.30]
    rush: bool = False

    @field_validator("tax_rate")
    @classmethod
    def _rate_range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("0.30")):
            raise ValueError("tax_rate must be within [0, 0.30]")
        return v

    @model_validator(mode="after")
    def _rush_needs_margin(self) -> "Quote":
        if self.rush and self.base < 100:
            raise ValueError("rush quotes require base >= 100")
        return self


def quote_total(quote: Quote) -> int:
    """Base plus tax (rounded down); a rush quote adds a flat 50 surcharge."""
    taxed = quote.base + int(quote.base * quote.tax_rate)
    return taxed + 50 if quote.rush else taxed


# --- T3: Union of several project types ------------------------------------------- Phase 1
class Cash(BaseModel):
    amount: int


class Card(BaseModel):
    amount: int
    fee_bps: int               # processing fee in basis points (1/100 of a percent)


def settle(payment: Union[Cash, Card]) -> int:
    """Net settled amount: Cash settles in full; a Card nets its fee (bps of amount, rounded down)."""
    if isinstance(payment, Card):
        return payment.amount - payment.amount * payment.fee_bps // 10000
    return payment.amount


# --- T4: factory-built plain class, opaque no-arg __init__ ------------------------- Phase 1
class Account:
    def __init__(self) -> None:
        # Internal default state; real accounts are built via open_account(...).
        self.balance = 0
        self.tier = "standard"


def open_account(balance: int, tier: str) -> Account:
    acct = Account()
    acct.balance = balance
    acct.tier = tier
    return acct


def credit_limit(account: Account, multiplier: int) -> int:
    """A gold account's credit is balance * multiplier; any other tier gets its balance only."""
    if account.tier == "gold":
        return account.balance * multiplier
    return account.balance
