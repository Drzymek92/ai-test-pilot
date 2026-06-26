"""`python` → pytest adapter.

introspect: parse the target module with `ast` (NEVER import it — the target may pull
heavy/optional deps like fitz/docx; ast is deterministic and side-effect-free).
emit: render a pytest function from a validated TestScenario via a Jinja2 template.
runner_cmd: plain `pytest`.

The build-first, no-Node half of the tool.
"""
from __future__ import annotations

import ast
import builtins
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

# Call/attribute identifiers that mean IO / side effects → not "pure". Matched as exact AST
# node names (a called function, or an attribute/method name), NOT substrings — so `reopen`
# / `is_open` / `overwrite` no longer false-match `open` / `write`.
_IMPURE_NAMES = frozenset({
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "write",
    "save", "load_workbook", "connect", "request", "Document", "print", "input",
})
# Modules whose use (any `<module>.x` access, or a bare reference) means IO / side effects.
_IMPURE_MODULES = frozenset({"os", "subprocess", "requests", "urllib", "fitz", "docx"})


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


def _attr_root(node: ast.expr) -> str | None:
    """Leftmost Name id of an attribute chain ('urllib.request.urlopen' → 'urllib')."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_pure(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body shows no IO / side-effect call, method, or impure-module use.

    AST node-name detection (not substring): a called function or attribute whose exact
    name is impure, or any access on an impure module. Precise where the old substring
    scan over-matched (`reopen`/`is_open`/`overwrite`) and under-matched aliased IO.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _name_of(node.func) in _IMPURE_NAMES:
            return False
        if isinstance(node, ast.Attribute):
            if node.attr in _IMPURE_NAMES or _attr_root(node) in _IMPURE_MODULES:
                return False
        if isinstance(node, ast.Name) and node.id in _IMPURE_MODULES:
            return False
    return True


# Call names (function/method) and modules whose use means the function reads the wall clock /
# RNG → its result is not reproducible unless the relevant time is pinned via a parameter.
_CLOCK_CALL_NAMES = frozenset({"now", "today", "utcnow", "monotonic", "perf_counter",
                               "uuid4", "uuid1", "gmtime", "localtime"})
_CLOCK_MODULES = frozenset({"time", "random"})


def _reads_clock(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _name_of(node.func) in _CLOCK_CALL_NAMES:
            return True
        if isinstance(node, ast.Attribute) and _attr_root(node) in _CLOCK_MODULES:
            return True
        if isinstance(node, ast.Name) and node.id in _CLOCK_MODULES:
            return True
    return False


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


# Hard safety bound on stored unit source (the prompt-time display cap is smaller + configurable).
_SOURCE_HARD_CAP = 6000


def _unit_source(src: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The unit's own source (CUT context, P3a) — bounded so a giant function can't blow the prompt."""
    seg = ast.get_source_segment(src, fn)
    if not seg:
        return None
    if len(seg) > _SOURCE_HARD_CAP:
        seg = seg[:_SOURCE_HARD_CAP].rsplit("\n", 1)[0] + "\n# ...(source truncated)"
    return seg


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


# attrs decorators, by their unparsed callee — both the modern (`@define`/`@frozen`/`@mutable`)
# and classic (`@attr.s`/`@attrs.define`/...) spellings.
_ATTRS_DECOS = {
    "define", "frozen", "mutable", "attrs", "attr.s", "attr.attrs", "attr.define",
    "attr.frozen", "attr.mutable", "attrs.define", "attrs.frozen", "attrs.mutable",
}


def _classify(cls: ast.ClassDef) -> str | None:
    base_ids = {_name_of(b) for b in cls.bases}
    if base_ids & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
        return "enum"
    if any(b.endswith("BaseModel") or b == "BaseModel" for b in base_ids):
        return "pydantic"
    deco_ids = {_name_of(d.func if isinstance(d, ast.Call) else d) for d in cls.decorator_list}
    if "dataclass" in deco_ids:
        return "dataclass"
    deco_strs = {ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in cls.decorator_list}
    if deco_strs & _ATTRS_DECOS:
        return "attrs"
    if "NamedTuple" in base_ids:                  # class-form typing.NamedTuple (functional form is skipped)
        return "namedtuple"
    return None


# Field-spec calls whose presence on the RHS means "this is a declared field, not a class var",
# and whose default/factory kwargs decide whether the field is required.
_FIELD_CALLS = {"Field", "field", "ib", "attr.ib", "attrib", "attr.field"}
# Constraint kwargs the LLM should respect when choosing a value (pydantic Field / con* types).
_CONSTRAINT_KW = ("gt", "ge", "lt", "le", "multiple_of",
                  "min_length", "max_length", "pattern", "max_digits", "decimal_places")


def _is_field_call(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Call) and _name_of(value.func) in {c.split(".")[-1] for c in _FIELD_CALLS}


def _field_has_default(value: ast.expr | None) -> bool:
    if value is None:
        return False
    if _is_field_call(value):
        if any(kw.arg in ("default", "default_factory", "factory") for kw in value.keywords):
            return True
        # Field(...)/field()/attr.ib() with Ellipsis or no positional default means required.
        return bool(value.args) and not (
            isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis
        )
    return True


def _constraints_from_call(call: ast.Call) -> str | None:
    """Collect value constraints from a Field(...)/conint(...)/constr(...) call → 'gt=0, le=100'."""
    parts = [f"{kw.arg}={ast.unparse(kw.value)}" for kw in call.keywords
             if kw.arg in _CONSTRAINT_KW]
    return ", ".join(parts) if parts else None


def _field_constraint(node: ast.AnnAssign) -> str | None:
    """Constraints from the RHS Field(...) or from a con*()/Annotated[...] annotation (P3b-2)."""
    if isinstance(node.value, ast.Call) and _name_of(node.value.func) in {"Field", "field"}:
        c = _constraints_from_call(node.value)
        if c:
            return c
    # conint(gt=0) / constr(min_length=1) / confloat(...) / condecimal(...) as the annotation
    ann = node.annotation
    if isinstance(ann, ast.Call) and _name_of(ann.func).startswith("con"):
        return _constraints_from_call(ann)
    # Annotated[int, Field(gt=0)] — scan the metadata args for a Field/con* call
    if isinstance(ann, ast.Subscript) and _name_of(ann.value) == "Annotated":
        elts = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else []
        for meta in elts:
            if isinstance(meta, ast.Call):
                c = _constraints_from_call(meta)
                if c:
                    return c
    return None


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
                                    has_default=_field_has_default(node.value),
                                    constraint=_field_constraint(node)))
        # old-style attrs: `x = attr.ib(...)` / `x = field(...)` (no annotation)
        elif isinstance(node, ast.Assign) and _is_field_call(node.value) \
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n.startswith("_"):
                continue
            fields.append(FieldSpec(name=n, annotation=None,
                                    has_default=_field_has_default(node.value),
                                    constraint=_constraints_from_call(node.value)))
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


def _init_fields(cls: ast.ClassDef) -> list[FieldSpec] | None:
    """A3(b): construction signature of a PLAIN class — its `__init__` params (sans self).

    Returns the param FieldSpecs (so a plain class is built as `Name(...)` via the existing $type
    grammar), or None if the class can't be reliably constructed positionally (e.g. `__init__` takes
    only `*args` with required slots). No `__init__` → object's no-arg ctor → []. ast-only.
    """
    init = next((n for n in cls.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__"), None)
    if init is None:
        return []                                  # default object.__init__ → constructible with no args
    a = init.args
    allp = list(a.posonlyargs) + list(a.args)      # includes self at index 0
    if a.vararg and len(allp) <= 1:                # only `*args` (besides self) → can't name args
        return None
    n_def = len(a.defaults)
    defaulted = set(range(len(allp) - n_def, len(allp)))
    fields: list[FieldSpec] = []
    for i, arg in enumerate(allp):
        if arg.arg == "self":
            continue
        fields.append(FieldSpec(name=arg.arg, annotation=_annotation_str(arg.annotation),
                                has_default=i in defaulted))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        fields.append(FieldSpec(name=arg.arg, annotation=_annotation_str(arg.annotation),
                                has_default=d is not None))
    return fields


def _duck_attrs(fn: ast.FunctionDef | ast.AsyncFunctionDef, param: str) -> list[str]:
    """A3(a): attributes the body reads off `param` (`param.x`) — its inferred duck shape."""
    attrs: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == param:
            attrs.add(node.attr)
    return sorted(attrs)


def _ctor_hint(tree: ast.Module, type_name: str) -> str | None:
    """A3(b) caller-scan: how `type_name` is CONSTRUCTED in this module — `Name(value=, n=)`.

    A lite, same-module call-graph scan: surfaces the kwargs/positional arity callers actually pass,
    as a prompt hint so the model builds a realistic instance (not a structurally-valid-but-empty one).
    """
    kwargs: set[str] = set()
    max_pos = 0
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name_of(node.func) == type_name:
            found = True
            kwargs.update(kw.arg for kw in node.keywords if kw.arg)
            max_pos = max(max_pos, len(node.args))
    if not found:
        return None
    parts = [f"{k}=" for k in sorted(kwargs)]
    if max_pos:
        parts.insert(0, f"{max_pos} positional")
    return f"{type_name}({', '.join(parts)})" if parts else f"{type_name}()"


def resolve_type(name: str, project_root: Path, importing_module: Path,
                 *, types: dict[str, TypeSpec], seen: set[str],
                 builders: dict[str, str] | None = None, depth: int = 0) -> None:
    """Resolve `name` (used in `importing_module`) into a TypeSpec, recursing into fields.

    Strategy order (A3): user `builder` (config override) → structured (dataclass/pydantic/enum/
    attrs/namedtuple) → plain-class `initclass` (build via `__init__`). A name left OUT of `types`
    here is handled by the duck-typing post-pass in `introspect` (or finally stays 'complex').
    ast-only (never imports the target's project).
    """
    if name in types or name in seen or depth > 5:
        return
    seen.add(name)

    if builders and name in builders:              # A3(c): explicit user builder wins outright
        types[name] = TypeSpec(name=name, kind="builder", module="", builder=builders[name])
        return

    cls = _find_classdef(_parse_module(str(importing_module)), name)   # defined locally?
    if cls is not None:
        def_path, def_module = importing_module, resolve_import(importing_module)[1]
    else:
        dotted = _import_map(importing_module).get(name)
        if not dotted:
            return
        def_path = _module_to_path(dotted, project_root)
        if def_path is None:                       # third-party / unresolvable → leave for duck pass
            return
        cls = _find_classdef(_parse_module(str(def_path)), name)
        if cls is None:
            return
        def_module = dotted

    kind = _classify(cls)
    if kind == "enum":
        types[name] = TypeSpec(name=name, kind="enum", module=def_module,
                               enum_members=_enum_members(cls))
        return
    if kind is None:                               # A3(b): plain class → construct via __init__
        init_fields = _init_fields(cls)
        if init_fields is None:                    # not reliably constructible → leave for duck pass
            return
        types[name] = TypeSpec(name=name, kind="initclass", module=def_module, fields=init_fields,
                               usage_hint=_ctor_hint(_parse_module(str(def_path)), name))
        fields = init_fields
    else:
        fields = _extract_fields(cls)
        types[name] = TypeSpec(name=name, kind=kind, module=def_module, fields=fields)
    for f in fields:                               # recurse into nested project types
        if f.annotation:
            for ident in _type_identifiers(f.annotation):
                resolve_type(ident, project_root, def_path, types=types, seen=seen,
                             builders=builders, depth=depth + 1)


# ── adapter surface ──────────────────────────────────────────────────────────
def introspect(ref: TargetRef, *, builders: dict[str, str] | None = None) -> TargetContract:
    path = Path(ref.locator)
    if not path.is_file():
        raise FileNotFoundError(f"Target module not found: {path}")
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    wanted = {s.strip() for s in ref.selector.split(",")} if ref.selector else None

    units: list[UnitSpec] = []
    fn_by_name: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:                       # top-level functions only (M1)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if wanted is not None and node.name not in wanted:
                continue
            u = _unit_from_fn(node)
            u.source = _unit_source(src, node)   # P3a: CUT context for specific-behaviour assertions
            units.append(u)
            fn_by_name[node.name] = node

    if not units:
        scope = f" matching {sorted(wanted)}" if wanted else ""
        raise ValueError(f"No top-level functions{scope} found in {path}")

    root, module = resolve_import(path)

    # Resolve the closure of constructible types referenced by any unit's params:
    # builder override → structured → plain-class initclass (A3).
    types: dict[str, TypeSpec] = {}
    seen: set[str] = set()
    for u in units:
        for p in u.params:
            if p.annotation:
                for ident in _type_identifiers(p.annotation):
                    resolve_type(ident, root, path, types=types, seen=seen, builders=builders)

    # A3(a) duck-typing post-pass: any param type STILL unresolved (third-party / opaque / no usable
    # __init__) becomes a SimpleNamespace stand-in carrying exactly the attributes the body reads.
    for u in units:
        fn = fn_by_name[u.name]
        for p in u.params:
            if not p.annotation:
                continue
            for ident in _type_identifiers(p.annotation):
                if ident in types or not _is_duckable(ident):
                    continue
                attrs = _duck_attrs(fn, p.name)
                if not attrs:
                    continue                       # no observed shape → stay 'complex' (honest, avoids
                                                   # a meaningless empty SimpleNamespace — H4 guard)
                types[ident] = TypeSpec(
                    name=ident, kind="duck", module="",
                    fields=[FieldSpec(name=a, has_default=True) for a in attrs],
                    usage_hint=_ctor_hint(tree, ident))

    # A param is "complex/unsupported" ONLY if it still names a type no strategy could build.
    for u in units:
        u.complex_params = [f"{p.name}: {p.annotation}" for p in u.params
                            if p.annotation and _unresolved_idents(p.annotation, types)]

    return TargetContract(ref=ref, module=module, units=units, types=types)


# Typing/callable constructs a SimpleNamespace stand-in can't honestly model (not callable / iterable).
_NON_DUCK = frozenset({
    "Callable", "Iterator", "Generator", "AsyncIterator", "AsyncGenerator",
    "Awaitable", "Coroutine", "Type", "Protocol", "Annotated", "TypeVar",
})


def _is_duckable(ident: str) -> bool:
    """A name worth a SimpleNamespace stand-in — a real class name, not a typing/callable construct."""
    return ident not in _NON_DUCK and (ident[:1].isupper() or ident.startswith("_"))


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
    need_simplenamespace = False
    for nm in types_used | enums_used:               # project types/enums → their source module
        ts = contract.types.get(nm)
        if not ts:
            continue
        if ts.kind == "duck":                        # A3(a): SimpleNamespace stand-in (synthesized)
            need_simplenamespace = True
        elif ts.kind == "builder" and ts.builder:    # A3(c): import the user builder function
            mod, _, func = ts.builder.partition(":")
            by_module.setdefault(mod, set()).add(func)
        else:                                        # structured / initclass → import the real type
            by_module.setdefault(ts.module, set()).add(nm)
    for nm in calls_used:                            # scalar ctors → stdlib module
        mod = _SCALAR_IMPORTS.get(nm)
        if mod:
            by_module.setdefault(mod, set()).add(nm)

    extra = ("from types import SimpleNamespace\n" if need_simplenamespace else "")
    extra += "".join(f"from {mod} import {', '.join(sorted(names))}\n"
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


def _type_callable(ts: TypeSpec) -> str:
    """The Python callable a `$type` renders to: duck → SimpleNamespace, builder → its function,
    everything else (structured / initclass) → the type's own name."""
    if ts.kind == "duck":
        return "SimpleNamespace"
    if ts.kind == "builder" and ts.builder:
        return ts.builder.split(":")[-1]
    return ts.name


def _render_map(contract: TargetContract) -> dict[str, str]:
    """$type name → the callable to emit for it (A3: duck/builder redirect; others are identity)."""
    return {n: _type_callable(t) for n, t in contract.types.items()}


def _render_value(node, render: dict[str, str] | None = None) -> str:
    """Render one input value-grammar node to a Python expression (recursive).

    Nodes: a JSON primitive (repr) · {"$type": T, "args": {...}} → callable_for(T)(...) ·
    {"$call": F, "args": [...] | {...}} → F(...) (scalar ctors) ·
    {"$enum": "E.MEMBER"} → literal · JSON list/dict → rendered recursively.

    `render` maps a $type name to the callable to emit (A3): a `duck` type renders to
    `SimpleNamespace(...)`, a `builder` type to its builder function, others to the type name itself.
    """
    if isinstance(node, dict):
        if "$type" in node:
            args = node.get("args") or {}
            inner = ", ".join(f"{k}={_render_value(v, render)}" for k, v in args.items())
            sym = (render or {}).get(node["$type"], node["$type"])
            return f"{sym}({inner})"
        if "$call" in node:
            args = node.get("args") or []
            if isinstance(args, dict):
                inner = ", ".join(f"{k}={_render_value(v, render)}" for k, v in args.items())
            else:
                inner = ", ".join(_render_value(a, render) for a in args)
            return f"{node['$call']}({inner})"
        if "$enum" in node:
            return str(node["$enum"])
        return "{" + ", ".join(f"{k!r}: {_render_value(v, render)}" for k, v in node.items()) + "}"
    if isinstance(node, list):
        return "[" + ", ".join(_render_value(v, render) for v in node) + "]"
    return repr(node)


# ── value-grammar allow-list (security: the model authors JSON, never code tokens) ──
def _check_value(node, *, types: set[str], enums: dict[str, list[str]]) -> None:
    """Reject any value-grammar node that would render a symbol/name the tool did not
    itself resolve. Every `$type`/`$call`/`$enum` and every kwarg name is interpolated
    raw into a code position by `_render_value`, so this is the guard that keeps the
    'every line rendered deterministically, never written by the model' invariant true.
    Raises ValueError (routed through generate()'s repair-retry; also a render-time
    safety net in emit/probe_source). Primitives are repr()'d and need no check.
    """
    if isinstance(node, dict):
        if "$type" in node:
            t = node["$type"]
            if not isinstance(t, str) or t not in types:
                raise ValueError(
                    f"value grammar: $type {t!r} is not a resolved constructible type "
                    f"(known: {sorted(types)})")
            args = node.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError(f"value grammar: $type {t!r} 'args' must be a name->value object")
            for k, v in args.items():
                if not (isinstance(k, str) and k.isidentifier()):
                    raise ValueError(f"value grammar: invalid argument name {k!r} for $type {t!r}")
                _check_value(v, types=types, enums=enums)
            return
        if "$call" in node:
            f = node["$call"]
            if not isinstance(f, str) or f not in _SCALAR_IMPORTS:
                raise ValueError(
                    f"value grammar: $call {f!r} is not an allowed scalar constructor "
                    f"(known: {sorted(_SCALAR_IMPORTS)})")
            args = node.get("args") or []
            if isinstance(args, dict):
                for k, v in args.items():
                    if not (isinstance(k, str) and k.isidentifier()):
                        raise ValueError(f"value grammar: invalid argument name {k!r} for $call {f!r}")
                    _check_value(v, types=types, enums=enums)
            else:
                for v in args:
                    _check_value(v, types=types, enums=enums)
            return
        if "$enum" in node:
            ref = node["$enum"]
            if not isinstance(ref, str) or "." not in ref:
                raise ValueError(f"value grammar: $enum {ref!r} must be 'EnumName.MEMBER'")
            base, _, member = ref.partition(".")
            if base not in enums:
                raise ValueError(
                    f"value grammar: $enum {ref!r} names unknown enum {base!r} "
                    f"(known: {sorted(enums)})")
            members = enums[base]
            if members and member not in members:
                raise ValueError(
                    f"value grammar: $enum {ref!r} member not in {base} (members: {members})")
            return
        for v in node.values():                          # plain dict literal — repr'd keys, recurse values
            _check_value(v, types=types, enums=enums)
    elif isinstance(node, list):
        for v in node:
            _check_value(v, types=types, enums=enums)


# ── assertion / expect_error allow-list (security: both are interpolated RAW into executed
#    code by pytest_function_v1.j2 — `assert {{ s.assertion }}` and `pytest.raises({{ s.expect_error }})`.
#    Without this the model authors live code there (e.g. `__import__('os').system('...') or True`),
#    breaking the "every runnable line is rendered deterministically, never written by the model"
#    invariant — the same gap _check_value closes for inputs. Generation-time only (a ValueError
#    routes through generate()'s repair-retry); golden mode rewrites the assertion deterministically
#    AFTER this, so characterization locks are unaffected. ──

# Builtins safe to NAME or CALL inside an assertion. Deliberately EXCLUDES the introspection /
# escape primitives: eval, exec, compile, open, getattr, setattr, delattr, vars, globals,
# locals, __import__, input, breakpoint.
_ASSERT_BUILTINS = frozenset({
    "len", "abs", "all", "any", "isinstance", "issubclass", "sorted", "sum", "min", "max",
    "round", "type", "repr", "set", "frozenset", "list", "dict", "tuple", "str", "int",
    "float", "bool", "bytes", "range", "enumerate", "zip", "divmod", "hash", "ord", "chr",
    "hex", "format",
})

# Builtin exception names — the only un-imported symbols allowed as an `expect_error` target.
_BUILTIN_EXCEPTIONS = frozenset(
    n for n in dir(builtins)
    if isinstance(getattr(builtins, n), type) and issubclass(getattr(builtins, n), BaseException)
)


def _bound_names(tree: ast.AST) -> set[str]:
    """Identifiers bound locally inside the expression (comprehension targets, lambda args).
    Safe to reference as Names — they're author-chosen loop/arg vars that can't reach globals."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                for t in ast.walk(gen.target):
                    if isinstance(t, ast.Name):
                        bound.add(t.id)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
    return bound


def _check_assertion(expr: str, allowed_names: set[str]) -> None:
    """AST-walk a model-authored assertion expression; reject any Load-name not in
    `allowed_names` and any dunder name/attribute (the classic sandbox-escape vector).
    Method/attribute access on `result` is permitted (trusted-code domain); only NEW
    global symbols and dunder traversal are blocked. Raises ValueError."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"assertion is not a parseable expression: {expr!r} ({e})")
    names = allowed_names | _bound_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                raise ValueError(
                    f"assertion: dunder attribute '.{node.attr}' is not allowed (in {expr!r})")
        elif isinstance(node, ast.Name):
            nm = node.id
            if nm.startswith("__") and nm.endswith("__"):
                raise ValueError(f"assertion: dunder name {nm!r} is not allowed (in {expr!r})")
            if isinstance(node.ctx, ast.Load) and nm not in names:
                raise ValueError(
                    f"assertion: name {nm!r} is not allow-listed (in {expr!r}); allowed: "
                    f"result, resolved types/enums, scalar constructors, and a safe builtin subset")


def _check_expect_error(expr: str, allowed_exceptions: set[str]) -> None:
    """An `expect_error` must NAME a known exception (or be a tuple of them) — never an
    arbitrary expression. Raises ValueError on anything else."""
    try:
        body = ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"expect_error is not a parseable exception expression: {expr!r} ({e})")
    candidates = body.elts if isinstance(body, ast.Tuple) else [body]
    for c in candidates:
        if not isinstance(c, ast.Name):
            raise ValueError(
                f"expect_error must name an exception, or a tuple of them, not {expr!r}")
        if c.id not in allowed_exceptions:
            raise ValueError(
                f"expect_error: {c.id!r} is not a known exception "
                f"(a builtin exception or a type resolved from the target)")


def validate_scenario(scenario: TestScenario, contract: TargetContract) -> None:
    """Allow-list every value-grammar symbol in a scenario's inputs AND every symbol in its
    model-authored assertion / expect_error against what the tool resolved from the target's
    own source. Raises ValueError on any unknown symbol (routed through the repair-retry)."""
    types = set(contract.types)
    enums = {n: t.enum_members for n, t in contract.types.items() if t.kind == "enum"}
    for v in scenario.inputs.values():
        _check_value(v, types=types, enums=enums)
    if scenario.assertion:
        allowed = {"result"} | _ASSERT_BUILTINS | set(_SCALAR_IMPORTS) | types
        _check_assertion(scenario.assertion, allowed)
    if scenario.expect_error:
        _check_expect_error(scenario.expect_error, _BUILTIN_EXCEPTIONS | types)


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


def _render_call(scenario: TestScenario, render: dict[str, str] | None = None) -> str:
    """The function call expression, with tmp-file params bound to their created paths."""
    # Bind tmp-file params to the created Path OBJECT (not str): a Path is compatible
    # with both `Path`-only signatures (which call .read_text/.read_bytes) and `str|Path`
    # signatures (which do Path(path)); a bare str breaks the former.
    tmp_params = {tf.param for tf in scenario.tmp_files}
    parts: list[str] = []
    for k, v in scenario.inputs.items():
        parts.append(f"{k}={_tmp_var(k)}" if k in tmp_params else f"{k}={_render_value(v, render)}")
    for tf in scenario.tmp_files:                    # tmp params not also in inputs
        if tf.param not in scenario.inputs:
            parts.append(f"{tf.param}={_tmp_var(tf.param)}")
    return f"{scenario.unit}({', '.join(parts)})"


def emit(scenario: TestScenario, contract: TargetContract) -> str:
    """Render ONE scenario to a pytest function body (no file header)."""
    validate_scenario(scenario, contract)        # safety net: never render an un-allow-listed symbol
    env = _jinja_env()
    template = env.get_template("pytest_function_v1.j2")
    func_args = "tmp_path" if scenario.tmp_files else ""
    return template.render(
        s=scenario,
        doc=_docstring(scenario),
        setup=_render_setup(scenario),
        call=_render_call(scenario, _render_map(contract)),
        func_args=func_args,
        module=contract.module,
    )


def probe_source(contract: TargetContract, scenarios: list[TestScenario]) -> str:
    """A standalone script that calls each scenario's unit and prints {id, ok, repr|error}.

    Used by golden/characterization mode to capture the ACTUAL result of a call so the test can
    lock it in. Only for scenarios with no tmp_files and no expected error (plain in-memory calls).
    """
    for s in scenarios:                              # safety net before any code is emitted/executed
        validate_scenario(s, contract)
    render = _render_map(contract)
    lines = [file_header(contract, scenarios), "import json as _aitp_json", ""]
    for s in scenarios:
        call = _render_call(s, render)
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
