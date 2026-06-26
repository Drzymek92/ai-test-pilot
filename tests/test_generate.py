"""Generation-stage parsing/validation tests (LLM call monkeypatched)."""
import json

import pytest

from scripts.core import generate
from scripts.core.models import TargetContract, TargetRef, UnitSpec


def _contract() -> TargetContract:
    return TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="add"),
        module="m",
        units=[UnitSpec(name="add", signature="(a, b)")],
    )


def test_extract_json_strips_fence():
    raw = "```json\n[{\"x\": 1}]\n```"
    assert generate._extract_json(raw) == [{"x": 1}]


def test_extract_json_unwraps_scenarios_key():
    raw = json.dumps({"scenarios": [{"x": 1}]})
    assert generate._extract_json(raw) == [{"x": 1}]


def test_parse_rejects_unknown_unit():
    raw = json.dumps([{"id": "a", "title": "t", "unit": "ghost", "expected": "e"}])
    with pytest.raises(ValueError):
        generate._parse_scenarios(raw, _contract())


def test_parse_accepts_valid_scenario():
    raw = json.dumps([{
        "id": "a", "title": "t", "unit": "add",
        "inputs": {"a": 1, "b": 2}, "expected": "3", "assertion": "result == 3",
        "tags": ["happy_path"],
    }])
    scenarios = generate._parse_scenarios(raw, _contract())
    assert len(scenarios) == 1 and scenarios[0].unit == "add"


def test_generate_scenarios_happy_path(monkeypatch):
    payload = json.dumps([{
        "id": "a", "title": "t", "unit": "add",
        "inputs": {"a": 1, "b": 2}, "expected": "3", "assertion": "result == 3",
    }])
    monkeypatch.setattr(generate, "llm_call", lambda *a, **k: payload)
    result = generate.generate_scenarios(_contract(), count=3, prompt_version="v1")
    assert len(result.scenarios) == 1


def test_generate_scenarios_repairs_then_succeeds(monkeypatch):
    calls = {"n": 0}
    good = json.dumps([{"id": "a", "title": "t", "unit": "add",
                        "expected": "3", "assertion": "result == 3"}])

    def fake_llm(prompt, **k):
        calls["n"] += 1
        return "not json" if calls["n"] == 1 else good

    monkeypatch.setattr(generate, "llm_call", fake_llm)
    result = generate.generate_scenarios(_contract(), repair_retries=1)
    assert calls["n"] == 2 and len(result.scenarios) == 1


def test_generate_scenarios_exhausts_retries(monkeypatch):
    from scripts.core.errors import LLMError
    monkeypatch.setattr(generate, "llm_call", lambda *a, **k: "still not json")
    with pytest.raises(LLMError):     # parse-exhaustion = clean exit-4 LLMError, caught by detection/sweep
        generate.generate_scenarios(_contract(), repair_retries=1)
