# Benchmark — AI Test Pilot vs. hand-written suite (librarian `extractors.py`)

_Run 2026-06-21. Coverage measured with a zero-dependency stdlib-`trace` harness
(`benchmark/cov.py`); coverage.py is not installed and needs no admin to avoid._

## Ground truth
- **Hand-written:** `librarian/tests/test_extractors.py` — 17 tests, **integration-style**:
  drives the public `extract()` dispatcher against real sample files built by the
  `sample_dir` fixture, across every branch (csv/xlsx/pdf/docx/text/image/html/json/jsonl/
  tsv/markdown/OCR/unsupported), with a fake `vision_fn`.
- **AI Test Pilot:** **unit-style** — points at individual functions, feeds literal inputs.

## Whole-module line coverage of `extractors.py`
| Suite | Tests | Passing | Line coverage | Notes |
|---|---|---|---|---|
| Hand-written | 17 | 17 | **89.9%** (312/347) | Misses only the real `_default_vision_transcribe`/OCR path (bypassed by the fake vision fn) + a few edge branches |
| AI (3 pure helpers) | 11 | 10 | 28.8% (100/347) | Only *targets* 3 of ~20 functions — whole-module % is not the fair metric |

Whole-module % is apples-to-oranges (the AI suite deliberately targeted 3 functions). The
fair comparison is **coverage of the functions the AI actually targeted**:

## Per-targeted-function coverage (the fair comparison)
| Function | Hand-written | AI | Verdict |
|---|---|---|---|
| `_chunk_text` (L48–62) | misses the multi-paragraph split branch (L54–55) + whitespace fallback (L61) | **same misses** | tie — *both never exercise the core split* |
| `_flatten_json` (L251–266) | **misses the list branch (L261–264)** | **covers the list branch** | **AI better** |
| `_split_markdown_sections` (L344–360) | full | full | tie |

**Headline:** on the functions it targeted, the AI suite **matched or beat** the
hand-written suite — it covered the `_flatten_json` list branch the human suite misses.
Neither suite exercises `_chunk_text`'s real multi-paragraph split (L54–55): the human
sample files never exceed `_MAX_CHARS`, and the AI *tried* (`test_chunk_text_long`) but
fed a single-paragraph string and asserted wrong semantics — see below.

## Accuracy findings (the reliability signal that matters)
1. **Recurring bad scenario — no-docstring functions.** `_chunk_text` has **no docstring**,
   so the LLM guessed it was a character-splitter. It is a *paragraph-boundary packer*
   (splits only on `\n\n`, only when the buffer exceeds `max_chars`). Result: a wrong
   assertion (`len>=2 and all(len(c)<=max_chars)`) that fails on `'hello world', max_chars=5`.
   This same misunderstanding recurred across two independent runs → **systematic**, not random.
2. **File/IO functions without fixtures = non-runnable happy paths.** Pointed at `extract`,
   `_extract_csv`, `_extract_json`: **2/6 passed**. The only passes were error/dispatch cases
   needing no real file (unsupported extension, empty path). Every happy-path test fabricated
   a path (`'data.csv'`) → `FileNotFoundError`. The `is_pure=False` signal the introspector
   already computes was **not used** to prevent this.
3. **No flakiness** observed; deterministic emission + run.

## Conclusions → hardening actions
| Finding | Fix |
|---|---|
| No-docstring → wrong exact assertions | Prompt: when a function lacks a docstring/clear spec, assert **invariants/shape/length**, not guessed exact values; tag such scenarios `uncertain`. Surface "no docstring" per unit in the contract. |
| File functions → fabricated paths | (a) New `tmp_files` capability: a scenario can declare real temp files (via pytest `tmp_path`) and pass their paths as inputs — makes file-processing functions genuinely testable. (b) Prompt: never reference paths that don't exist; for an impure file-taking unit, either use `tmp_files` or omit the scenario. (c) Surface impurity per unit. |
| extract dispatches on suffix before reading | Covered by (b) — the LLM is told the real contract via the docstring/code; tmp_files give it valid suffixed files. |

See `BENCHMARK_extractors_after.md` for the post-hardening re-run.
