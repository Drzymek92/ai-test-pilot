# Limitations & Boundaries

A deliberately honest map of what AI Test Pilot does **not** do (yet). It's a focused, tested tool
— not production test infrastructure — and knowing the edges is part of using it well. Each item is
a real boundary observed in development, not a hypothetical.

## Generation quality (the real frontier)

The tool is validated on a curated set of target shapes (typed business-rules engines, pure
data-transformation helpers, web forms, an authenticated app, a WebSocket feed). Outside that set,
quality degrades gracefully but is **not** guaranteed. Known out-of-scope input shapes:

- **Constrained / exotic types it won't construct:** pydantic validator-constrained fields,
  `attrs`/`NamedTuple`, a `Union` of several project types, and alternate constructors. These hit
  the deterministic **warn-and-skip** guard rather than producing a wrong test.
- **Third-party / dynamic parameter types** are not resolved from source — flagged as
  `complex_params` and skipped, not guessed.
- **Assertion strength:** for computed results the LLM asserts *type + structure*, not exact math,
  unless you opt into `--golden` (characterization mode), which locks the real computed value.
- **No-docstring functions:** the tool asserts invariants (type/shape/length) and tags the test
  `uncertain` rather than guessing exact behaviour. A weak-but-correct test beats a confident-wrong
  one — but it *is* weaker.
- **Binary file inputs** (pdf/xlsx/png) need real fixtures; auto-generated `tmp_files` cover text
  formats only (csv/json/txt/md/html).

## Determinism

The LLM step (scenario proposal) is **non-deterministic** — the same target can yield different
scenarios across runs. `--golden` + the run ledger mitigate this (lock values, track the best
prompt version), but there is no hard reproducibility guarantee on the *set* of scenarios.

## Scale & cost

Designed for per-target runs (a handful of flows each). It is **not** a repo-scale batch engine:
no built-in concurrency, rate-limit backoff, or token-budget guardrails for thousands of targets.
The `discover --changed` / `--since` incremental path narrows a run to git-changed modules, which
is the cheap way to stay within bounds — but large sweeps are still your responsibility to budget.

## Safety

Generated tests are **executed** (in a subprocess) to validate them. Targets are assumed to be
**trusted** code (your own projects). There is no sandbox — do not point it at untrusted code.
Introspected source is sent to the configured LLM gateway; mind data-governance for sensitive code.

## Packaging & install

`pip install -e .` installs an `ai-test-pilot` command, but the import package is still the generic
`scripts` (kept to avoid churning every internal import). That's fine for an editable/personal
install; a true PyPI distribution would rename it to `ai_test_pilot`. The web adapter's browser
(`python -m playwright install chromium`) is a separate, manual step; CI is browser-free by design
(web tests assert on the *generated source*, not a live browser).

## Scope

This is a build-to-learn / portfolio tool that I use on my own pipelines — honest framing, not
production infrastructure. See the README "How it works" section for what it *does* do well.
