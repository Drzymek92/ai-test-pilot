"""Deep-Playwright (served mode) emit — browser-free, hand-built contracts/scenarios.

Covers the task-A additions: network interception (route / expect_request), the storage_state
auth path, conftest emission, the async variant, and the TypeScript export of the new actions.
A live end-to-end run against demo/login_app exercises the real browser separately.
"""
import ast

from scripts.adapters import web_playwright as wp
from scripts.core.models import (
    ScenarioSet,
    TargetContract,
    TargetRef,
    TestScenario,
    UnitSpec,
    WebAction,
)


def _served_contract() -> TargetContract:
    unit = UnitSpec(
        name="login", kind="route", docstring="Log in",
        elements=[
            {"tag": "input", "selector": "#email", "text": "", "type": "email", "name": "email"},
            {"tag": "input", "selector": "#password", "text": "", "type": "password", "name": "password"},
            {"tag": "button", "selector": '[data-testid="submit"]', "text": "Log in"},
            {"tag": "div", "selector": '[data-testid="dashboard"]', "text": ""},
        ],
    )
    return TargetContract(
        ref=TargetRef(adapter="web_playwright", locator="demo/login_app/index.html", selector="login"),
        module="http://localhost:3000/index.html", units=[unit],
        serve_dir="demo/login_app", page_path="/index.html")


def _login_scenario() -> TestScenario:
    return TestScenario(
        id="login_success", title="successful login", unit="login", expected="dashboard shown",
        tags=["happy_path"],
        actions=[
            WebAction(action="route", url_pattern="**/api/login", status=200,
                      body='{"token": "t", "user": "a@b.com"}'),
            WebAction(action="fill", selector="#email", value="a@b.com"),
            WebAction(action="fill", selector="#password", value="secret123"),
            WebAction(action="click", selector='[data-testid="submit"]'),
            WebAction(action="expect_visible", selector='[data-testid="dashboard"]'),
            WebAction(action="expect_request", url_pattern="**/api/login"),
        ],
    )


def _auth_scenario() -> TestScenario:
    return TestScenario(
        id="already_authed", title="restored session skips login", unit="login",
        expected="dashboard shown without logging in", storage_state=True, tags=["regression"],
        actions=[WebAction(action="expect_visible", selector='[data-testid="dashboard"]')],
    )


def _is_valid_python(src: str) -> bool:
    ast.parse(src)
    return True


# ── route interception ───────────────────────────────────────────────────────
def test_route_rendered_before_goto():
    src = wp.emit(_login_scenario(), _served_contract())
    assert _is_valid_python(
        wp.file_header(_served_contract()) + "\n" + src)
    # route registered, and it precedes the goto
    assert "_page.route('**/api/login', lambda route: route.fulfill(" in src
    assert "status=200" in src and 'content_type="application/json"' in src
    assert src.index("_page.route(") < src.index("_page.goto(")


def test_expect_request_uses_listener():
    src = wp.emit(_login_scenario(), _served_contract())
    assert '_page.on("request"' in src
    assert "assert any('/api/login' in _u for _u in _requests)" in src


def test_served_test_takes_base_url_fixture():
    src = wp.emit(_login_scenario(), _served_contract())
    assert src.startswith("def test_login_success(base_url):")
    assert "_page.goto(base_url + '/index.html')" in src


def test_non_served_path_unchanged():
    """The M4 self-contained path must be byte-identical (no base_url, BASE_URL constant)."""
    c = TargetContract(
        ref=TargetRef(adapter="web_playwright", locator="demo/signup.html", selector="signup"),
        module="file:///demo/signup.html",
        units=[UnitSpec(name="signup", kind="route",
                        elements=[{"tag": "input", "selector": "#email"}])])
    s = TestScenario(id="x", title="t", unit="signup", expected="e",
                     actions=[WebAction(action="fill", selector="#email", value="a@b.com")])
    src = wp.emit(s, c)
    assert src.startswith("def test_x():")
    assert "_page.goto(BASE_URL)" in src and "base_url" not in src


# ── storage_state auth path ──────────────────────────────────────────────────
def test_storage_state_uses_new_context_and_fixture():
    src = wp.emit(_auth_scenario(), _served_contract())
    assert src.startswith("def test_already_authed(base_url, auth_state):")
    assert "_b.new_context(storage_state=auth_state)" in src
    assert "_ctx.new_page()" in src
    assert "_page.fill" not in src  # restored session never logs in


# ── conftest emission ────────────────────────────────────────────────────────
def test_extra_files_emits_valid_conftest():
    ss = ScenarioSet(target=_served_contract().ref,
                     scenarios=[_login_scenario(), _auth_scenario()])
    files = wp.extra_files(_served_contract(), ss)
    assert "conftest.py" in files
    conftest = files["conftest.py"]
    assert _is_valid_python(conftest)
    assert "def base_url():" in conftest
    assert "def auth_state(tmp_path_factory, base_url):" in conftest
    assert 'page.route("**/api/login", _stub_login)' in conftest
    assert "context.storage_state(path=str(state))" in conftest
    # login selectors derived from the contract elements
    assert "page.fill('#email', STUB_USER)" in conftest
    assert "page.fill('#password'" in conftest


def test_no_conftest_in_non_served_mode():
    c = TargetContract(ref=TargetRef(adapter="web_playwright", locator="demo/signup.html"),
                       module="file:///demo/signup.html",
                       units=[UnitSpec(name="signup", kind="route")])
    assert wp.extra_files(c, ScenarioSet(target=c.ref, scenarios=[])) == {}


# ── async variant ────────────────────────────────────────────────────────────
def test_async_variant_emits_asyncio_playwright():
    s = _login_scenario()
    s.tags.append("async")
    src = wp.emit(s, _served_contract())
    assert _is_valid_python(src)
    assert "from playwright.async_api import async_playwright, expect as aexpect" in src
    assert "async def _run():" in src
    assert "asyncio.run(_run())" in src
    assert "await _page.goto(base_url + '/index.html')" in src
    assert "await _page.fill('#email', 'a@b.com')" in src
    assert "await aexpect(_page.locator(" in src
    # async route handler must be awaitable, not a lambda
    assert "async def _route_0(route): await route.fulfill(" in src


def test_async_storage_state_combo():
    s = _auth_scenario()
    s.tags.append("async")
    src = wp.emit(s, _served_contract())
    assert _is_valid_python(src)
    assert "_ctx = await _b.new_context(storage_state=auth_state)" in src
    assert "_page = await _ctx.new_page()" in src


# ── TypeScript export ────────────────────────────────────────────────────────
def test_typescript_exports_new_actions():
    ts = wp.typescript(_login_scenario(), _served_contract())
    assert "await page.route('**/api/login'" in ts
    assert "route.fulfill(" in ts and "status: 200" in ts
    assert "await page.waitForRequest(" in ts


def test_typescript_notes_storage_state():
    ts = wp.typescript(_auth_scenario(), _served_contract())
    assert "storageState" in ts


# ── websocket (task B) ───────────────────────────────────────────────────────
def _ws_contract() -> TargetContract:
    unit = UnitSpec(
        name="live", kind="route", docstring="Live updates",
        elements=[
            {"tag": "button", "selector": '[data-testid="connect"]', "text": "Connect"},
            {"tag": "input", "selector": "#msg", "text": "", "name": "msg"},
            {"tag": "button", "selector": '[data-testid="send"]', "text": "Send"},
            {"tag": "p", "selector": "#status", "text": ""},
            {"tag": "ul", "selector": "#feed", "text": ""},
        ],
    )
    return TargetContract(
        ref=TargetRef(adapter="web_playwright", locator="demo/ws_app/index.html", selector="live"),
        module="http://localhost:3000/index.html", units=[unit],
        serve_dir="demo/ws_app", page_path="/index.html", page_features=["websocket"])


def _ws_scenario() -> TestScenario:
    return TestScenario(
        id="live_feed", title="receives push and echoes a message", unit="live",
        expected="pushed notification and echoed message appear in the feed", tags=["happy_path"],
        actions=[
            WebAction(action="route_ws", url_pattern="**/ws", body='{"text": "New update!"}'),
            WebAction(action="click", selector='[data-testid="connect"]'),
            WebAction(action="expect_text", selector="#status", text="connected"),
            WebAction(action="expect_text", selector="#feed", text="New update!"),
            WebAction(action="fill", selector="#msg", value="hello"),
            WebAction(action="click", selector='[data-testid="send"]'),
            WebAction(action="expect_text", selector="#feed", text="echo:hello"),
            WebAction(action="expect_ws_message", text="hello"),
        ],
    )


def test_route_ws_mocks_socket_before_goto():
    src = wp.emit(_ws_scenario(), _ws_contract())
    assert _is_valid_python(wp.file_header(_ws_contract()) + "\n" + src)
    assert "_ws_log = []" in src
    assert "def _ws_route_0(ws):" in src
    assert "ws.on_message(_on_msg_0)" in src
    assert 'ws.send("echo:" + message)' in src
    assert "_ws_log.append(message)" in src
    assert "ws.send('{\"text\": \"New update!\"}')" in src  # server push body
    assert "_page.route_web_socket('**/ws', _ws_route_0)" in src
    # the ws mock is registered before navigation
    assert src.index("route_web_socket") < src.index("_page.goto(")


def test_expect_ws_message_asserts_over_ws_log():
    src = wp.emit(_ws_scenario(), _ws_contract())
    assert "assert any('hello' in str(_m) for _m in _ws_log)" in src


def test_ws_conftest_has_no_auth_fixture():
    """A ws-only demo (no storage_state scenario) must NOT emit the login auth_state fixture."""
    ss = ScenarioSet(target=_ws_contract().ref, scenarios=[_ws_scenario()])
    conftest = wp.extra_files(_ws_contract(), ss)["conftest.py"]
    assert _is_valid_python(conftest)
    assert "def base_url():" in conftest
    assert "auth_state" not in conftest
    assert "import json" not in conftest  # only pulled in for the auth fixture


def test_async_ws_awaits_only_registration():
    s = _ws_scenario()
    s.tags.append("async")
    src = wp.emit(s, _ws_contract())
    assert _is_valid_python(src)
    assert "await _page.route_web_socket('**/ws', _ws_route_0)" in src
    # handler methods are synchronous in both APIs — must NOT be awaited
    assert "await ws.send" not in src and "await ws.on_message" not in src


def test_describe_contract_emits_ws_guidance():
    text = wp.describe_contract(_ws_contract())
    assert "WEBSOCKET" in text
    assert "route_ws" in text and "expect_ws_message" in text
    # a ws-only page should not get the API/login guidance
    assert "storage_state: true" not in text
