# Bug-Detection Evaluation — does the tool actually catch bugs?

The P5 `quality` gate proves the generated suite **runs and covers lines**. Coverage is a known
liar: a suite can execute every line and assert nothing useful. The real proof of a test generator
is **kill rate** — generate a suite from *correct* code, then re-run it against a *buggy* version and
count how many bugs it catches. This is the standard mutation-testing / QuixBugs metric.

## The metric

> A bug is **killed** when a generated test that **passed on the correct code** **fails on the buggy
> code**. `kill_rate = bugs killed / total bugs`.

Only tests that were green on the correct version are credited (a test already red on correct code
can't be said to have "caught" the bug). Higher is better.

## Two corpora

| Corpus | What | Why |
|---|---|---|
| **QuixBugs** (external) | 40 classic algorithms with human-verified `correct ↔ buggy` pairs (cloned to `benchmark/corpora/_cache/`, gitignored). Self-contained programs only. | Independent, third-party ground truth. Run at the tool's **default config** → the honest "what it does out of the box" number. |
| **Mutation** (in-repo) | The deterministic AST injector (`benchmark/mutation.py`) seeds one classic bug at a time (arithmetic/comparison/boolean/constant/return flips) into curated own-targets (`detection_targets.toml`). | Fully controlled + offline; the **ablation** substrate. |

## Headline result (baseline 2026-06-25, gemini-3.1-pro)

**QuixBugs (20 self-contained programs, default config): 16/20 verified bugs killed = 0.80.** All 20
produced a runnable suite, so scored == headline. The 4 misses are honest and instructive:
`breadth_first_search` / `depth_first_search` (the model built only 2 trivial graph inputs that don't
traverse far enough to hit the bug) and `lis` / `longest_common_subsequence` (DP algorithms whose suite
covers the function but the chosen inputs don't trigger the seeded edge-case — a test-INPUT-selection
limit, not a coverage gap). The full 40-program corpus is available via `--full` (≈2× the token cost;
the back half includes graph/object programs whose typed inputs the tool can't construct → green=0,
which the "scored" rate excludes).

## How our numbers compare (literature + peers)

All rates are binomial proportions over small mutant sets, so each carries a **95% Wilson CI** (A/M4) —
the point estimate alone overstates precision at these n. Comparable published scores are catalogued in
the shared resources KB (`research/llm-testgen-mutation-score-comparables/`).

| Source | Metric | Score (95% CI) | Notes |
|---|---|---|---|
| **Ours — QuixBugs** (default config) | mutation kill rate | **0.80 [0.584–0.919]** (n=20) | external verified correct↔buggy pairs |
| **Ours — in-repo mutation** (full) | mutation kill rate | **0.818 [0.523–0.949]** (n=11) | controlled AST mutants; ablation substrate |
| **Ours — HumanEval holdout (cosmic-ray, standard tool)** | mutation kill rate | **0.923 [0.906–0.937]** (n=1166) | held-out; easy distribution; equiv mutants NOT excluded → lower bound |
| **Pynguin** (SBST peer, same QuixBugs subset) | mutation kill rate | **0.30 [0.145–0.519]** (6/20; 30s/SIMPLE) | measured floor for Pynguin (modest budget/assertions); our same-subset kill mechanic |
| EvoSuite / SBST (literature) | mutation score | ~0.59–0.70 | Java SBST baselines reported across the field |
| Plain LLM test-gen (literature) | mutation score | ~0.70–0.78 | single-shot LLM, no feedback loop |
| SOTA mutation-guided (MutGen, literature) | mutation score | ~0.89 | uses the mutant set as a feedback signal |
| PBT-Bench (literature) | bug recall | 0.31–0.83 | property-based, task-dependent |

**Where we sit:** QuixBugs 0.80 / mutation 0.818 is **credible and well-placed** — above plain-LLM and
EvoSuite/SBST, below SOTA mutation-guided (which trains on the mutants; we deliberately never feed the
mutant set back — see our anti-overfit guard). Our literature-motivated edge is the **A1
coverage-feedback loop** (the ChatGPT-vs-SBST literature notes plain LLMs "lack feedback mechanisms").

**Head-to-head vs Pynguin (A/M2):** on the SAME 20 QuixBugs programs and the SAME kill mechanic, **ours
0.80 vs Pynguin 0.30** (Pynguin 6/20, 17/20 scored). Fairness caveat: Pynguin ran at a *modest* 30s
search + SIMPLE assertions, so 0.30 is a **floor** — a longer budget / mutation-analysis assertions would
raise it. The conclusion is robust either way: even the literature's stronger SBST range (~0.59–0.70)
sits **below** our 0.80. The gap is the expected LLM-vs-SBST story — the model proposes
semantically-meaningful inputs/assertions that blind search doesn't reach in a short budget. Artifact:
`benchmark/eval/baselines/pynguin_20260626_163700.json`.

**On the HumanEval holdout 0.98 (reframed, A/M1):** that figure is a kill rate over our *lightweight,
capped* operators on an easier distribution — a generalization check, **not** a PIT-style mutation score,
so it is **not** comparable to the rows above. The tool-comparable re-measure (cosmic-ray, richer `core/*`
operators, same held-out targets, same green suites) lands at **function-scoped 0.923 [0.906–0.937]**
(1076/1166 mutants over 39/50 scored targets; 11 trivial functions — e.g. `flip_case`, `strlen` — have no
mutable sites). As expected it's **below the inflated 0.98** (a richer operator set is harder to kill) and
it *is* a real mutation score. Two honest caveats keep it from being read as SOTA: HumanEval is an easy,
fully-constructible distribution, and **cosmic-ray does not exclude equivalent mutants → 0.923 is a
conservative lower bound**. The harder, more representative figure remains QuixBugs **0.80**. Artifact:
`benchmark/eval/holdout/cosmic/cosmic_ray_20260626_162325.json`.

## Honest measurement (4-lite, 2026-06-25)

Two changes make the mutation kill rate trustworthy before we tune anything against it:
- **Stronger operators** — the AST injector now also mutates membership (`in`↔`not in`), identity
  (`is`↔`is not`), and `min`↔`max`, on top of arithmetic/comparison/boolean/constant/return — so the
  in-repo corpus stresses more real bug shapes.
- **Equivalent-mutant detection** (`benchmark/equivalence.py`, `[detection] detect_equivalent`) — a
  survived mutant that is *behaviourally identical* to the correct code isn't a bug, so it must leave
  the denominator. Classification is **variant-independent and done once per target**: a mutant killed
  by ANY variant is provably distinct; the never-killed ones get a differential fuzz test (run
  correct-vs-mutant on same-shape perturbations of the suite's own inputs, pooled across variants). A
  weak suite therefore can't mislabel a real bug as "equivalent" (the bug that first appeared as golden
  spuriously hitting 1.0). Only plain-literal inputs are fuzzable; typed-object inputs → left in the
  denominator (conservative).

Dev baseline after 4-lite: QuixBugs 0.80; mutation naive 0.636 / cut_source 0.818 / golden 0.818 /
full 0.818 (1 equivalent mutant excluded). The golden==cut_source substitute finding survives the
harder, honest corpus.

## The ablation — is it overengineered?

The mutation corpus is regenerated under four variants, isolating the assertion-strength features:

| Variant | cut_source (P3a) | golden (characterization) |
|---|---|---|
| `naive` | off | off |
| `+cut_source` | on | off |
| `+golden` | off | on |
| `full` | on | on |

Reported deltas: `cut_source_contributes = full − golden`, `golden_contributes = full − cut_source`,
`full_over_naive`. If a feature adds ~no kill rate, that is concrete evidence it isn't earning its
complexity for bug detection. (Tuning is forced off here to remove confounds; the `--context`
feature is domain-value realism, not assertion strength, so it's outside this ablation.)

## How the mechanic reuses the pipeline (no duplication)

1. Copy the **correct** source into an isolated temp dir (originals are never mutated in place).
2. `run_pipeline(..., no_run=True)` introspects + generates + materializes a suite against it.
3. Run the suite once → the **green baseline** (passing scenario ids).
4. For each bug variant: overwrite the temp source file (same path → the generated test's `sys.path`
   import still resolves), call `run_tests` again — **zero extra tokens** — then restore the correct
   source. Killed iff a green test now fails.

Detection generations route to a throwaway ledger (`scripts/outputs/_detection_ledger.duckdb`) so the
real `runs.duckdb` and tuning history stay clean.

## Finding: golden mode is a SUBSTITUTE for cut-source, not a complement (2026-06-25)

The ablation was run on two corpora to test whether golden/characterization mode earns its keep for
**bug detection** (it doubles the per-call cost — it runs the target an extra time to capture results):

| corpus | naive | +cut_source | +golden | full | full − cut_source (golden's marginal lift) |
|---|---|---|---|---|---|
| main (simple pure fns: synthetic + extractors) | 0.50 | 0.75 | 0.667 | 0.75 | **+0.000** |
| no-docstring, hard-to-predict (`nodoc_sample.py`, 12 mutants) | 0.333 | **0.833** | **0.833** | 0.833 | **+0.000** |

Reading it:
- **cut_source clearly earns its keep:** +0.25 (main) and **+0.50** (hard) kill rate over the naive
  baseline. It is the single biggest lever, and it's cheap (just feeds the unit's own source).
- **golden reaches the SAME kill rate as cut_source but never beats it.** On the hard corpus golden
  alone also lifts naive→0.833 (+0.50), i.e. it independently solves the no-docstring weakness — but
  stacking it on cut_source (`full`) adds **+0.000** on both corpora. They are **substitutes**: each
  produces exact-value assertions, so once one is on, the other is redundant for catching injected bugs.
  (Note the green baseline stayed at 6/6 across variants — golden didn't rescue *more* passing tests
  here; the model already predicted correctly from source, so golden only matched it.)

**Position taken:** for the `python_pytest` adapter's bug-detection job, **cut_source is the better
default and golden is redundant on top of it** — golden costs a second target execution to reach a
kill rate cut_source already achieves for free. Golden's honest, non-overlapping value is a *different*
goal — **regression-locking current behaviour** (assert "this hasn't changed", incl. exact Decimal math
the model can't reproduce by reading) — and the fallback when cut_source must be off (oversized source,
or not sending source to the gateway). It should be framed/sold that way, **not** as a bug-detection
booster. This is a concrete "watch for overengineering" data point: keep golden, but scope its claim.

## Running it (the trackable procedure)

```powershell
$env:PYTHONIOENCODING="utf-8"
cd projects\ai-test-pilot

# First time — bounded subset, set the baseline (clones QuixBugs on first run):
python scripts\main.py detect --subset 8 --update-baseline

# Re-run any time (cache makes unchanged targets ~free) — appends a row, gates vs baseline:
python scripts\main.py detect --subset 8

# Cheapest live run (validates wiring): mutation corpus, full variant only, no QuixBugs:
python scripts\main.py detect --corpus mutation --no-ablation
```

> **Note on cost.** Every `detect` run must first GENERATE a suite (one LLM call per
> target/program/variant), so no `detect` invocation is zero-LLM — but the per-mutant kill-checks are
> free (source-swap + re-run only), and the P1 scenario cache makes a *repeat* run of unchanged
> targets ~free. The detection *mechanic* (green baseline → swap → kill check) is covered offline by
> `tests/test_detection.py`.

Each run writes `benchmark/eval/runs/detection_<ts>.json` + `.md`, appends a row to
`benchmark/eval/EVAL_DETECTION.md` (the time series — run it a couple of times and compare rows), and
exits **7** if a kill-rate regressed vs `benchmark/detection_baseline.json` (set with
`--update-baseline`, `--tol N` to ignore small moves). The model id is recorded so a model change is
visible. Flags: `--corpus both|quixbugs|mutation`, `--full` (whole QuixBugs corpus),
`--no-ablation`, `--no-clone`.
