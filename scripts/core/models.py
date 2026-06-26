"""Pydantic data contracts — the schema spine.

Every stage consumes and produces typed objects, not free text. The LLM's output
is validated against TestScenario/ScenarioSet (reject + one repair retry on invalid
JSON). M1 implements the introspect→generate→materialize→run subset; TriageVerdict
and RunRecord arrive with the M2 ledger.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TargetRef(BaseModel):
    """What we're testing."""
    adapter: str                      # "python_pytest" | "web_playwright"
    locator: str                      # module path | base URL
    selector: str | None = None       # function name(s) | route, optional


class ParamSpec(BaseModel):
    """One parameter of a callable unit (python adapter)."""
    name: str
    annotation: str | None = None
    default: str | None = None        # repr of the default, or None if required
    kind: str = "positional_or_keyword"


class UnitSpec(BaseModel):
    """One testable unit — a function (python) or a route/element (web)."""
    name: str
    kind: Literal["function", "method", "route"] = "function"
    signature: str | None = None      # human-readable signature, e.g. "(text: str, max_chars: int = 1200)"
    params: list[ParamSpec] = Field(default_factory=list)
    returns: str | None = None        # return annotation, if any
    docstring: str | None = None
    raises: list[str] = Field(default_factory=list)   # exception types named in the docstring/body
    is_pure: bool = True              # heuristic: no obvious IO/side effects → safe to call directly
    reads_clock: bool = False         # body reads the wall clock/RNG → non-deterministic unless time is pinned
    complex_params: list[str] = Field(default_factory=list)   # params typed as non-constructible domain objects ("name: Type")
    source: str | None = None         # P3a: bounded slice of the unit's own source → specific-behaviour assertions (CUT context)
    elements: list[dict] = Field(default_factory=list)        # web route only: interactive DOM elements {tag, selector, text, attrs}


class FieldSpec(BaseModel):
    """One field of a constructible domain type."""
    name: str
    annotation: str | None = None
    has_default: bool = False         # omit from a constructor call to accept the default
    constraint: str | None = None     # P3b-2: value constraints to respect, e.g. "gt=0, le=100" (pydantic Field/con*)


class TypeSpec(BaseModel):
    """A project-defined type the tool can construct (dataclass / pydantic / enum).

    Recovered deterministically by ast-parsing the defining module (never imported).
    Lets the LLM describe how to BUILD a typed param instead of fabricating a dict.

    A3 (usage-guided construction) adds three more strategies so green≈0 targets become testable:
      - `initclass`: a plain class — construct the REAL object via its `__init__` signature.
      - `duck`:      an opaque/third-party type — a `types.SimpleNamespace` stand-in carrying just the
                     attributes the function body actually reads (inferred from in-body usage).
      - `builder`:   a user-provided builder (config) constructs it — the safe hatch for types the
                     above can't build (cyclic graphs, validator-heavy configs).
    All four render through the SAME `$type` value grammar + allow-list, so the model still only
    authors JSON. `module` is "" for `duck`/`builder` (synthesized / imported via `builder`).
    """
    name: str
    kind: Literal["dataclass", "pydantic", "enum", "attrs", "namedtuple", "initclass", "duck", "builder"]
    module: str                       # importable module the type lives in ("" for duck/builder)
    fields: list[FieldSpec] = Field(default_factory=list)   # dataclass/pydantic/initclass; duck=attrs
    enum_members: list[str] = Field(default_factory=list)   # enum only (member names)
    builder: str | None = None        # A3(c): "dotted.module:func" used to build a `builder` type
    usage_hint: str | None = None     # A3(b): observed construction from callers, e.g. "Node(value=, successors=)"


class TargetContract(BaseModel):
    """Deterministic introspection output handed to the LLM."""
    ref: TargetRef
    module: str | None = None         # importable module path the units live in (python adapter)
    units: list[UnitSpec]
    types: dict[str, TypeSpec] = Field(default_factory=dict)   # closure of constructible param types
    serve_dir: str | None = None      # web: dir to serve over localhost http (enables base_url/auth_state fixtures + storage_state)
    page_path: str | None = None      # web: served page path appended to base_url, e.g. "/index.html"
    page_features: list[str] = Field(default_factory=list)   # web: detected capabilities, e.g. ["api", "websocket"]


class TmpFile(BaseModel):
    """A real temp file created via pytest's `tmp_path`, whose path feeds an input param.

    Lets the LLM test file-processing functions with VALID inputs instead of fabricating
    paths that don't exist (the #1 bad-scenario source for IO functions).
    """
    param: str                        # the function parameter that receives this file's path
    filename: str                     # name incl. extension (dispatchers often switch on suffix)
    text: str = ""                    # file contents (text formats only: csv/json/txt/md/html)
    from_fixture: bool = False         # if true, `text` is filled from the data-factory fixture file


class WebAction(BaseModel):
    """One structured step in a web (Playwright) scenario — rendered deterministically to code."""
    action: Literal["click", "fill", "check", "select", "press",
                    "expect_text", "expect_visible", "expect_hidden", "expect_value", "expect_url",
                    "route", "expect_request", "route_ws", "expect_ws_message"]
    selector: str | None = None       # CSS / text= selector from the contract's elements
    value: str | None = None          # fill/select/press value, or expected value/url
    text: str | None = None           # expected text for expect_text / expect_ws_message
    # network interception (route) / assertion (expect_request) — the "advanced Playwright" path
    url_pattern: str | None = None    # glob matched against request/websocket URLs, e.g. "**/api/login"
    status: int | None = None         # route: HTTP status to fulfill the stubbed response with
    body: str | None = None           # route: HTTP response body; route_ws: message pushed to the client on connect


class TestScenario(BaseModel):
    """ONE proposed test — the LLM's structured output."""
    __test__ = False                  # not a pytest test class despite the "Test" prefix
    id: str
    title: str
    unit: str                         # which UnitSpec.name it targets
    preconditions: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)   # kwargs to call the unit with
    tmp_files: list[TmpFile] = Field(default_factory=list) # real temp inputs for file params
    actions: list[WebAction] = Field(default_factory=list) # web route only: ordered Playwright steps
    steps: list[str] = Field(default_factory=list)         # web: actions; python: call sequence
    expected: str                     # asserted outcome, in words
    assertion: str | None = None      # concrete boolean expression over `result`, e.g. "result == []"
    expect_error: str | None = None   # exception type expected instead of a return, e.g. "ValueError"
    rationale: str = ""               # why this case matters (audit trail)
    tags: list[str] = Field(default_factory=list)          # happy_path | edge | error | regression | async
    fixture: str | None = None        # name of a data-factory fixture feeding `inputs`, if any
    storage_state: bool = False       # web: start from a logged-in context (auth_state fixture) instead of logging in


class ScenarioSet(BaseModel):
    target: TargetRef
    scenarios: list[TestScenario]
    model: str = ""
    prompt_version: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


class RunResult(BaseModel):
    scenario_id: str
    status: Literal["passed", "failed", "error"]
    signal: str = ""                  # assertion | import_error | timeout | exception_type ...
    captured: str = ""                # truncated stdout/stderr


class TriageVerdict(BaseModel):
    """Stage 5 — why a failure failed (ARCHITECTURE §4)."""
    scenario_id: str
    verdict: Literal["real_bug", "bad_scenario", "flaky", "env_issue"]
    confidence: float
    evidence: str = ""                # the signal / reasoning behind the verdict
    suggested_fix: str | None = None
    source: Literal["deterministic", "llm"] = "deterministic"


class RunRequest(BaseModel):
    """Typed INPUT contract for `run_pipeline`.

    Replaces the raw `argparse.Namespace` the pipeline used to consume. There is exactly ONE place each
    field is named (here), so the five callers that drive the pipeline — the CLI, `sweep`, `quality`,
    `detection`, and the MCP server — build a `RunRequest` instead of each hand-rolling a namespace.
    That eliminated a real bug class: a namespace missing a field added later (e.g. `feedback`)
    crashed `run_pipeline` at access time; defaults here make every field always present.
    """
    model_config = ConfigDict(extra="forbid")
    target: str | None = None
    adapter: str | None = None
    selector: str | None = None
    count: int | None = None
    model: str | None = None
    prompt_version: str | None = None
    no_run: bool = False
    no_cache: bool = False             # P1 reproducibility
    refresh_cache: bool = False
    fixtures: bool = False             # data-factory fixtures
    fixture_domain: str | None = None
    fixture_entity: str | None = None
    fixture_rows: int | None = None
    context: str | None = None         # project domain-context
    no_context: bool = False
    no_cut_source: bool = False
    golden: bool = False
    feedback: bool = False             # A1 coverage-feedback loop
    no_feedback: bool = False
    serve: bool = False                # web adapter (served mode)
    web_async: bool = False

    @classmethod
    def from_namespace(cls, ns: Any) -> "RunRequest":
        """Build from an argparse.Namespace (the CLI). Missing attrs fall back to the field defaults."""
        return cls(**{k: getattr(ns, k) for k in cls.model_fields if hasattr(ns, k)})


class RunReport(BaseModel):
    """M1 run summary (the M2 ledger persists a richer RunRecord)."""
    run_id: str
    ts: datetime
    adapter: str
    target: str
    model: str
    prompt_version: str
    generated: int
    passed: int
    failed: int
    errored: int
    test_file: str | None = None
    scenarios_file: str | None = None
    report_file: str | None = None
    fixture_file: str | None = None
    context_file: str | None = None
    caveats: list[str] = Field(default_factory=list)   # tool-support warnings for this target
    tokens_in: int = 0                 # P4: real generation spend this run (0 on a cache replay)
    tokens_out: int = 0
    cost_est: float = 0.0
    feedback_rounds: int = 0           # Approach 1: coverage-feedback regeneration rounds run
    feedback_added: int = 0            # Approach 1: extra scenarios added by the feedback loop


class RunRecord(BaseModel):
    """One ledger row — the self-tracking unit (ARCHITECTURE §4/§6)."""
    run_id: str
    ts: datetime
    adapter: str
    target: str
    model: str
    prompt_version: str
    generated: int
    passed: int
    failed: int
    triage: dict[str, int] = Field(default_factory=dict)   # {"real_bug": 1, "bad_scenario": 2, ...}
    accepted: int | None = None        # filled when the human reviews (kept tests)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_est: float = 0.0
    acceptance_rate: float | None = None
