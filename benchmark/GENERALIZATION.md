# Cross-project generalization (2026-06-21)

AI Test Pilot run end-to-end against THREE unrelated projects to confirm it generalizes — not just
to the projects it was built against. Capabilities exercised: primitive/file targets, typed-object
construction (Phase 1), and characterization/golden assertions (Phase 2).

| Project | Target(s) | Mode | Result |
|---|---|---|---|
| librarian | `extractors.py` pure helpers | primitives, no-docstring guard | 5/6 (1 known bad-scenario on `_chunk_text`) |
| librarian / marketplace-cx-agent | `load_catalog(factory_csv)` | data-factory fixture-as-file | 4/4 |
| marketplace-cx-agent | `compute_commission(order: OrderView, config: RulesConfig)` | typed construction + `--golden` | **5/5**, full `CommissionBreakdown` locked |
| marketplace-cx-agent | `is_returnable(order, config, now)` | typed construction + `--golden` (clock-guarded) | **5/5**, full `ReturnDecision` locked |
| **fts_batch_sync** (new) | `flatten_fts.py`: `_slug`, `strip_html`, `split_score`, `_document_id`, `_prefix` | primitives + `--golden` | **8/8** |

## Phase 2 — characterization/golden mode
`--golden` runs each eligible call and rewrites its assertion to `repr(result) == <captured>`,
turning a weak `type(result).__name__ == 'X'` into a real regression lock. Example locks:
- `compute_commission`: `CommissionBreakdown(... items_commission=Decimal('65.00'),
  transaction_fee=Decimal('1.00'), total_commission=Decimal('66.00') ...)` — the Decimal math.
- `split_score('5- Transparent')` → `(5, 'Transparent')`; `split_score('Transparent')` → `(None, 'Transparent')`.
- `_document_id('…_prod_new')` → UUID portion; `_document_id('…')` → whole string.

**Safety (golden asserts CURRENT behaviour, so it must not lock the non-reproducible):**
1. probe runs TWICE; keep only results identical across runs (filters RNG / sub-second drift);
2. `UnitSpec.reads_clock` (ast heuristic) → skip clock/RNG units UNLESS the scenario pins a
   datetime/date param (kills the "passes today, fails next week" time-bomb) — verified on `is_returnable`;
3. skip file/error scenarios, default-`repr` objects (` at 0x`), and reprs > 600 chars.
Opt-in (`--golden` / `[generation] golden`); scenarios tagged `characterization`.

## Takeaway
The same engine produced runnable, correctly-typed, regression-grade tests on three codebases it
wasn't tailored to — typed domain objects (marketplace rules) and pure transformers (fts flatten)
alike — auto-using each project's `agent/project.md` for realistic values. The honest residual is
the deferred Phase 3 cases (pydantic validators, attrs/NamedTuple, Union-of-types, alternate
constructors), which warn-and-skip rather than emit broken tests.
