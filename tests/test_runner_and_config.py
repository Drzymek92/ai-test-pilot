"""Runner JUnit parsing, config loading, and fixture-selection tests (all offline)."""
from pathlib import Path

from scripts.adapters import python_pytest as adapter
from scripts.config import load_config
from scripts.core import runner
from scripts.core.fixtures import FixtureBundle, _pick_output, bind_fixture_files, prompt_block
from scripts.core.models import ScenarioSet, TargetRef, TestScenario, TmpFile


_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
  <testcase classname="t" name="test_pass"/>
  <testcase classname="t" name="test_fail"><failure type="AssertionError" message="boom"/></testcase>
  <testcase classname="t" name="test_err"><error type="ImportError" message="no mod"/></testcase>
</testsuite></testsuites>"""


def test_parse_junit(tmp_path: Path):
    p = tmp_path / "r.xml"
    p.write_text(_JUNIT, encoding="utf-8")
    parsed = runner._parse_junit(p)
    assert parsed["test_pass"][0] == "passed"
    assert parsed["test_fail"] == ("failed", "AssertionError", "boom")
    assert parsed["test_err"] == ("error", "ImportError", "no mod")


def test_build_cmd_uses_interpreter(tmp_path: Path):
    cmd = runner._build_cmd(adapter, tmp_path / "t.py", tmp_path / "o.xml")
    assert cmd[1:3] == ["-m", "pytest"]            # invoked via this interpreter
    assert any(c.startswith("--junit-xml=") for c in cmd)


def test_load_config_defaults():
    cfg = load_config()                            # the committed ai_test_pilot.toml
    assert cfg.run.adapter == "python_pytest"
    assert cfg.fixtures.enabled is False
    assert cfg.generation.repair_retries == 1


def test_pick_output_prefers_entity(tmp_path: Path):
    a = tmp_path / "marketplace_offers_1.csv"
    b = tmp_path / "marketplace_buyers_1.csv"
    for f in (a, b):
        f.write_text("x\n1\n", encoding="utf-8")
    picked = _pick_output(tmp_path, before=set(), domain="marketplace", entity="offers")
    assert picked == a


def test_pick_output_skips_chat_jsonl_and_old(tmp_path: Path):
    old = tmp_path / "marketplace_offers_0.csv"
    old.write_text("x\n", encoding="utf-8")
    new = tmp_path / "marketplace_offers_1.csv"
    new.write_text("x\n1\n2\n", encoding="utf-8")
    picked = _pick_output(tmp_path, before={old}, domain="marketplace", entity=None)
    assert picked == new                           # only the file not in `before`


def test_prompt_block_contains_sample_and_file_hint():
    bundle = FixtureBundle(domain="marketplace", records=[{"id": "OFR-1", "price": "9"}])
    block = prompt_block(bundle)
    assert "OFR-1" in block and "marketplace" in block
    assert "from_fixture" in block and "columns" in block        # file-bridge hint present


def test_bind_fixture_files_fills_real_content(tmp_path: Path):
    csv_path = tmp_path / "offers.csv"
    csv_path.write_text("name,category,price_pln\nThing,home,99\n", encoding="utf-8")
    bundle = FixtureBundle(domain="marketplace_offers", records=[{"name": "Thing"}], path=csv_path)
    ss = ScenarioSet(
        target=TargetRef(adapter="python_pytest", locator="m.py"),
        scenarios=[TestScenario(
            id="lc", title="load", unit="load_catalog", expected="rows",
            assertion="len(result) >= 1",
            tmp_files=[TmpFile(param="factory_csv", filename="x.csv", from_fixture=True)],
        )],
    )
    n = bind_fixture_files(ss, bundle)
    assert n == 1
    tf = ss.scenarios[0].tmp_files[0]
    assert "price_pln" in tf.text and "Thing" in tf.text         # real content injected
    assert ss.scenarios[0].fixture == "marketplace_offers"       # tagged


def test_bind_fixture_files_noop_without_bundle():
    ss = ScenarioSet(target=TargetRef(adapter="python_pytest", locator="m.py"), scenarios=[])
    assert bind_fixture_files(ss, None) == 0
