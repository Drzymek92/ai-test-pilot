# Map — `main.py`

CLI entry point + the orchestration that every interface reuses. `run_pipeline(cfg, args)` is the
single function the MCP server (`mcp_server.py`) also calls — so there is one pipeline, not two.

## Entry points
- `main(argv)` — dispatches: `accept <run_id> --kept N` → `_accept_cmd` (ledger backfill);
  `--smoke` → one LLM call; otherwise `load_config` → `run_pipeline` → print summary + caveats.
- `_accept_cmd(rest)` — its own argparse; calls `ledger.backfill_acceptance`.
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
10. `_write_report` → markdown (scenarios table + failure triage + tuning sections + caveats).

## Notes
- Adapter-agnostic: web vs python differences are entirely inside the adapter + `generate`'s
  `prompt_kind`/`describe_contract`; main doesn't branch on adapter (golden/fixtures are no-ops for web).
- Output paths under `scripts/outputs/{scenarios,tests,reports,fixtures}` + `runs.duckdb`; all timestamped by `run_id`.
- Guardrail: writes only under `scripts/outputs/`, never the target repo.
