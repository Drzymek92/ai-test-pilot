# Limitations & Boundaries

A deliberately honest map of what AI Test Pilot does **not** do. The **`python` adapter** is a tested,
quality-gated, cost-bounded tool the author relies on for their own pipelines — reliable for
**trusted code on a single machine**, not infrastructure for arbitrary or untrusted use. The **`web`
(Playwright) adapter remains a demo.** Knowing the edges is part of using it well; each item below is a
real boundary observed in development, not a hypothetical.

## Generation quality (the real frontier)

The tool is validated on a curated set of target shapes (typed business-rules engines, pure
data-transformation helpers, web forms, an authenticated app, a WebSocket feed). Outside that set,
quality degrades gracefully but is **not** guaranteed. Known out-of-scope input shapes:

- **Constrained / exotic types it won't construct:** params gated by a custom pydantic
  `@field_validator`, a `Union` of several project types, alternate constructors (classmethods /
  factory functions), and fixture-injected params. These hit the deterministic **warn-and-skip**
  guard rather than producing a wrong test. (Declarative pydantic `Field` constraints — `gt`/`le`/
  `min_length`/`Annotated[...]` — plus `attrs` and `NamedTuple` *are* now constructed, per P3b.)
- **Third-party / dynamic parameter types** are not resolved from source — flagged as
  `complex_params` and skipped, not guessed.
- **Assertion strength:** for computed results the LLM asserts *type + structure*, not exact math,
  unless you opt into `--golden` (characterization mode), which locks the real computed value.
- **No-docstring functions:** the tool asserts invariants (type/shape/length) and tags the test
  `uncertain` rather than guessing exact behaviour. A weak-but-correct test beats a confident-wrong
  one — but it *is* weaker.
- **Binary file inputs** (pdf/xlsx/png) need real fixtures; auto-generated `tmp_files` cover text
  formats only (csv/json/txt/md/html).

## Determinism (P1)

Generation now defaults to **`temperature=0`**, and a **scenario cache/lock** replays the same
scenarios for an unchanged target: the cache key is the full system+human prompt (which embeds the
target source, prompt version, and context blocks) plus the **resolved model version**, temperature,
and scenario count — so a re-run is identical, and any change (including a model upgrade) invalidates
the entry and regenerates. Use `--no-cache` to always call the LLM, or `--refresh-cache` to
regenerate and overwrite. `--golden` remains the recommended **regression** mode (locks assertions to
real computed values). Residual caveat: at `temperature>0` or `--no-cache`, the LLM step is still
inherently non-deterministic; the lock guarantees replay, not that two *fresh* generations match.

## Robustness & exit codes (P2)

Built to run non-interactively. The LLM call has a **timeout + exponential-backoff retry** and
raises on exhaustion rather than emitting a half-generated suite; a **malformed/syntax-error target
is skipped with a reason**, not a stack trace; and a **per-test time budget** caps the run so a
hanging generated test can't hang the pipeline (there's no `pytest-timeout` plugin here, so the cap
is enforced at the subprocess level = per-test budget × scenarios + buffer; it bounds the run rather
than isolating one test). Documented exit codes: **0** success · **1** internal error · **2** usage
(bad args) · **3** target error (uninspectable) · **4** LLM error (generation failed after retries) ·
**5** quality regression (the `quality` gate) · **6** budget exceeded (`on_over=abort`). Tunables in `[generation]` of
`config/ai_test_pilot.toml` (`llm_timeout`, `llm_retries`, `per_test_timeout`, `cache`).

## Quality gate (P5)

`python scripts/main.py quality` runs a small **curated, known-good** target set
(`benchmark/quality_targets.toml`) and reports a panel — coverage, pass-rate, **false-positive rate**,
error-rate, **test-smell density**, ledger acceptance — then exits 5 if any metric regressed vs the
stored baseline (`--update-baseline` to set one). Honest bounds: the **false-positive rate assumes the
target set is genuinely correct** (a failing test on known-good code is counted as a false positive);
the **smell checks omit "Magic Number"** on purpose (exact-value assertions are the goal here, not a
smell); coverage is line-coverage via the stdlib `trace` module (no branch/mutation coverage —
**mutation score is out of scope**, and per Pynguin only ~0.45-correlates with line coverage anyway).
A missing curated target (e.g. a sibling project absent in the portfolio copy) is skipped, not failed.

## Scale & cost (P4)

Every run **measures real token usage** (input/output, recorded to the ledger + report; a P1 cache
replay spends 0) and can compute a `cost_est` from a configurable price table (`[budget]`). **Budget
caps are opt-in** (`max_tokens_per_run` / `max_tokens_per_sweep`, `on_over=warn|abort`→exit 6, both 0
by default so nothing blocks unless you ask): the estimate counts input tokens from the prompt and
**estimates output from your own ledger history** (avg per scenario), so the cap **bounds surprise but
is not an exact guarantee** — output length is unknowable a priori. `sweep <project>` "tests the diff"
(`discover --changed` → generate per module) under the per-sweep cap. Still **not** a high-throughput
engine: **no concurrency** (sequential calls — deferred to avoid rate-limit risk), so very large sweeps
are slow even when bounded.

## Safety (P6)

**Trusted code only.** This tool both *introspects* a target (ast-only — it never imports it) and
**executes code** on its behalf: the generated tests run in a subprocess, and `--golden` runs a probe
that calls the target's own functions. Point it only at code you trust (your own projects).

**Defense-in-depth, not a sandbox.** Every spawned child is now **time-bounded** so a hang can't wedge
the tool: the test run (per-test budget), the golden probe (60s), the data-factory fixture child
(300s), the coverage trace (180s), and each git call (30s). This bounds *accidents* (infinite loops,
hangs) — it is **not** a security sandbox: there is no memory/CPU/filesystem isolation (OS-level
resource caps are Unix-only and intentionally out of scope here). Do not run it on hostile code.

**The model authors JSON, never code.** Generated tests are rendered deterministically from the
schema-validated scenario; the model never writes a code line. The value grammar
(`$type`/`$call`/`$enum` and constructor argument names) is **allow-listed against the types the tool
resolved from the target's own source — at both generation time and render time**. An unknown symbol
or a non-identifier arg name is rejected (routed through the repair retry), so a crafted
docstring/source string — which *is* fed to the prompt by default (`cut_source`) and whose output is
*executed* by pytest and the golden probe — cannot smuggle code tokens into the materialized test.

**Data governance.** Introspected source — including the **bounded CUT source slice** (P3a) and any
`agent/project.md`/README context — is sent to the configured LLM gateway for scenario
generation. For sensitive code, mind what leaves the machine; `--no-cut-source` / `--no-context`
reduce what is sent.

## Failure triage

When a generated test fails, `triage` classifies it deterministically (real_bug / bad_scenario /
flaky / env_issue) and only calls the LLM for genuinely ambiguous cases. **When the LLM is
unavailable, an ambiguous failure defaults to `bad_scenario` at low confidence (0.3).** For a *test
generator* that is the self-flattering direction — it can mask a real bug in the target — so treat a
low-confidence `bad_scenario` verdict as "a human should look", not "the code is fine".

## Packaging & install

`pip install -e .` installs an `ai-test-pilot` command, but the import package is still the generic
`scripts` (kept to avoid churning every internal import). That's fine for an editable/personal
install; a true PyPI distribution would rename it to `ai_test_pilot`. The web adapter's browser
(`python -m playwright install chromium`) is a separate, manual step; CI is browser-free by design
(web tests assert on the *generated source*, not a live browser).

**Raw drafts embed an absolute path.** A generated draft in `outputs/` bootstraps its imports with
`sys.path.insert(0, '<absolute target-project path>')`, so a raw draft only runs on the machine that
generated it and would leak that local path if shared verbatim. `promote` strips this bootstrap when
moving a test into a real suite — promote (don't copy a raw draft) before sharing.

## Efficacy evaluation (what the kill-rate numbers do and don't mean)

The `benchmark/` eval measures **mutation kill rate** (see the README "Does it actually catch bugs?"
and `benchmark/DETECTION.md`). Read the numbers with these caveats:

- **Small n → wide intervals.** The dev corpora are small (QuixBugs n=20, in-repo mutation n=11), so
  the point estimates carry **wide 95% Wilson CIs** (e.g. mutation 0.818 **[0.523–0.949]**). Every
  published rate is reported with its CI; don't read a bare point estimate as precise.
- **The held-out HumanEval 0.98 is NOT a PIT-style mutation score.** It is a kill rate over this
  repo's own *lightweight, capped* AST operators on an easy, fully-constructible distribution — a
  *generalization check*. The comparable figure is the **standard-tool (cosmic-ray) re-measure,
  0.923 [0.906–0.937]**, and even that is a **conservative lower bound** because cosmic-ray does not
  exclude equivalent mutants. The harder, most representative number is **QuixBugs 0.80**.
- **The Pynguin head-to-head is a floor for the peer.** Pynguin ran at a modest 30s search + SIMPLE
  assertions; a longer budget / mutation-analysis assertions would raise its 0.30. The comparison is
  honest about this — the conclusion (we sit above SBST) holds against the literature range too.
- **Anti-overfit, but not a guarantee.** The feedback loop is fed *only* coverage gaps, never the
  mutant set, and the HumanEval holdout was run once and never developed against — but a single
  held-out corpus on an easy distribution is evidence of generalization, not proof of it.
- **Reproducible, with a gateway.** `detect`/`holdout`/the baselines need an LLM gateway to generate
  suites (the P1 cache makes re-runs ~free); the deterministic kill-mechanic tests run with no gateway.

## Scope

The **`python` adapter** has crossed from demo to a tool I trust on my own pipelines day-to-day:
reproducible (P1), fail-safe with a CI-grade exit contract (P2), source-grounded + typed-input aware
assertions (P3a/P3b), a quality-regression gate (P5), measured spend with opt-in caps (P4), and a
documented safety boundary (P6). That is the honest claim — **reliable for trusted code on one
machine**, not hardened multi-tenant infrastructure. The **`web` (Playwright) adapter stays a demo.**
See the README "How it works" for what it does well.
