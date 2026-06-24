"""P4 — spend accounting (usage capture) + budget estimate/cap. Offline."""
import json

import pytest

from scripts import llm_client
from scripts.config import load_config
from scripts.core import budget as budget_mod
from scripts.core import generate
from scripts.core.budget import Budget
from scripts.core.errors import BudgetError
from scripts.core.models import TargetContract, TargetRef, UnitSpec


def _contract():
    return TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="add"),
        module="m", units=[UnitSpec(name="add", signature="(a, b)")])


_PAYLOAD = json.dumps([{"id": "a", "title": "t", "unit": "add",
                        "inputs": {"a": 1, "b": 2}, "expected": "3", "assertion": "result == 3"}])


# ── P4-1: usage extraction + capture ─────────────────────────────────────────
def test_extract_usage_from_usage_metadata():
    msg = type("M", (), {"usage_metadata": {"input_tokens": 100, "output_tokens": 40}})()
    assert llm_client._extract_usage(msg) == {"input_tokens": 100, "output_tokens": 40}


def test_extract_usage_falls_back_to_response_metadata():
    msg = type("M", (), {"usage_metadata": None,
                         "response_metadata": {"token_usage": {"prompt_tokens": 7, "completion_tokens": 3}}})()
    assert llm_client._extract_usage(msg) == {"input_tokens": 7, "output_tokens": 3}


def test_generate_records_token_spend(monkeypatch):
    monkeypatch.setattr(generate, "llm_call",
                        lambda *a, **k: (_PAYLOAD, {"input_tokens": 120, "output_tokens": 30}))
    ss = generate.generate_scenarios(_contract(), prompt_version="v1")
    assert ss.tokens_in == 120 and ss.tokens_out == 30


def test_cache_replay_spends_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "llm_call",
                        lambda *a, **k: (_PAYLOAD, {"input_tokens": 120, "output_tokens": 30}))
    kw = dict(prompt_version="v1", cache_dir=tmp_path, use_cache=True)
    first = generate.generate_scenarios(_contract(), **kw)
    second = generate.generate_scenarios(_contract(), **kw)        # cache hit
    assert first.tokens_out == 30 and second.tokens_in == 0 and second.tokens_out == 0


# ── P4-2: estimation + cost + cap ────────────────────────────────────────────
def test_estimate_and_cost():
    b = Budget(default_out_per_scenario=200, price_in=1.0, price_out=2.0)
    est = budget_mod.estimate_call("sys", "x" * 400, adapter="python_pytest", count=3, budget=b)
    assert est["input"] == 101                       # ceil((3 + 400)/4)
    assert est["output"] == 600                       # 200 * 3 (no ledger history)
    assert budget_mod.cost(1_000_000, 500_000, b) == 2.0   # 1*1.0 + 0.5*2.0


def test_enforce_warns_then_aborts():
    budget_mod.enforce(100, cap=50, on_over="warn", scope="run")        # no raise
    budget_mod.enforce(100, cap=0, on_over="abort", scope="run")        # disabled cap → no raise
    with pytest.raises(BudgetError):
        budget_mod.enforce(100, cap=50, on_over="abort", scope="run")


def test_generation_aborts_over_cap(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(generate, "llm_call",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (_PAYLOAD, {}))
    b = Budget(max_tokens_per_run=10, on_over="abort", default_out_per_scenario=500)
    with pytest.raises(BudgetError):
        generate.generate_scenarios(_contract(), prompt_version="v1", count=6, budget=b)
    assert called["n"] == 0                           # aborted BEFORE the LLM call


def test_config_exposes_budget_defaults():
    cfg = load_config()
    assert cfg.budget.on_over == "warn"               # safe default: never blocks
    assert cfg.budget.max_tokens_per_run == 0         # opt-in caps
