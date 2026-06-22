"""Deterministic introspection + emission tests (no LLM)."""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core.models import TargetContract, TargetRef, TestScenario, TmpFile, UnitSpec


def _contract(sample_module: Path, selector: str | None = None) -> TargetContract:
    ref = TargetRef(adapter="python_pytest", locator=str(sample_module), selector=selector)
    return adapter.introspect(ref)


def test_introspect_extracts_signature_and_defaults(sample_module):
    c = _contract(sample_module, selector="add")
    assert len(c.units) == 1
    add = c.units[0]
    assert add.name == "add"
    assert add.returns == "int"
    assert add.docstring == "Add two ints."
    names = [p.name for p in add.params]
    assert names == ["a", "b"]
    assert add.params[1].default == "1"          # default captured


def test_introspect_detects_raises_and_purity(sample_module):
    c = _contract(sample_module)
    by_name = {u.name: u for u in c.units}
    assert "ValueError" in by_name["needs_positive"].raises
    assert by_name["add"].is_pure is True
    assert by_name["reads_a_file"].is_pure is False    # open() → impure


def test_introspect_selector_filters(sample_module):
    c = _contract(sample_module, selector="add,needs_positive")
    assert {u.name for u in c.units} == {"add", "needs_positive"}


def test_introspect_unknown_selector_raises(sample_module):
    ref = TargetRef(adapter="python_pytest", locator=str(sample_module), selector="nope")
    with pytest.raises(ValueError):
        adapter.introspect(ref)


def test_resolve_import_loose_file(sample_module):
    root, module = adapter.resolve_import(sample_module)
    assert root == sample_module.parent
    assert module == "sample_target"


def test_emit_normal_and_error_scenarios(sample_module):
    c = _contract(sample_module)
    happy = TestScenario(id="add-ok", title="adds", unit="add",
                         inputs={"a": 2, "b": 3}, expected="five",
                         assertion="result == 5", tags=["happy_path"])
    err = TestScenario(id="neg", title="rejects negative", unit="needs_positive",
                       inputs={"n": -1}, expected="raises", expect_error="ValueError",
                       tags=["error"])
    src_happy = adapter.emit(happy, c)
    src_err = adapter.emit(err, c)
    assert "def test_add_ok():" in src_happy
    assert "result = add(a=2, b=3)" in src_happy
    assert "assert result == 5" in src_happy
    assert "with pytest.raises(ValueError):" in src_err
    assert "needs_positive(n=-1)" in src_err


def test_emit_includes_fixture_note(sample_module):
    c = _contract(sample_module, selector="add")
    s = TestScenario(id="x", title="t", unit="add", inputs={"a": 1}, expected="e",
                     assertion="result == 2", fixture="marketplace")
    src = adapter.emit(s, c)
    assert "Fixture (synthetic-data-factory): marketplace" in src
    # the closing docstring quotes must stay on their own line (the trim_blocks bug)
    assert '\n    """\n' in src


def test_emit_tmp_files_creates_real_input(sample_module):
    c = _contract(sample_module, selector="reads_a_file")
    s = TestScenario(
        id="reads", title="reads a file", unit="reads_a_file",
        tmp_files=[TmpFile(param="path", filename="in.txt", text="hello\n")],
        expected="returns the file contents", assertion="result == 'hello\\n'",
        tags=["happy_path"],
    )
    src = adapter.emit(s, c)
    assert "def test_reads(tmp_path):" in src                 # tmp_path fixture injected
    assert "p_path = tmp_path / 'in.txt'" in src
    assert "p_path.write_text('hello\\n', encoding='utf-8')" in src
    assert "reads_a_file(path=p_path)" in src                 # Path object bound, not fabricated


def test_file_header_imports_units(sample_module):
    c = _contract(sample_module, selector="add,needs_positive")
    header = adapter.file_header(c)
    assert "import pytest" in header
    assert "from sample_target import add, needs_positive" in header
    assert "sys.path.insert(0," in header


def test_function_name_matches_slug():
    s = TestScenario(id="weird id!!", title="t", unit="add", expected="e")
    assert adapter.test_function_name(s) == "test_weird_id"


def test_constructible_vs_complex_annotations():
    assert adapter._constructible("int")
    assert adapter._constructible("Optional[list[dict]]")
    assert adapter._constructible("Path")
    assert not adapter._constructible("OrderView")
    assert not adapter._constructible("RulesConfig | None")


def test_unresolved_complex_params_flagged(tmp_path):
    # Types that are NOT defined/importable in the project stay unresolved → flagged.
    p = tmp_path / "rules.py"
    p.write_text(
        "def compute(order: OrderView, config: RulesConfig, n: int = 1) -> int:\n"
        "    return n\n",
        encoding="utf-8",
    )
    ref = TargetRef(adapter="python_pytest", locator=str(p), selector="compute")
    contract = adapter.introspect(ref)
    u = contract.units[0]
    assert u.complex_params == ["order: OrderView", "config: RulesConfig"]   # unresolved → flagged
    assert contract.types == {}                                              # nothing resolvable
