"""`python` → pytest adapter.

introspect: parse the target module with `ast` (NEVER import it — the target may pull
heavy/optional deps like fitz/docx; ast is deterministic and side-effect-free).
emit: render a pytest function from a validated TestScenario via a Jinja2 template.
runner_cmd: plain `pytest`.

The build-first, no-Node half of the tool.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from scripts.core.models import (
    FieldSpec,
    ParamSpec,
    TargetContract,
    TargetRef,
    TestScenario,
    TypeSpec,
    UnitSpec,
)

name = "python_pytest"

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompts" / "templates"

# Annotation identifiers the tool can construct from JSON primitives (or that pytest can
# supply). Anything else (a domain dataclass/pydantic model) is a "complex" param the tool
# cannot yet build reliably — surfaced so the LLM doesn't fabricate it as a bare dict.
_CONSTRUCTIBLE = {
    "str", "int", "float", "bool", "bytes", "complex", "None", "NoneType", "object",
    "list", "dict", "tuple", "set", "frozenset", "bytearray",
    "Any", "Optional", "Union", "List", "Dict", "Tuple", "Set", "FrozenSet",
    "Sequence", "Iterable", "Mapping", "Collection", "Literal",
    "Path", "PurePath", "datetime", "date", "time", "timedelta", "Decimal", "UUID",
}

# Scalar constructors the value grammar may use via {"$call": ...} → their stdlib module.
_SCALAR_IMPORTS = {
    "Decimal": "decimal",
    "datetime": "datetime", "date": "datetime", "time": "datetime", "timedelta": "datetime",
    "UUID": "uuid",
}

# Names whose appearance in a function body suggests IO / side effects → not "pure".
_IMPURE_HINTS = (
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "write",
    "save", "load_workbook", "connect", "request", "Document", "fitz", "docx",
    "print", "input", "os.", "subprocess", "requests", "urllib",
)


# ── import resolution ────────────────────────────────────────────────────────
def resolve_import(path: Path) -> tuple[Path, str]:
    """Return (sys_path_root, dotted_module) for a .py file.

    Walks up the package chain (dirs with __init__.py) to build the dotted module
    name, and returns the directory that must be on sys.path to import it. Falls
    back to (file's parent, filename stem) for a loose script.
    """
    path = path.resolve()
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    return parent, ".".join(reversed(parts))


# ── ast helpers ──────────────────────────────────────────────────────────────
def _annotation_str(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _render_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, list[ParamSpec]]:
    a = fn.args
    specs: list[ParamSpec] = []
    # defaults align to the tail of posonly+args
    posargs = list(a.posonlyargs) + list(a.args)
    n_defaults = len(a.defaults)
    default_for = {len(posargs) - n_defaults + i: d for i, d in enumerate(a.defaults)}
    for i, arg in enumerate(posargs):
        default = default_for.get(i)
        specs.append(ParamSpec(
            name=arg.arg,
            annotation=_annotation_str(arg.annotation),
            default=_annotation_str(default),
            kind="positional_or_keyword",
        ))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        specs.append(ParamSpec(
            name=arg.arg,
            annotation=_annotation_str(arg.annotation),
            default=_annotation_str(d) if d is not None else None,
            kind="keyword_only",
        ))
    rendered = ", ".join(
        s.name
        + (f": {s.annotation}" if s.annotation else "")
        + (f" = {s.default}" if s.default is not None else "")
        for s in specs
    )
    return f"({rendered})", specs


def _raises(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    out: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            target = exc.func if isinstance(exc, ast.Call) else exc
            label = _annotation_str(target)
            if label and label not in out:
                out.append(label)
    return out


def _is_pure(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body_src = ast.unparse(fn)
    return not any(hint in body_src for hint in _IMPURE_HINTS)


# Tokens whose presence means the function reads the wall clock / RNG → its result is not
# reproducible unless the relevant time is pinned via a parameter.
_CLOCK_HINTS = ("now(", "today(", "utcnow(", "time.time(", "monotonic(", "perf_counter(",
                "random", "uuid4(", "uuid1(", "gmtime(", "localtime(")


def _reads_clock(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(tok in ast.unparse(fn) for tok in _CLOCK_HINTS)


def _constructible(annotation: str) -> bool:
    """True if the tool can build a value for this annotation from JSON primitives."""
    ids = set(re.findall(r"[A-Za-z_]\w*", annotation))
    return ids <= _CONSTRUCTIBLE


def _type_identifiers(annotation: str) -> list[str]:
    """Candidate type names in an annotation (e.g. 'list[LineItemView]' → LineItemView)."""
    return [i for i in re.findall(r"[A-Za-z_]\w*", annotation) if i not in _CONSTRUCTIBLE]


def _unresolved_idents(annotation: str, known: dict) -> bool:
    """True if the annotation names a non-primitive type the tool could NOT resolve."""
    return any(i not in known for i in _type_identifiers(annotation))


def _unit_from_fn(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> UnitSpec:
    sig, params = _render_signature(fn)
    return UnitSpec(
        name=fn.name,
        kind="function",
        signature=sig,
        params=params,
        returns=_annotation_str(fn.returns),
        docstring=ast.get_docstring(fn),
        raises=_raises(fn),
        is_pure=_is_pure(fn),
        reads_clock=_reads_clock(fn),
        complex_params=[],            # set in introspect() after type resolution
    )


# ── typed-input construction: resolve a param's type from source (ast only) ──
@functools.lru_cache(maxsize=128)
def _parse_module(path_str: str) -> ast.Module:
    return ast.parse(Path(path_str).read_text(encoding="utf-8"), filename=path_str)


def _import_map(module_path: Path) -> dict[str, str]:
    """name → dotted module, from this module's `from X import a` / `import X` statements."""
    out: dict[str, str] = {}
    for node in _parse_module(str(module_path)).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name
    return out


def _module_to_path(dotted: str, root: Path) -> Path | None:
    base = root.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _find_classdef(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _name_of(node: ast.expr) -> str:
    """Last identifier of a Name/Attribute/Subscript/Call node ('a.b.C' → 'C')."""
    label = _annotation_str(node) or ""
    ids = re.findall(r"[A-Za-z_]\w*", label)
    return ids[-1] if ids else ""


def _classify(cls: ast.ClassDef) -> str | None:
    base_ids = {_name_of(b) for b in cls.bases}
    if base_ids & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
        return "enum"
    if any(b.endswith("BaseModel") or b == "BaseModel" for b in base_ids):
        return "pydantic"
    deco_ids = {_name_of(d.func if isinstance(d, ast.Call) else d) for d in cls.decorator_list}
    if "dataclass" in deco_ids:
        return "dataclass"
    return None


def _field_has_default(value: ast.expr | None) -> bool:
    if value is None:
        return False
    if isinstance(value, ast.Call) and _name_of(value.func) == "Field":
        if any(kw.arg in ("default", "default_factory") for kw in value.keywords):
            return True
        # Field(...) with Ellipsis (or no positional) means required.
        return bool(value.args) and not (
            isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis
        )
    return True


def _extract_fields(cls: ast.ClassDef) -> list[FieldSpec]:
    fields: list[FieldSpec] = []
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            n = node.target.id
            if n.startswith("_") or n == "model_config":
                continue
            ann = _annotation_str(node.annotation) or ""
            if ann.startswith("ClassVar"):
                continue
            fields.append(FieldSpec(name=n, annotation=ann or None,
                                    has_default=_field_has_default(node.value)))
    return fields


def _enum_members(cls: ast.ClassDef) -> list[str]:
    out: list[str] = []
    for node in cls.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and not t.id.startswith("_") and t.id != "model_config":
                out.append(t.id)
    return out


def resolve_type(name: str, project_root: Path, importing_module: Path,
                 *, types: dict[str, TypeSpec], seen: set[str], depth: int = 0) -> None:
    """Resolve `name` (used in `importing_module`) into a TypeSpec, recursing into fields.

    ast-only (never imports the target's project). Adds to `types`; leaves a name OUT of
    `types` when it can't be resolved (third-party / attrs / dynamic) so it stays 'complex'.
    """
    if name in types or name in seen or depth > 5:
        return
    seen.add(name)
    cls = _find_classdef(_parse_module(str(importing_module)), name)   # defined locally?
    if cls is not None:
        def_path, def_module = importing_module, resolve_import(importing_module)[1]
    else:
        dotted = _import_map(importing_module).get(name)
        if not dotted:
            return
        def_path = _module_to_path(dotted, project_root)
        if def_path is None:                       # third-party / unresolvable in this project
            return
        cls = _find_classdef(_parse_module(str(def_path)), name)
        if cls is None:
            return
        def_module = dotted

    kind = _classify(cls)
    if kind is None:
        return
    if kind == "enum":
        types[name] = TypeSpec(name=name, kind="enum", module=def_module,
                               enum_members=_enum_members(cls))
        return
    fields = _extract_fields(cls)
    types[name] = TypeSpec(name=name, kind=kind, module=def_module, fields=fields)
    for f in fields:                               # recurse into nested project types
        if f.annotation:
            for ident in _type_identifiers(f.annotation):
                resolve_type(ident, project_root, def_path, types=types, seen=seen, depth=depth + 1)


# ── adapter surface ──────────────────────────────────────────────────────────
def introspect(ref: TargetRef) -> TargetContract:
    path = Path(ref.locator)
    if not path.is_file():
        raise FileNotFoundError(f"Target module not found: {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {s.strip() for s in ref.selector.split(",")} if ref.selector else None

    units: list[UnitSpec] = []
    for node in tree.body:                       # top-level functions only (M1)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if wanted is not None and node.name not in wanted:
                continue
            units.append(_unit_from_fn(node))

    if not units:
        scope = f" matching {sorted(wanted)}" if wanted else ""
        raise ValueError(f"No top-level functions{scope} found in {path}")

    root, module = resolve_import(path)

    # Resolve the closure of constructible types referenced by any unit's params.
    types: dict[str, TypeSpec] = {}
    seen: set[str] = set()
    for u in units:
        for p in u.params:
            if p.annotation:
                for ident in _type_identifiers(p.annotation):
                    resolve_type(ident, root, path, types=types, seen=seen)

    # A param is "complex/unsupported" ONLY if it names a type we could not resolve.
    for u in units:
        u.complex_params = [f"{p.name}: {p.annotation}" for p in u.params
                            if p.annotation and _unresolved_idents(p.annotation, types)]

    return TargetContract(ref=ref, module=module, units=units, types=types)


def _slug(value: str) -> str:
    """Make an identifier-safe test-function suffix from a scenario id."""
    s = re.sub(r"\W+", "_", str(value)).strip("_")
    return s or "scenario"


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["slug"] = _slug
    env.filters["pyrepr"] = repr
    return env


def file_header(contract: TargetContract, scenarios=()) -> str:
    """Imports + a sys.path bootstrap so the target module + constructed types are importable."""
    root, _module = resolve_import(Path(contract.ref.locator))
    units = ", ".join(u.name for u in contract.units)

    # Collect the domain types + scalar ctors the scenarios construct, and import them.
    types_used: set[str] = set()
    calls_used: set[str] = set()
    enums_used: set[str] = set()
    for s in scenarios:
        for v in s.inputs.values():
            _collect_symbols(v, types_used, calls_used, enums_used)

    by_module: dict[str, set[str]] = {}
    for nm in types_used | enums_used:               # project types/enums → their source module
        ts = contract.types.get(nm)
        if ts:
            by_module.setdefault(ts.module, set()).add(nm)
    for nm in calls_used:                            # scalar ctors → stdlib module
        mod = _SCALAR_IMPORTS.get(nm)
        if mod:
            by_module.setdefault(mod, set()).add(nm)

    extra = "".join(f"from {mod} import {', '.join(sorted(names))}\n"
                    for mod, names in sorted(by_module.items()))

    return (
        '"""Generated by AI Test Pilot — REVIEW before promoting into a real suite.\n'
        'Tests are proposed, never auto-committed. Edit/curate, then move into place.\n'
        '"""\n'
        "import sys\n\n"
        "import pytest\n\n"
        f"sys.path.insert(0, {root.as_posix()!r})\n\n"
        f"from {contract.module} import {units}\n"
        f"{extra}"
    )


def _docstring(scenario: TestScenario) -> str:
    """Build the test's docstring in Python (avoids Jinja whitespace pitfalls)."""
    lines = [f'    """{scenario.title}', "",
             f"    Why: {scenario.rationale or '-'}",
             f"    Expected: {scenario.expected}"]
    if scenario.fixture:
        lines.append(f"    Fixture (synthetic-data-factory): {scenario.fixture}")
    lines.append('    """')
    return "\n".join(lines)


def _tmp_var(param: str) -> str:
    return f"p_{_slug(param)}"


def _render_setup(scenario: TestScenario) -> str:
    """Lines that create each tmp file via `tmp_path` before the call (already indented)."""
    lines: list[str] = []
    for tf in scenario.tmp_files:
        var = _tmp_var(tf.param)
        lines.append(f"    {var} = tmp_path / {tf.filename!r}")
        lines.append(f"    {var}.write_text({tf.text!r}, encoding='utf-8')")
    return "\n".join(lines)


def _render_value(node) -> str:
    """Render one input value-grammar node to a Python expression (recursive).

    Nodes: a JSON primitive (repr) · {"$type": T, "args": {...}} → T(...) ·
    {"$call": F, "args": [...] | {...}} → F(...) (scalar ctors) ·
    {"$enum": "E.MEMBER"} → literal · JSON list/dict → rendered recursively.
    """
    if isinstance(node, dict):
        if "$type" in node:
            args = node.get("args") or {}
            inner = ", ".join(f"{k}={_render_value(v)}" for k, v in args.items())
            return f"{node['$type']}({inner})"
        if "$call" in node:
            args = node.get("args") or []
            if isinstance(args, dict):
                inner = ", ".join(f"{k}={_render_value(v)}" for k, v in args.items())
            else:
                inner = ", ".join(_render_value(a) for a in args)
            return f"{node['$call']}({inner})"
        if "$enum" in node:
            return str(node["$enum"])
        return "{" + ", ".join(f"{k!r}: {_render_value(v)}" for k, v in node.items()) + "}"
    if isinstance(node, list):
        return "[" + ", ".join(_render_value(v) for v in node) + "]"
    return repr(node)


def _collect_symbols(node, types_used: set, calls_used: set, enums_used: set) -> None:
    """Walk a value tree collecting symbols that need importing."""
    if isinstance(node, dict):
        if "$type" in node:
            types_used.add(node["$type"])
            for v in (node.get("args") or {}).values():
                _collect_symbols(v, types_used, calls_used, enums_used)
            return
        if "$call" in node:
            calls_used.add(node["$call"])
            args = node.get("args") or []
            for v in (args.values() if isinstance(args, dict) else args):
                _collect_symbols(v, types_used, calls_used, enums_used)
            return
        if "$enum" in node:
            enums_used.add(str(node["$enum"]).split(".")[0])
            return
        for v in node.values():
            _collect_symbols(v, types_used, calls_used, enums_used)
    elif isinstance(node, list):
        for v in node:
            _collect_symbols(v, types_used, calls_used, enums_used)


def _render_call(scenario: TestScenario) -> str:
    """The function call expression, with tmp-file params bound to their created paths."""
    # Bind tmp-file params to the created Path OBJECT (not str): a Path is compatible
    # with both `Path`-only signatures (which call .read_text/.read_bytes) and `str|Path`
    # signatures (which do Path(path)); a bare str breaks the former.
    tmp_params = {tf.param for tf in scenario.tmp_files}
    parts: list[str] = []
    for k, v in scenario.inputs.items():
        parts.append(f"{k}={_tmp_var(k)}" if k in tmp_params else f"{k}={_render_value(v)}")
    for tf in scenario.tmp_files:                    # tmp params not also in inputs
        if tf.param not in scenario.inputs:
            parts.append(f"{tf.param}={_tmp_var(tf.param)}")
    return f"{scenario.unit}({', '.join(parts)})"


def emit(scenario: TestScenario, contract: TargetContract) -> str:
    """Render ONE scenario to a pytest function body (no file header)."""
    env = _jinja_env()
    template = env.get_template("pytest_function_v1.j2")
    func_args = "tmp_path" if scenario.tmp_files else ""
    return template.render(
        s=scenario,
        doc=_docstring(scenario),
        setup=_render_setup(scenario),
        call=_render_call(scenario),
        func_args=func_args,
        module=contract.module,
    )


def probe_source(contract: TargetContract, scenarios: list[TestScenario]) -> str:
    """A standalone script that calls each scenario's unit and prints {id, ok, repr|error}.

    Used by golden/characterization mode to capture the ACTUAL result of a call so the test can
    lock it in. Only for scenarios with no tmp_files and no expected error (plain in-memory calls).
    """
    lines = [file_header(contract, scenarios), "import json as _aitp_json", ""]
    for s in scenarios:
        call = _render_call(s)
        lines += [
            "try:",
            f"    _r = {call}",
            f"    print(_aitp_json.dumps({{'id': {s.id!r}, 'ok': True, 'repr': repr(_r)}}))",
            "except Exception as _e:",
            f"    print(_aitp_json.dumps({{'id': {s.id!r}, 'ok': False, 'error': repr(_e)}}))",
            "",
        ]
    return "\n".join(lines)


def test_function_name(scenario: TestScenario) -> str:
    """The pytest function name emitted for a scenario — used to map run results back."""
    return f"test_{_slug(scenario.id)}"


def runner_cmd(test_path: Path) -> list[str]:
    return ["pytest", "-q", "--no-header", str(test_path)]
