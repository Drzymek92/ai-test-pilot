# Map — entry / CLI / pipeline (`main.py` · `cli.py` · `pipeline.py`)

Split by concern: `scripts/main.py` is a thin launcher (re-exports `main`/`EXIT_*`/
`run_pipeline`); `scripts/cli.py` holds arg parsing + subcommand dispatch + the exit-code contract;
`scripts/pipeline.py` holds `run_pipeline(cfg, req)`; `scripts/core/report.py` renders the run report.
`run_pipeline` takes a typed `RunRequest` and is the single function the MCP server, `sweep`,
`quality`, and `detection` also call — one pipeline, not two.

## Entry points
- `cli.main(argv)` — dispatches by first token via `_SUBCOMMANDS`: `accept` → `_accept_cmd` (ledger
  backfill); `promote` → `_promote_cmd`; `discover` → `_discover_cmd`; `quality`/`detect`/`holdout`/
  `sweep` → their handlers; otherwise `--smoke` → one LLM call, or `load_config` →
  `RunRequest.from_namespace` → `run_pipeline` → print summary + caveats. (`main.py` re-exports it.)
- `_accept_cmd(rest)` — its own argparse; calls `ledger.backfill_acceptance`.
- `_discover_cmd(rest)` — `discover <project|path>` lists testable-now vs needs-fixtures targets
  across a project's `scripts/` (ast-only, zero tokens). `--changed` / `--since <ref>` restrict the
  scan to git-changed modules (`discover.git_changed_py` → `only=` filter) — the incremental path:
  regenerate tests only for what moved. Errors if the target isn't a git work tree.
- `_parse_args` — all flags: target/adapter/selector/count/model/prompt-version, `--no-run`,
  `--golden`, fixtures (`--fixtures`/`--fixture-domain`/`--fixture-entity`/`--fixture-rows`),
  context (`--context`/`--no-context`).

## `run_pipeline` stage order (the spine)
1. **introspect** (`adapter.introspect`) → contract; collect `complex_params` caveats for the report.
2. **1.5 fixtures** (opt-in): `fixtures.generate_fixture` → prompt block + bundle.
3. **1.6 context** (opt-in/auto): `context.load_context` (agent/project.md→README excerpt).
4. **1.7 prompt version**: `tuning.select_prompt_version` (`auto` → ledger-best).
5. **2 generate** (`generate.generate_scenarios`, adapter-aware) → ScenarioSet.
6. **2.5 bind fixtures** (`fixtures.bind_fixture_files`); **2.6 golden** (`golden.capture_goldens`+`apply_goldens`).
7. persist scenarios JSON → **3 materialize** (`.py` + optional `.spec.ts`) → **4 run** (`runner.run_tests`).
8. **5 triage** (`triage.triage`, only if failures) → counts.
9. build `RunReport` → **6 ledger** (`ledger.append RunRecord`) → **7 tuning** (`tuning.suggestions`).
10. `report.write_report` (`core/report.py`) → markdown (scenarios table + failure triage + tuning + caveats).

## Notes
- Adapter-agnostic: web vs python differences are entirely inside the adapter + `generate`'s
  `prompt_kind`/`describe_contract`; main doesn't branch on adapter (golden/fixtures are no-ops for web).
- Output paths under `scripts/outputs/{scenarios,tests,reports,fixtures}` + `runs.duckdb`; all timestamped by `run_id`.
- Guardrail: writes only under `scripts/outputs/`, never the target repo.
