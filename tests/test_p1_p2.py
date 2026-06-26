"""P1 (reproducibility: temp=0 + scenario cache) and P2 (fail-safe: retry/timeout +
exit-code contract) — all offline (LLM + subprocess monkeypatched)."""
import json
import subprocess

import pytest

from scripts import llm_client
from scripts.config import load_config
from scripts.core import cache, generate, runner
from scripts.core.errors import LLMError, TargetError
from scripts.core.models import ScenarioSet, TargetContract, TargetRef, TestScenario, UnitSpec


def _contract() -> TargetContract:
    return TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="add"),
        module="m",
        units=[UnitSpec(name="add", signature="(a, b)")],
    )


_PAYLOAD = json.dumps([{
    "id": "a", "title": "t", "unit": "add",
    "inputs": {"a": 1, "b": 2}, "expected": "3", "assertion": "result == 3",
}])


# ── P1: config defaults ──────────────────────────────────────────────────────
def test_config_defaults_are_deterministic_and_safe():
    cfg = load_config()
    assert cfg.generation.temperature == 0.0       # P1: deterministic by default
    assert cfg.generation.cache is True            # P1: scenario cache on
    assert cfg.generation.llm_retries == 2         # P2
    assert cfg.generation.per_test_timeout == 15.0  # P2


# ── P1: cache key sensitivity ────────────────────────────────────────────────
def test_cache_key_is_stable_and_sensitive():
    base = dict(system="S", human="H", model="m1", temperature=0.0, count=6)
    k = cache.cache_key(**base)
    assert k == cache.cache_key(**base)                          # stable
    assert k != cache.cache_key(**{**base, "model": "m2"})       # H1: model drift invalidates
    assert k != cache.cache_key(**{**base, "temperature": 0.5})  # temp invalidates
    assert k != cache.cache_key(**{**base, "human": "H2"})       # target/prompt change invalidates


def test_cache_round_trip(tmp_path):
    ss = ScenarioSet(target=TargetRef(adapter="python_pytest", locator="m.py"),
                     scenarios=[TestScenario(id="a", title="t", unit="add",
                                             expected="3", assertion="result == 3")])
    assert cache.load(tmp_path, "k") is None                     # miss
    cache.store(tmp_path, "k", ss)
    got = cache.load(tmp_path, "k")
    assert got is not None and got.scenarios[0].unit == "add"


# ── P1: generate uses the cache ──────────────────────────────────────────────
def test_generate_caches_then_replays_without_llm(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_llm(*a, **k):
        calls["n"] += 1
        return _PAYLOAD

    monkeypatch.setattr(generate, "llm_call", fake_llm)
    kw = dict(prompt_version="v1", cache_dir=tmp_path, use_cache=True)
    r1 = generate.generate_scenarios(_contract(), **kw)
    r2 = generate.generate_scenarios(_contract(), **kw)
    assert calls["n"] == 1                          # second call served from cache
    assert r1.scenarios[0].unit == r2.scenarios[0].unit == "add"


def test_refresh_cache_forces_regeneration(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(generate, "llm_call", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or _PAYLOAD))
    generate.generate_scenarios(_contract(), prompt_version="v1", cache_dir=tmp_path, use_cache=True)
    generate.generate_scenarios(_contract(), prompt_version="v1", cache_dir=tmp_path,
                                use_cache=True, refresh_cache=True)
    assert calls["n"] == 2                           # refresh bypassed the hit, regenerated


# ── P2: LLM retry/backoff → LLMError ─────────────────────────────────────────
def test_llm_call_retries_then_raises(monkeypatch):
    attempts = {"n": 0}

    class _Boom:
        def invoke(self, _messages):
            attempts["n"] += 1
            raise ConnectionError("gateway down")

    monkeypatch.setattr(llm_client, "get_llm", lambda **k: _Boom())
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)   # no real backoff wait
    with pytest.raises(LLMError):
        llm_client.llm_call("hi", retries=2)
    assert attempts["n"] == 3                          # first try + 2 retries


def test_llm_call_succeeds_after_transient_failure(monkeypatch):
    attempts = {"n": 0}

    class _Flaky:
        def invoke(self, _messages):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("slow")
            return type("R", (), {"content": "ok"})()

    monkeypatch.setattr(llm_client, "get_llm", lambda **k: _Flaky())
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)
    assert llm_client.llm_call("hi", retries=2) == "ok"
    assert attempts["n"] == 2


def test_llm_call_is_hard_bounded_on_a_wedged_socket(monkeypatch):
    """A wedged `invoke` that ignores the client timeout must NOT hang the call: the hard
    wall-clock deadline abandons it, the retry loop runs, and LLMError is raised promptly.
    (Regression for the gateway wedge that hung a whole `detect` run for hours.)"""
    import threading as _thr
    import time as _t

    class _Wedged:
        def invoke(self, _messages):
            _thr.Event().wait(30)                       # block (not time.sleep, which the patch stubs)
            return type("R", (), {"content": "never"})()

    monkeypatch.setattr(llm_client, "get_llm", lambda **k: _Wedged())
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_: None)   # skip backoff waits
    start = _t.monotonic()
    with pytest.raises(LLMError):
        # timeout=1 → hard deadline 1.5s; 2 attempts ≈ 3s, nowhere near 30s × attempts.
        llm_client.llm_call("hi", timeout=1, retries=1)
    assert _t.monotonic() - start < 10                  # returned control, did not hang on the 30s sleep


# ── P2: runner bounds a hanging test ─────────────────────────────────────────
def test_runner_timeout_marks_all_timed_out(tmp_path, monkeypatch):
    from scripts.adapters import python_pytest as adapter

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=k.get("timeout", 1))

    monkeypatch.setattr(runner.subprocess, "run", boom)
    ss = ScenarioSet(target=TargetRef(adapter="python_pytest", locator="m.py"),
                     scenarios=[TestScenario(id="a", title="t", unit="add",
                                             expected="3", assertion="result == 3")])
    results = runner.run_tests(adapter, tmp_path / "test_m.py", ss, per_test_timeout=0.01)
    assert len(results) == 1
    assert results[0].status == "error" and results[0].signal == "timeout"


# ── P2: exit-code contract via main() ────────────────────────────────────────
def test_main_target_error_exit_3(tmp_path):
    from scripts import main
    bad = tmp_path / "broken.py"
    bad.write_text("def add(a, b):\n    return a +\n", encoding="utf-8")   # syntax error
    assert main.main(["--target", str(bad), "--no-run"]) == main.EXIT_TARGET


def test_main_llm_error_exit_4(tmp_path, monkeypatch):
    from scripts import main
    good = tmp_path / "ok.py"
    good.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    def boom(*a, **k):
        raise LLMError("gateway exhausted")

    monkeypatch.setattr(generate, "llm_call", boom)
    assert main.main(["--target", str(good), "--no-run", "--no-cache"]) == main.EXIT_LLM
