"""P3a — CUT source context fed into generation for specific-behaviour assertions. Offline."""
from pathlib import Path

from scripts.adapters import python_pytest as adapter
from scripts.config import load_config
from scripts.core import generate
from scripts.core.models import TargetContract, TargetRef, UnitSpec


def _contract_with_source(src: str = "def add(a, b):\n    return a + b") -> TargetContract:
    return TargetContract(
        ref=TargetRef(adapter="python_pytest", locator="m.py", selector="add"),
        module="m",
        units=[UnitSpec(name="add", signature="(a, b)", source=src)],
    )


def test_introspect_captures_unit_source(tmp_path: Path):
    p = tmp_path / "m.py"
    p.write_text('def add(a, b):\n    """Sum."""\n    return a + b\n', encoding="utf-8")
    contract = adapter.introspect(TargetRef(adapter="python_pytest", locator=str(p), selector="add"))
    assert contract.units[0].source is not None
    assert "return a + b" in contract.units[0].source


def test_contract_block_includes_source_when_enabled():
    block = generate._contract_block(_contract_with_source(), include_source=True)
    assert "```python" in block and "return a + b" in block
    assert "the code under test" in block


def test_contract_block_omits_source_when_disabled():
    block = generate._contract_block(_contract_with_source(), include_source=False)
    assert "return a + b" not in block and "```python" not in block


def test_source_is_truncated_to_budget():
    big = "def f():\n" + "\n".join(f"    x{i} = {i}" for i in range(500))
    out = generate._truncate_source(big, 200)
    assert len(out) <= 200 + len("\n# ...(source truncated for token budget)")
    assert "source truncated" in out


def test_config_defaults_enable_cut_source():
    cfg = load_config()
    assert cfg.generation.cut_source is True
    assert cfg.generation.cut_source_max_chars == 1200


def test_generate_feeds_source_into_prompt(monkeypatch):
    seen = {}

    def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return '[{"id":"a","title":"t","unit":"add","inputs":{"a":1,"b":2},' \
               '"expected":"3","assertion":"result == 3"}]'

    monkeypatch.setattr(generate, "llm_call", fake_llm)
    generate.generate_scenarios(_contract_with_source(), prompt_version="v1", include_source=True)
    assert "return a + b" in seen["prompt"]                  # the CUT source reached the model
