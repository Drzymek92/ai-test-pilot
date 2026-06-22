# Map — `adapters/python_pytest.py`

The `python` → pytest adapter: the densest module in the project. Implements the Adapter Protocol
(`adapters/base.py`) plus the typed-input-construction machinery. **ast-only — never imports the
target** (the target may pull heavy/optional deps). Read this instead of scanning the 530+ lines.

## Responsibilities (Protocol surface)
| Function | Stage | Notes |
|---|---|---|
| `introspect(ref)` | 1 | top-level functions → `UnitSpec[]`; resolves the closure of constructible param types → `TargetContract.types`; sets `complex_params` for UNRESOLVED types only |
| `describe_contract(contract)` | 2 | LLM-facing text: signatures + docstrings + purity/clock + the `## Constructible types` schema |
| `file_header(contract, scenarios)` | 3 | sys.path bootstrap + `from <module> import <units>` + auto-collected imports for every `$type`/`$call`/`$enum` symbol the scenarios use |
| `emit(scenario, contract)` | 3 | one pytest function via the Jinja template `prompts/templates/pytest_function_v1.j2` |
| `probe_source(contract, scenarios)` | golden | a script printing `{id, ok, repr|error}` per scenario (used by `core/golden.py`) |
| `test_function_name(scenario)` | run | `test_<slug(id)>` — lets `core/runner.py` map JUnit results back |
| `runner_cmd(test_path)` | 4 | `["pytest","-q","--no-header", path]` |

`prompt_kind = "python"` (tells `core/generate.py` which prompt + describe to use).

## Type resolution (typed-input construction, Phase 1)
- `resolve_type(name, project_root, importing_module, *, types, seen, depth)` — **recursive, ast-only.**
  Finds the defining module (local ClassDef, else via `_import_map` of the importing module),
  classifies (`_classify`: dataclass decorator / `BaseModel` base / `Enum` base), extracts fields
  (`_extract_fields`, with `_field_has_default` handling pydantic `Field(default_factory=...)` /
  `Field(...)` Ellipsis-required), recurses into nested project types. Depth cap 5 + `seen` cycle set.
  Unresolvable (third-party / attrs / dynamic) → left out of `types` → stays in `complex_params`.
- `_module_to_path` maps a dotted module to a file under the project root (reuses `resolve_import`).

## Value grammar (emission)
- `_render_value(node)` — recursive renderer for the LLM's input values:
  `{"$type":T,"args":{…}}` → `T(…)` · `{"$call":F,"args":[…]}` → `F(…)` (Decimal/datetime/UUID) ·
  `{"$enum":"E.MEMBER"}` → literal · lists/dicts/primitives → recursed/`repr`.
- `_collect_symbols` walks the value tree so `file_header` imports exactly what's constructed.
- `_render_call` binds tmp-file params to the created **Path object** (not str), else `_render_value`.
- `_render_setup` writes `tmp_files` via `tmp_path`; `_docstring` built in Python (avoids a Jinja
  `trim_blocks` whitespace bug).

## Heuristic constant tables (top of file)
- `_CONSTRUCTIBLE` — annotation identifiers buildable from JSON primitives (builtins + typing +
  Path/datetime/Decimal/UUID). Anything else is a candidate domain type to resolve.
- `_SCALAR_IMPORTS` — `$call` ctor → stdlib module (Decimal→decimal, datetime→datetime, …).
- `_IMPURE_HINTS` → `is_pure`; `_CLOCK_HINTS` → `reads_clock` (golden mode skips clock units unless time pinned).

## Gotchas
- Introspection is **ast-only**; if a target type can't be resolved it degrades to the
  `complex_params` warn-and-skip guard — never fabricates a dict.
- `file_header` imports only symbols present in `contract.types`; an LLM-hallucinated `$type` will
  NameError at run (surfaced as a bad scenario by triage), by design.
