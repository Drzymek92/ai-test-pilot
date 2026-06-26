# Benchmark — post-hardening re-run (librarian `extractors.py`)

_Run 2026-06-21, after the fixes in `BENCHMARK_extractors.md`. Same targets, same tool,
improved prompt + emitter._

## What changed in the tool
1. **`tmp_files` capability** (`models.TmpFile`, emitter): a scenario can create real temp
   input files via pytest's `tmp_path` and bind their paths to params — file-processing
   functions become genuinely testable instead of failing on fabricated paths.
2. **Emitter binds the Path OBJECT, not `str(path)`** — works for `Path`-only signatures
   (`.read_text`/`.read_bytes`) and `str|Path` signatures alike. (This was a real emitter bug
   the benchmark surfaced: `str()` broke `_extract_csv`/`_extract_json`.)
3. **Prompt + contract directives:** functions with no docstring → assert invariants
   (type/shape/length), not guessed exact values, tag `uncertain`; never reference a path
   that doesn't exist; impure file functions must use `tmp_files` or be omitted. Per-unit
   `docstring: NONE` and `IMPURE` markers are now surfaced in the introspection contract.

## Results: before → after
| Target set | Before | After | Change |
|---|---|---|---|
| IO funcs (`extract`, `_extract_csv`, `_extract_json`) | **2/6 pass** (fabricated paths) | **7/7 pass** (real `tmp_files`) | +bad-scenario elimination |
| Pure helpers (`_chunk_text`, `_flatten_json`, `_split_markdown`) | 10/11 (1 recurring bad scenario) | **10/10** | recurring `_chunk_text` bad scenario gone |

The IO 3/7→7/7 jump was confirmed **deterministically** by re-materializing the *same*
scenario JSON with the fixed emitter (zero new LLM tokens) — isolating the `str`→`Path` fix
as the cause.

## Accuracy: the recurring bad scenario, resolved
`_chunk_text` has no docstring. Before, the LLM asserted `len>=2 and all(len(c)<=max_chars)`
(wrong — it's a paragraph packer, not a char splitter) → false failure, twice.
After, the directive made it assert `len(result) >= 1 and all(isinstance(c, str) ...)` — a
**correct invariant**. Weaker, but right. (A wrong test is worse than a weak one — especially
for a tool meant to run across many projects.)

## Honest residual limitation
Neither suite exercises `_chunk_text`'s real multi-paragraph split branch (L54–55). Without a
docstring, the tool now *correctly declines to guess* the exact split semantics rather than
asserting them wrongly. Closing that branch needs either a docstring on the source function or
a human-authored case — the tool degrades gracefully (weak-but-correct) instead of failing.

## Net verdict
On the functions it targets, AI Test Pilot now produces **runnable, correct** tests for both
pure and file-processing functions, matches/beats the hand-written suite's per-function
coverage (e.g. the `_flatten_json` list branch), and no longer emits false-failing scenarios
for no-docstring or IO targets. The remaining honest gaps are exact-behaviour assertions on
undocumented functions (deferred to a human, by design) and binary file formats (pdf/xlsx/png
need real fixtures — use `--fixtures` or a sample dir).
