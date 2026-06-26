"""Security: a model-authored assertion / expect_error may only reference symbols the tool
itself resolved — never arbitrary code.

`pytest_function_v1.j2` interpolates `s.assertion` and `s.expect_error` RAW into executed code
(`assert {{ s.assertion }}` and `with pytest.raises({{ s.expect_error }})`). Since the target's
source/docstrings are fed to the prompt by default, a crafted scenario must NOT be able to author
code there (e.g. `__import__('os').system(...) or True`). `validate_scenario` AST-walks both fields
and allow-lists every name; it is enforced at generation time so a violation routes through the
repair-retry. This is the assertion-side companion to test_value_grammar_safety.py.
"""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core import generate
from scripts.core.models import TargetRef, TestScenario


_MODELS = '''\
from enum import Enum


class Status(Enum):
    NEW = "new"
    DONE = "done"
'''

_TARGET = '''\
from models import Status


def label(name: str, status: Status) -> str:
    return f"{name}:{status.value}"
'''


@pytest.fixture
def contract(tmp_path: Path):
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector="label"))


def _scn(*, assertion: str | None = None, expect_error: str | None = None) -> TestScenario:
    return TestScenario(id="s", title="t", unit="label", expected="x",
                        assertion=assertion, expect_error=expect_error,
                        inputs={"name": "A", "status": {"$enum": "Status.NEW"}})


# ── accepts legitimate assertions (regression guard) ──────────────────────────
@pytest.mark.parametrize("expr", [
    "result == 'A:new'",
    "len(result) == 5",
    "isinstance(result, str)",
    "result.startswith('A')",                       # method call on result is fine
    "all(c.isalpha() or c in ':' for c in result)", # comprehension-bound name `c`
    "result == Status.NEW.value",                   # resolved enum type
    "type(result) == str and result != ''",
])
def test_accepts_safe_assertions(contract, expr):
    adapter.validate_scenario(_scn(assertion=expr), contract)   # no raise


# ── rejects code smuggled into the assertion ──────────────────────────────────
@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo pwned') or True",
    "result or eval('1')",
    "result == open('/etc/passwd').read()",
    "result.__class__.__bases__[0].__subclasses__() == []",   # dunder traversal escape
    "os.getcwd() == result",                                  # unknown global name
    "getattr(result, '__class__')",
])
def test_rejects_unsafe_assertions(contract, expr):
    with pytest.raises(ValueError, match=r"assertion:"):
        adapter.validate_scenario(_scn(assertion=expr), contract)


def test_rejects_unparseable_assertion(contract):
    with pytest.raises(ValueError, match=r"parseable"):
        adapter.validate_scenario(_scn(assertion="result =="), contract)


# ── expect_error must name a known exception ──────────────────────────────────
@pytest.mark.parametrize("expr", ["ValueError", "KeyError", "(ValueError, TypeError)"])
def test_accepts_known_exceptions(contract, expr):
    adapter.validate_scenario(_scn(expect_error=expr), contract)   # no raise


@pytest.mark.parametrize("expr", [
    "__import__('os')",
    "os.system",
    "eval('ValueError')",
    "Nope",
])
def test_rejects_bad_expect_error(contract, expr):
    with pytest.raises(ValueError, match=r"expect_error"):
        adapter.validate_scenario(_scn(expect_error=expr), contract)


# ── a smuggled assertion routes through the repair-retry at generation time ────
def test_generation_repairs_unsafe_assertion(contract, monkeypatch):
    bad = ('[{"id":"s","title":"t","unit":"label","expected":"x",'
           '"assertion":"__import__(\'os\').system(\'x\') or True",'
           '"inputs":{"name":"A","status":{"$enum":"Status.NEW"}}}]')
    good = ('[{"id":"s","title":"t","unit":"label","expected":"x","assertion":"result == \'A:new\'",'
            '"inputs":{"name":"A","status":{"$enum":"Status.NEW"}}}]')
    replies = iter([bad, good])
    monkeypatch.setattr(generate, "llm_call", lambda *a, **k: next(replies))
    result = generate.generate_scenarios(contract, adapter=adapter, count=3, repair_retries=1)
    assert len(result.scenarios) == 1
    assert result.scenarios[0].assertion == "result == 'A:new'"
