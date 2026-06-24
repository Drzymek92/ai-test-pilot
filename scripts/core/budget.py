"""P4 — cost/scale guardrails: a-priori token estimate, cap enforcement, and cost.

Spend is always *measured* (the LLM client returns real usage; P4-1). This module adds the
*estimate-before-spend* half: input tokens are countable from the prompt; output tokens are
unknowable a priori, so we estimate from the ledger's own history (avg output per scenario for
this adapter) with a configurable heuristic fallback. The cap is therefore a **guardrail, not a
guarantee** — it bounds surprise, it doesn't promise an exact spend.

Deterministic, stdlib-only (a char/4 token heuristic — no tokenizer dependency).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from scripts.core import ledger as ledger_mod
from scripts.core.errors import BudgetError
from scripts.logger import get_logger

logger = get_logger("budget")

_CHARS_PER_TOKEN = 4   # rough, model-agnostic; good enough for a guardrail estimate


@dataclass
class Budget:
    max_tokens_per_run: int = 0
    max_tokens_per_sweep: int = 0
    on_over: str = "warn"               # warn | abort
    price_in: float = 0.0               # USD / 1M input tokens
    price_out: float = 0.0
    default_out_per_scenario: int = 200
    ledger_path: Path | None = None

    @property
    def enabled(self) -> bool:
        return self.max_tokens_per_run > 0 or self.max_tokens_per_sweep > 0


def estimate_input_tokens(*texts: str) -> int:
    return math.ceil(sum(len(t) for t in texts) / _CHARS_PER_TOKEN)


def estimate_output_tokens(adapter: str, count: int, budget: Budget) -> int:
    """Per-scenario output from ledger history (avg) × count, else the configured heuristic."""
    avg = None
    if budget.ledger_path is not None:
        avg = ledger_mod.avg_tokens_per_scenario(adapter, budget.ledger_path)
    per = avg if avg else budget.default_out_per_scenario
    return int(round(per * max(1, count)))


def cost(tokens_in: int, tokens_out: int, budget: Budget) -> float:
    return round(tokens_in / 1e6 * budget.price_in + tokens_out / 1e6 * budget.price_out, 6)


def estimate_call(system: str, human: str, *, adapter: str, count: int, budget: Budget) -> dict:
    """{input, output, total} estimate for one generation call (cache misses only)."""
    ti = estimate_input_tokens(system, human)
    to = estimate_output_tokens(adapter, count, budget)
    return {"input": ti, "output": to, "total": ti + to}


def enforce(estimated_total: int, cap: int, *, on_over: str, scope: str) -> None:
    """Apply a cap: raise BudgetError if `abort`, else log a warning. No-op when cap<=0."""
    if cap <= 0 or estimated_total <= cap:
        return
    msg = f"{scope} estimate {estimated_total} tokens exceeds cap {cap}."
    if on_over == "abort":
        raise BudgetError(msg + " Aborting (on_over=abort).")
    logger.warning("%s Proceeding (on_over=warn).", msg)
