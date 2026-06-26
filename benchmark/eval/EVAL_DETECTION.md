# Bug-detection eval — tracked runs

Kill rate = fraction of bugs caught (a test green on correct code fails on the buggy version). Higher is better. Re-run: `python scripts/main.py detect`.

Methodology + the corpora are in `benchmark/DETECTION.md`. The baseline the gate compares against lives in `benchmark/detection_baseline.json`. Each `detect` run appends one row below; a kill-rate drop vs baseline exits 7. QuixBugs runs at **20 programs** (`[detection] subset_size`); changing the subset changes the `quixbugs_kill` denominator, so keep it fixed for comparable rows.

| ts | model | quixbugs_kill (n=20) | mut_naive | mut_full | full_over_naive | gate |
|---|---|---|---|---|---|---|
| 2026-06-25T09:10:01 | gemini-3.1-pro | 0.80 | 0.636 | 0.818 | 0.182 | PASS (baseline) |

<!-- Baseline reset 2026-06-25 09:10 after 4-lite (stronger mutation operators + honest, variant-
independent equivalent-mutant detection). The mutation numbers shifted vs the earlier 0.5/0.75 baseline
because the corpus is now harder (membership/identity/min-max operators) AND fairer (1 behaviourally-
equivalent mutant excluded from the denominator). QuixBugs (0.80) is unchanged (real curated bugs, no
equivalence step). Earlier rows dropped: harness bring-up + per-variant-equivalence iterations (the
latter had a bug — equivalence is now classified once per mutant, killed-by-any ⇒ distinct). The
golden-mode no-docstring study and the HumanEval+ HOLDOUT are separate corpora, NOT in this series. -->
| 2026-06-25T10:06:25 | gemini-3.1-pro | — | 0.727 | 0.818 | 0.091 | PASS |
| 2026-06-25T11:12:46 | gemini-3.1-pro | 0.667 | — | — | — | PASS |
| 2026-06-25T11:54:17 | gemini-3.1-pro | — | — | 0.833 | — | PASS |
| 2026-06-25T12:57:38 | gemini-3.1-pro | — | — | 0.917 | — | PASS |
| 2026-06-25T13:14:53 | gemini-3.1-pro | — | — | 0.5 | — | PASS |
| 2026-06-25T13:37:28 | gemini-3.1-pro | — | — | 0.9 | — | PASS |
| 2026-06-25T13:38:43 | gemini-3.1-pro | — | — | 0.583 | — | PASS |
| 2026-06-25T13:40:39 | gemini-3.1-pro | — | — | 0.467 | — | PASS |
| 2026-06-25T13:42:34 | gemini-3.1-pro | — | — | 0.367 | — | PASS |
| 2026-06-25T13:44:34 | gemini-3.1-pro | — | — | 0.367 | — | PASS |
| 2026-06-25T14:21:15 | gemini-3.1-pro | — | — | 0.75 | — | PASS |
| 2026-06-25T14:24:17 | gemini-3.1-pro | — | — | 0.933 | — | PASS |
| 2026-06-25T14:27:25 | gemini-3.1-pro | — | — | 0.967 | — | PASS |
| 2026-06-25T14:30:39 | gemini-3.1-pro | — | — | 0.933 | — | PASS |
| 2026-06-25T15:15:23 | gemini-3.1-pro | — | — | 0.933 | — | PASS |
| 2026-06-25T20:35:34 | gemini-3.1-pro | 0.75 | — | — | — | REGRESSION |
