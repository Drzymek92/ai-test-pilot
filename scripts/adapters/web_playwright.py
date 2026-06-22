"""`web` → Playwright adapter (M4 + deep-Playwright extension).

introspect: load the page headless (Playwright/Chromium — bundled Node driver, no system Node) and
scan the DOM for interactive elements + stable selectors.
emit: render a SELF-CONTAINED Python-Playwright pytest function from a scenario's structured
`actions` (no pytest-playwright plugin needed — each test launches its own headless browser, so it
runs under the same runner as the python adapter).
typescript: also export an idiomatic Playwright **.spec.ts** as a portfolio artifact.

Two emit modes, parallel and additive (the no-duplication seam):
- **Self-contained (default):** the M4 path — `file://`/URL target, a `BASE_URL` constant, one
  `new_page()` per test. Unchanged.
- **Served / deep (`contract.serve_dir` set):** the app is served over an ephemeral localhost http
  server (a real origin so `localStorage`/`storage_state` work). Tests take a `base_url` fixture and
  exercise advanced Playwright: `page.route(...)` network interception, an `auth_state` fixture that
  logs in once and reuses `storage_state`, and an optional `async_playwright` variant. The generated
  `conftest.py` carrying those fixtures is emitted via `extra_files`.

The portfolio half of the tool; same shared core, one adapter file, zero core edits beyond the
generic `elements`/`actions`/`serve_dir` fields on the contract.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.core.models import (
    ScenarioSet,
    TargetContract,
    TargetRef,
    TestScenario,
    UnitSpec,
    WebAction,
)

name = "web_playwright"
prompt_kind = "web"


# ── url / selector helpers ───────────────────────────────────────────────────
def _to_url(locator: str) -> str:
    if locator.startswith(("http://", "https://", "file://")):
        return locator
    p = Path(locator)
    return p.resolve().as_uri() if p.exists() else locator


def _slug(value: str) -> str:
    s = re.sub(r"\W+", "_", str(value)).strip("_")
    return s or "scenario"


_SCAN_JS = """() => {
  const sel = 'button, a[href], input, textarea, select, [data-testid], [role=button]';
  return Array.from(document.querySelectorAll(sel)).slice(0, 40).map(el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    name: el.getAttribute('name'),
    id: el.id || null,
    testid: el.getAttribute('data-testid'),
    placeholder: el.getAttribute('placeholder'),
    text: (el.innerText || el.value || '').trim().slice(0, 40),
  }));
}"""


def _selector(e: dict) -> str:
    if e.get("id"):
        return f"#{e['id']}"
    if e.get("testid"):
        return f"[data-testid=\"{e['testid']}\"]"
    if e.get("name"):
        return f"[name=\"{e['name']}\"]"
    if e["tag"] in ("button", "a") and e.get("text"):
        return f"{e['tag']}:has-text(\"{e['text']}\")"
    return e["tag"]


# ── adapter surface ──────────────────────────────────────────────────────────
def introspect(ref: TargetRef) -> TargetContract:
    from playwright.sync_api import sync_playwright

    url = _to_url(ref.locator)
    elements: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            title = page.title()
            for e in page.evaluate(_SCAN_JS):
                elements.append({
                    "tag": e["tag"], "selector": _selector(e),
                    "text": e.get("text") or "", "type": e.get("type") or "",
                    "name": e.get("name") or "", "placeholder": e.get("placeholder") or "",
                })
        finally:
            browser.close()

    unit = UnitSpec(name=ref.selector or "page", kind="route",
                    signature=f"({url})", docstring=title, elements=elements)
    return TargetContract(ref=ref, module=url, units=[unit])


def _serving(contract: TargetContract) -> bool:
    return bool(contract.serve_dir)


def describe_contract(contract: TargetContract) -> str:
    u = contract.units[0]
    lines = [f"Web page: {contract.module}", f"Title: {u.docstring or '(none)'}",
             f"Target route name (use as scenario.unit): {u.name}", "",
             "Interactive elements — use these EXACT selectors:"]
    for e in u.elements:
        hint = e.get("text") or e.get("placeholder") or e.get("name") or e.get("type") or ""
        lines.append(f"- {e['tag']} `{e['selector']}`" + (f"  — {hint}" if hint else ""))
    if not u.elements:
        lines.append("- (no interactive elements detected)")
    if _serving(contract):
        feats = contract.page_features
        if "api" in feats or not feats:
            lines += [
                "",
                "This page talks to a BACKEND API (it is served over http for the tests). You SHOULD:",
                "- intercept network calls with a `route` action (stub status + JSON body) to drive "
                "success vs. failure deterministically — never rely on a real server response;",
                "- optionally assert a request was made with `expect_request` (place it AFTER a UI "
                "assertion so the request has fired);",
                "- propose ONE scenario with `storage_state: true` that starts already logged-in "
                "(it reuses a saved auth session and skips the login form);",
                "- optionally tag ONE scenario `async` to emit the asyncio Playwright variant.",
            ]
        if "websocket" in feats:
            lines += [
                "",
                "This page uses a WEBSOCKET (event-driven). The socket server is MOCKED in-test. You SHOULD:",
                "- place a `route_ws` action FIRST (before the connect action). Its `body` is a message "
                "the mock PUSHES to the client on connect (event-driven); it also echoes `echo:<msg>` "
                "for anything the client sends. Use `url_pattern` like \"**/ws\".",
                "- assert the pushed/echoed messages show in the UI feed with `expect_text`;",
                "- optionally assert a message crossed the socket with `expect_ws_message` (text = a "
                "substring of what the CLIENT sent), placed AFTER the UI assertion.",
            ]
    return "\n".join(lines)


def file_header(contract: TargetContract, scenarios=()) -> str:
    head = (
        '"""Generated by AI Test Pilot (web_playwright) — REVIEW before promoting.\n'
        'Self-contained Python-Playwright tests; each launches its own headless Chromium.\n'
        '"""\n'
        "import re\n\n"
        "from playwright.sync_api import expect, sync_playwright\n\n"
    )
    if not _serving(contract):
        head += f"BASE_URL = {contract.module!r}\n"
    return head


# ── action rendering ─────────────────────────────────────────────────────────
def _goto_target(contract: TargetContract) -> str:
    if _serving(contract):
        return f"base_url + {contract.page_path or '/index.html'!r}"
    return "BASE_URL"


def _fixture_params(scenario: TestScenario, contract: TargetContract) -> list[str]:
    params: list[str] = []
    if _serving(contract):
        params.append("base_url")
        if scenario.storage_state:
            params.append("auth_state")
    return params


def _render_action(a: WebAction, is_async: bool = False) -> str:
    aw = "await " if is_async else ""
    exp = "aexpect" if is_async else "expect"
    s = a.selector
    if a.action == "click":
        return f"{aw}_page.click({s!r})"
    if a.action == "fill":
        return f"{aw}_page.fill({s!r}, {a.value!r})"
    if a.action == "check":
        return f"{aw}_page.check({s!r})"
    if a.action == "select":
        return f"{aw}_page.select_option({s!r}, {a.value!r})"
    if a.action == "press":
        return f"{aw}_page.press({s!r}, {a.value!r})"
    if a.action == "expect_text":
        return f"{aw}{exp}(_page.locator({s!r})).to_contain_text({a.text!r})"
    if a.action == "expect_visible":
        return f"{aw}{exp}(_page.locator({s!r})).to_be_visible()"
    if a.action == "expect_hidden":
        return f"{aw}{exp}(_page.locator({s!r})).to_be_hidden()"
    if a.action == "expect_value":
        return f"{aw}{exp}(_page.locator({s!r})).to_have_value({a.value!r})"
    if a.action == "expect_url":
        return f"assert {a.value!r} in _page.url"
    if a.action == "expect_request":
        needle = (a.url_pattern or a.value or "").replace("*", "")
        return f"assert any({needle!r} in _u for _u in _requests)"
    if a.action == "expect_ws_message":
        needle = a.text or a.value or ""
        return f"assert any({needle!r} in str(_m) for _m in _ws_log)"
    return f"pass  # unknown action {a.action!r}"


def _route_args(a: WebAction) -> str:
    status = a.status if a.status is not None else 200
    body = a.body if a.body is not None else ""
    return f'status={status}, content_type="application/json", body={body!r}'


def _render_route_lines(a: WebAction, i: int, is_async: bool, indent: str) -> list[str]:
    pat = a.url_pattern or a.value or "**/*"
    if is_async:
        return [
            f"{indent}async def _route_{i}(route): await route.fulfill({_route_args(a)})",
            f"{indent}await _page.route({pat!r}, _route_{i})",
        ]
    return [f"{indent}_page.route({pat!r}, lambda route: route.fulfill({_route_args(a)}))"]


def _render_ws_route_lines(a: WebAction, i: int, is_async: bool, indent: str) -> list[str]:
    """Mock a WebSocket server in-test (page.route_web_socket). Echoes client messages
    (recording them in _ws_log) and, if `body` is set, pushes that message on connect.
    The handler methods (send/on_message) are synchronous in both APIs; only the
    route_web_socket REGISTRATION is awaited in the async path."""
    pat = a.url_pattern or a.value or "**/ws"
    aw = "await " if is_async else ""
    lines = [
        f"{indent}def _ws_route_{i}(ws):",
        f"{indent}    def _on_msg_{i}(message):",
        f"{indent}        _ws_log.append(message)",
        f'{indent}        ws.send("echo:" + message)',
        f"{indent}    ws.on_message(_on_msg_{i})",
    ]
    if a.body is not None:
        lines.append(f"{indent}    ws.send({a.body!r})")
    lines.append(f"{indent}{aw}_page.route_web_socket({pat!r}, _ws_route_{i})")
    return lines


def _docstring_lines(scenario: TestScenario) -> list[str]:
    return [f'    """{scenario.title}', "",
            f"    Why: {scenario.rationale or '-'}",
            f"    Expected: {scenario.expected}", '    """']


def emit(scenario: TestScenario, contract: TargetContract) -> str:
    is_async = "async" in scenario.tags
    params = _fixture_params(scenario, contract)
    # `route` (HTTP) and `route_ws` (WebSocket) are interception setup — register them before goto
    setup = [a for a in scenario.actions if a.action in ("route", "route_ws")]
    rest = [a for a in scenario.actions if a.action not in ("route", "route_ws")]
    need_reqlog = any(a.action == "expect_request" for a in scenario.actions)
    need_wslog = any(a.action in ("route_ws", "expect_ws_message") for a in scenario.actions)

    lines = [f"def {test_function_name(scenario)}({', '.join(params)}):"]
    lines += _docstring_lines(scenario)

    if is_async:
        lines += ["    import asyncio",
                  "    from playwright.async_api import async_playwright, expect as aexpect",
                  "    async def _run():",
                  "        async with async_playwright() as _p:",
                  "            _b = await _p.chromium.launch()"]
        ctx_i, body_i, aw = "            ", "                ", "await "
    else:
        lines += ["    with sync_playwright() as _p:",
                  "        _b = _p.chromium.launch()"]
        ctx_i, body_i, aw = "        ", "            ", ""

    if scenario.storage_state and _serving(contract):
        lines += [f"{ctx_i}_ctx = {aw}_b.new_context(storage_state=auth_state)",
                  f"{ctx_i}_page = {aw}_ctx.new_page()"]
    else:
        lines.append(f"{ctx_i}_page = {aw}_b.new_page()")
    lines.append(f"{ctx_i}try:")

    if need_reqlog:
        lines += [f"{body_i}_requests = []",
                  f'{body_i}_page.on("request", lambda r: _requests.append(r.url))']
    if need_wslog:
        lines.append(f"{body_i}_ws_log = []")
    for i, a in enumerate(setup):
        if a.action == "route_ws":
            lines += _render_ws_route_lines(a, i, is_async, body_i)
        else:
            lines += _render_route_lines(a, i, is_async, body_i)

    lines.append(f"{body_i}{aw}_page.goto({_goto_target(contract)})")

    if rest:
        lines += [f"{body_i}{_render_action(a, is_async)}" for a in rest]
    elif not scenario.actions:
        lines.append(f'{body_i}import pytest; pytest.skip("scenario had no actions")')

    lines += [f"{ctx_i}finally:", f"{body_i}{aw}_b.close()"]
    if is_async:
        lines.append("    asyncio.run(_run())")
    lines.append("")
    return "\n".join(lines)


# ── conftest emission (served mode) ──────────────────────────────────────────
def _login_selectors(contract: TargetContract) -> tuple[str, str, str]:
    """Best-effort email/password/submit selectors for the login-once auth fixture."""
    elements = contract.units[0].elements if contract.units else []
    email = pwd = submit = None
    for e in elements:
        t = (e.get("type") or "").lower()
        nm = (e.get("name") or "").lower()
        sel = e.get("selector")
        if (t == "email" or "email" in nm) and not email:
            email = sel
        elif (t == "password" or "pass" in nm) and not pwd:
            pwd = sel
        if e.get("tag") == "button" and not submit:
            submit = sel
    return email or "#email", pwd or "#password", submit or "#submit"


_CONFTEST_BASE = '''"""Auto-generated by AI Test Pilot (web_playwright, served mode) — REVIEW before promoting.

Fixtures shared by the generated web tests:
- base_url:   serves the demo app over an ephemeral localhost http server. A REAL origin is
              required for localStorage / storage_state to round-trip (file:// drops them).{auth_doc}
"""
import functools
import http.server
import socketserver
import threading
{auth_imports}
import pytest
{auth_pw_import}
DEMO_DIR = {demo_dir!r}
PAGE_PATH = {page_path!r}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="session")
def base_url():
    handler = functools.partial(_QuietHandler, directory=DEMO_DIR)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{{port}}"
    finally:
        httpd.shutdown()
'''

_CONFTEST_AUTH = '''

STUB_USER = "alice@example.com"
STUB_TOKEN = "fixture-token-123"


def _stub_login(route):
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({{"token": STUB_TOKEN, "user": STUB_USER}}))


@pytest.fixture(scope="session")
def auth_state(tmp_path_factory, base_url):
    state = tmp_path_factory.mktemp("auth") / "state.json"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.route("**/api/login", _stub_login)
        page.goto(base_url + PAGE_PATH)
        page.fill({email_sel!r}, STUB_USER)
        page.fill({pass_sel!r}, "correct horse battery staple")
        page.click({submit_sel!r})
        page.wait_for_function("() => window.localStorage.getItem('token') !== null")
        context.storage_state(path=str(state))
        browser.close()
    return str(state)
'''


def _conftest_source(contract: TargetContract, scenario_set: ScenarioSet) -> str:
    need_auth = any(s.storage_state for s in scenario_set.scenarios)
    src = _CONFTEST_BASE.format(
        demo_dir=str(Path(contract.serve_dir).resolve()),
        page_path=contract.page_path or "/index.html",
        auth_doc=("\n- auth_state: logs in ONCE (stubbing /api/login) and saves storage_state to a "
                  "temp JSON,\n              so authenticated tests skip the login flow." if need_auth else ""),
        auth_imports="import json\n" if need_auth else "",
        auth_pw_import="from playwright.sync_api import sync_playwright\n" if need_auth else "",
    )
    if need_auth:
        email_sel, pass_sel, submit_sel = _login_selectors(contract)
        src += _CONFTEST_AUTH.format(email_sel=email_sel, pass_sel=pass_sel, submit_sel=submit_sel)
    return src


def extra_files(contract: TargetContract, scenario_set: ScenarioSet) -> dict[str, str]:
    """Extra files to write next to the test file (served mode → a conftest with the fixtures)."""
    if not _serving(contract):
        return {}
    return {"conftest.py": _conftest_source(contract, scenario_set)}


# ── TypeScript export (published artifact, not run here) ──────────────────────
def typescript(scenario: TestScenario, contract: TargetContract) -> str:
    """Idiomatic Playwright TypeScript for the same scenario (published artifact, not run here)."""
    ts_map = {
        "click": lambda a: f"await page.click('{a.selector}');",
        "fill": lambda a: f"await page.fill('{a.selector}', '{a.value}');",
        "check": lambda a: f"await page.check('{a.selector}');",
        "select": lambda a: f"await page.selectOption('{a.selector}', '{a.value}');",
        "press": lambda a: f"await page.press('{a.selector}', '{a.value}');",
        "route": lambda a: (
            f"await page.route('{a.url_pattern or a.value or '**/*'}', route => route.fulfill("
            f"{{ status: {a.status if a.status is not None else 200}, "
            f"contentType: 'application/json', body: {(a.body or '')!r} }}));"),
        "expect_request": lambda a: (
            f"await page.waitForRequest(/{re.escape((a.url_pattern or a.value or '').replace('*',''))}/);"),
        "route_ws": lambda a: (
            f"await page.routeWebSocket('{a.url_pattern or a.value or '**/ws'}', ws => {{\n"
            f"    ws.onMessage(message => ws.send('echo:' + message));"
            + (f"\n    ws.send({(a.body or '')!r});" if a.body is not None else "")
            + "\n  });"),
        "expect_ws_message": lambda a: (
            f"  // asserted in-app: a websocket message containing {(a.text or a.value or '')!r}"),
        "expect_text": lambda a: f"await expect(page.locator('{a.selector}')).toContainText('{a.text}');",
        "expect_visible": lambda a: f"await expect(page.locator('{a.selector}')).toBeVisible();",
        "expect_hidden": lambda a: f"await expect(page.locator('{a.selector}')).toBeHidden();",
        "expect_value": lambda a: f"await expect(page.locator('{a.selector}')).toHaveValue('{a.value}');",
        "expect_url": lambda a: f"await expect(page).toHaveURL(/{re.escape(a.value or '')}/);",
    }
    steps = "\n  ".join(ts_map.get(a.action, lambda a: "")(a) for a in scenario.actions)
    note = "  // uses a stored auth session (storageState)\n" if scenario.storage_state else ""
    return (f"test('{scenario.title}', async ({{ page }}) => {{\n"
            f"{note}  await page.goto(BASE_URL);\n  {steps}\n}});\n")


def test_function_name(scenario: TestScenario) -> str:
    return f"test_{_slug(scenario.id)}"


def runner_cmd(test_path: Path) -> list[str]:
    return ["pytest", "-q", "--no-header", str(test_path)]
