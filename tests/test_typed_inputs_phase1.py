"""Phase 1 — producer-set typed-input construction + allow-list kwarg enforcement.

When a type's own `__init__` is uninformative, the tool now builds it via an ALTERNATE constructor
discovered from source — a module-level factory (`open_account(...) -> Account`) or a `@classmethod`
factory (`Money.of(...) -> "Money"`) — instead of fabricating a constructor. The value-grammar
allow-list also now rejects a `$type` kwarg that is not a resolved constructor field (the gap that
let `Account(tier=, balance=)` through against a no-arg `__init__`). All deterministic/offline.
"""
from pathlib import Path

import pytest

from scripts.adapters import python_pytest as adapter
from scripts.core.models import ScenarioSet, TargetRef, TestScenario


_MODELS = '''\
class Account:
    """Factory-built: callers use open_account(); __init__ takes no args."""
    def __init__(self) -> None:
        self.balance = 0
        self.tier = "standard"


def open_account(balance: int, tier: str) -> Account:
    a = Account()
    a.balance = balance
    a.tier = tier
    return a


class Money:
    """Alternate constructor: no-arg __init__ (uninformative) → real construction via Money.of."""
    def __init__(self) -> None:
        self._cents = 0

    @classmethod
    def of(cls, dollars: int, cents: int) -> "Money":
        m = cls()
        m._cents = dollars * 100 + cents
        return m

    @property
    def cents(self) -> int:
        return self._cents


class Plain:
    """A real, informative __init__ → must stay initclass (no regression)."""
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class Bare:
    """No-arg __init__ and NO factory → stays initclass, built as Bare()."""
    def __init__(self) -> None:
        self.n = 0
'''

_TARGET = '''\
from models import Account, Money, Plain, Bare


def credit_limit(account: Account, multiplier: int) -> int:
    if account.tier == "gold":
        return account.balance * multiplier
    return account.balance


def cents_value(money: Money) -> int:
    return money.cents


def plain_sum(p: Plain) -> int:
    return p.x + p.y


def bare_n(b: Bare) -> int:
    return b.n
'''


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    (tmp_path / "models.py").write_text(_MODELS, encoding="utf-8")
    target = tmp_path / "calc.py"
    target.write_text(_TARGET, encoding="utf-8")
    return target


def _contract(target: Path, selector: str):
    return adapter.introspect(TargetRef(adapter="python_pytest", locator=str(target), selector=selector))


def test_module_factory_resolves_as_producer(proj):
    c = _contract(proj, "credit_limit")
    ts = c.types["Account"]
    assert ts.kind == "producer"
    assert ts.builder == "open_account" and ts.import_symbol == "open_account"
    assert [f.name for f in ts.fields] == ["balance", "tier"]
    assert c.units[0].complex_params == []                         # no longer warn-and-skip


def test_classmethod_factory_resolves_as_producer(proj):
    c = _contract(proj, "cents_value")
    ts = c.types["Money"]
    assert ts.kind == "producer"
    assert ts.builder == "Money.of"          # called as an attribute on the class
    assert ts.import_symbol == "Money"        # but the CLASS is what gets imported
    assert [f.name for f in ts.fields] == ["dollars", "cents"]


def test_producer_renders_to_factory_call(proj):
    c = _contract(proj, "credit_limit")
    rmap = adapter._render_map(c)
    out = adapter._render_value({"$type": "Account", "args": {"balance": 100, "tier": "gold"}}, rmap)
    assert out == "open_account(balance=100, tier='gold')"


def test_classmethod_producer_renders_as_attribute_call(proj):
    c = _contract(proj, "cents_value")
    rmap = adapter._render_map(c)
    out = adapter._render_value({"$type": "Money", "args": {"dollars": 3, "cents": 5}}, rmap)
    assert out == "Money.of(dollars=3, cents=5)"


def test_real_init_stays_initclass(proj):
    c = _contract(proj, "plain_sum")
    assert c.types["Plain"].kind == "initclass"     # informative __init__ → not diverted to a producer


def test_noarg_no_factory_stays_initclass(proj):
    c = _contract(proj, "bare_n")
    ts = c.types["Bare"]
    assert ts.kind == "initclass" and ts.fields == []
    assert adapter._render_value({"$type": "Bare", "args": {}}, adapter._render_map(c)) == "Bare()"


def test_file_header_imports_factory_and_class(proj):
    c = _contract(proj, "credit_limit")
    s = TestScenario(id="t", title="t", unit="credit_limit", expected="x",
                     inputs={"account": {"$type": "Account", "args": {"balance": 1, "tier": "gold"}},
                             "multiplier": 2})
    header = adapter.file_header(c, [s])
    assert "from calc import credit_limit" in header        # target fn lives in calc.py
    assert "from models import open_account" in header      # the factory is imported, not Account


def test_allowlist_rejects_fabricated_kwarg_on_noarg_class(proj):
    """The Part-B gap: a kwarg that is not a resolved constructor field must be rejected."""
    c = _contract(proj, "bare_n")
    bogus = TestScenario(id="t", title="t", unit="bare_n", expected="x",
                         inputs={"b": {"$type": "Bare", "args": {"fabricated": 1}}})
    with pytest.raises(ValueError, match="unknown argument"):
        adapter.validate_scenario(bogus, c)


def test_allowlist_accepts_real_producer_fields_rejects_bogus(proj):
    c = _contract(proj, "credit_limit")
    ok = TestScenario(id="t", title="t", unit="credit_limit", expected="x",
                      inputs={"account": {"$type": "Account", "args": {"balance": 10, "tier": "gold"}},
                              "multiplier": 3})
    adapter.validate_scenario(ok, c)                                # no raise
    bad = TestScenario(id="t", title="t", unit="credit_limit", expected="x",
                       inputs={"account": {"$type": "Account", "args": {"nonsense": 1}}, "multiplier": 3})
    with pytest.raises(ValueError, match="unknown argument"):
        adapter.validate_scenario(bad, c)


def test_producer_end_to_end_offline(proj, tmp_path):
    """Materialize + run a producer scenario with no LLM — proves the rendered factory call executes."""
    from scripts.core.materialize import materialize
    from scripts.core.runner import run_tests

    c = _contract(proj, "credit_limit")
    s = TestScenario(id="cl", title="gold credit", unit="credit_limit", expected="500",
                     assertion="result == 500",
                     inputs={"account": {"$type": "Account", "args": {"balance": 100, "tier": "gold"}},
                             "multiplier": 5})
    ss = ScenarioSet(target=c.ref, scenarios=[s])
    out = tmp_path / "test_cl_gen.py"
    materialize(adapter, c, ss, out)
    results = run_tests(adapter, out, ss, cwd=tmp_path)
    assert results[0].status == "passed", results[0].captured
