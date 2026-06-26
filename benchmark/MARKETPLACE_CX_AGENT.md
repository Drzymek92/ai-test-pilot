# Pipeline validation — marketplace-cx-agent (2026-06-21)

Tested AI Test Pilot end-to-end against a *second, unrelated* project to check generalization
and to validate the data-factory **fixture-as-file bridge**.

## 1. Data-factory fixture-as-file bridge — `graphql_server/generate_seed.py::load_catalog`
`load_catalog(factory_csv)` literally ingests a synthetic-data-factory CSV (needs `name`,
`category`, `price_pln`). Ran:

```
main.py --target ...generate_seed.py --selector load_catalog \
        --fixtures --fixture-domain marketplace_offers --fixture-rows 8
```

Flow that fired: factory generated 8 real `marketplace_offers` → tool persisted the CSV →
LLM proposed a scenario with `tmp_files:[{param:"factory_csv", from_fixture:true}]` →
`bind_fixture_files` injected the **real CSV bytes** → emitter wrote them via `tmp_path` and
called `load_catalog(factory_csv=<that file>)`.

**Result: 4/4 passed.** The happy-path test ran against genuine factory data (8 Polish
listings — "Kawa Ziarnista Arabica…", perishable/personalized/digital_unsealed, real PLN
prices), plus auto-generated edge cases (no arg → built-in catalogue, empty CSV → fallback,
headers-only → fallback). This is the two portfolio projects genuinely composing: the factory
produces domain data; the tool feeds it as a real fixture to the consumer built to ingest it.

### New capability: `from_fixture`
- `TmpFile.from_fixture=true` → the tool fills the temp file with the actual factory file's
  contents (LLM only flags intent; the tool supplies the bytes — deterministic, not guessed).
- The prompt now advertises the fixture's filename + columns so the LLM knows to use it.

## 2. Typed-object generalization gap — `rules/commission.py::compute_commission`
`compute_commission(order: OrderView, config: RulesConfig)` takes domain objects.

**Before the guard:** the LLM fabricated `order`/`config` as plain dicts →
`AttributeError: 'dict' object has no attribute 'commission'` → **0/4 passed** (four broken,
confidently-wrong tests).

**Root cause:** introspection sees the annotations only as strings; the tool can't construct
`OrderView`/`RulesConfig` (nested domain types) from JSON primitives.

**Fix v1 (complex-param guard):** flagged the types and refused to fabricate dicts → 1/1 honest
error-path test instead of 4 broken ones.

**Fix v2 — Typed-Input Construction, Phase 1 (2026-06-21):** introspection now resolves the param
types from source (`ast`, never importing) into `TypeSpec`s — dataclass + pydantic v2 + enum,
recursively, with defaults — and the LLM builds them via a `$type`/`$call`/`$enum` value grammar the
emitter renders to real constructor calls + imports. **After:**
- `compute_commission` → **4/4 passed**, constructing nested `OrderView(... line_items=[LineItemView(...
  unit_amount=Decimal('100.00'))])` and `RulesConfig(commission=CommissionRules(per_category_pct={...}))`.
- `is_returnable` → **5/5 passed**, including using the `now=datetime(...)` override to make the
  14-day-window logic deterministic, with nested `ReturnsRules` and category-exclusion scenarios.
- Auto-detected `agent/project.md` as domain context → realistic categories/statuses (electronics,
  DELIVERED, PLN).

Honest residual: assertions are type/structure-level (the LLM won't guess exact quantized Decimal
math); a golden/characterization mode is the Phase 2 follow-up.

## Verdict
- **Generalizes cleanly** to a new project for primitive / dict / list / **file-taking** targets
  (the `load_catalog` bridge is a highlight) AND now **typed/domain-object** targets (the rules
  engine) via Phase 1 typed-input construction.
- **Remaining honest boundaries (Phase 2+):** pydantic validator-constrained fields, attrs/NamedTuple,
  Union of multiple project types, alternate constructors (`OrderView.from_graphql_order`), and
  *assertion strength* on computed results (golden/characterization mode). Genuinely unresolvable
  types still warn-and-skip rather than emit broken tests.
