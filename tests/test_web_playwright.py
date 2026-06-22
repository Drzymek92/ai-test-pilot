"""web_playwright adapter — emit/describe/typescript are browser-free (hand-built contract).
The live DOM introspection is exercised separately in the M4 live run, not in unit tests."""
from scripts.adapters import web_playwright as wp
from scripts.core.models import TargetContract, TargetRef, TestScenario, UnitSpec, WebAction


def _contract() -> TargetContract:
    unit = UnitSpec(
        name="signup", kind="route", docstring="Sign up",
        elements=[
            {"tag": "input", "selector": "#email", "text": "", "name": "email"},
            {"tag": "button", "selector": '[data-testid="submit"]', "text": "Sign up"},
            {"tag": "p", "selector": "#message", "text": ""},
        ],
    )
    return TargetContract(
        ref=TargetRef(adapter="web_playwright", locator="demo/signup.html", selector="signup"),
        module="file:///demo/signup.html", units=[unit])


def _scenario() -> TestScenario:
    return TestScenario(
        id="happy", title="valid signup", unit="signup", expected="welcome shown",
        actions=[
            WebAction(action="fill", selector="#email", value="a@b.com"),
            WebAction(action="click", selector='[data-testid="submit"]'),
            WebAction(action="expect_text", selector="#message", text="Account created"),
        ],
    )


def test_describe_lists_selectors():
    text = wp.describe_contract(_contract())
    assert "#email" in text and '[data-testid="submit"]' in text and "signup" in text


def test_emit_renders_self_contained_playwright():
    src = wp.emit(_scenario(), _contract())
    assert "def test_happy():" in src
    assert "sync_playwright()" in src and "_page.goto(BASE_URL)" in src
    assert "_page.fill('#email', 'a@b.com')" in src
    assert '_page.click(\'[data-testid="submit"]\')' in src
    assert "expect(_page.locator('#message')).to_contain_text('Account created')" in src
    assert "_b.close()" in src


def test_file_header_imports_playwright():
    h = wp.file_header(_contract(), [_scenario()])
    assert "from playwright.sync_api import expect, sync_playwright" in h
    assert "BASE_URL = 'file:///demo/signup.html'" in h


def test_typescript_export():
    ts = wp.typescript(_scenario(), _contract())
    assert "test('valid signup'" in ts
    assert "await page.fill('#email', 'a@b.com');" in ts
    assert "toContainText('Account created')" in ts


def test_registered_in_registry():
    from scripts.core import registry
    assert "web_playwright" in registry.available()
    assert registry.get_adapter("web_playwright").name == "web_playwright"
