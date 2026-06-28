"""Layered config: committed TOML defaults → CLI overrides (env handles secrets,
loaded in llm_client). Validated by pydantic so a bad config fails fast and clear.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "ai_test_pilot.toml"


class RunCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    adapter: str = "python_pytest"
    scenario_count: int = 6          # FLOOR (and the fixed count when autoscale is off)
    scenarios_per_unit: int = 3      # autoscale: target ~this many scenarios PER unit (0 = fixed scenario_count)
    scenario_max: int = 30           # autoscale cap (token-budget bound)
    model_tier: str = "bulk"
    prompt_version: str = "v1"
    output_dir: str = "scripts/outputs"


def autoscale_count(run: RunCfg, n_units: int, override: int | None = None) -> int:
    """Effective scenario count for a target.

    An explicit `--count` (override) always wins. Otherwise scale with the number of units under test:
    `scenarios_per_unit * n_units`, clamped to `[scenario_count (floor), scenario_max]`. A fixed default
    of 6 silently under-tests multi-function modules (a 10-function module got 0.6 scenarios/function and
    its mutation kill rate cratered ~0.37 vs ~0.93 at proper density — see the blind-target experiment).
    Set `scenarios_per_unit = 0` to restore the legacy fixed `scenario_count`.
    """
    if override:
        return override
    if run.scenarios_per_unit <= 0:
        return run.scenario_count
    scaled = run.scenarios_per_unit * max(1, n_units)
    return max(run.scenario_count, min(scaled, run.scenario_max))


class GenCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    temperature: float = 0.0          # P1: deterministic by default (low run-to-run variance)
    compress_prompt: bool = False
    repair_retries: int = 1
    use_context: bool = True          # auto-detect agent/project.md|README near the target (domain VALUES)
    context_max_chars: int = 2000     # bound the injected excerpt (token budget)
    cut_source: bool = True           # P3a: feed the unit's own source (CUT context) for specific-behaviour assertions
    cut_source_max_chars: int = 1200  # per-unit source display cap (token budget)
    golden: bool = False              # characterization mode: lock asserts to captured results
    cache: bool = True                # P1: replay scenarios for an unchanged (target+prompt+model+temp)
    llm_timeout: float = 60.0         # P2: seconds per LLM request
    llm_retries: int = 2              # P2: transient-failure retries after the first attempt
    per_test_timeout: float = 15.0    # P2: per-test budget → bounds the run (0 = no cap)
    rejection_tests: bool = False     # also emit deterministic validator-rejection tests (opt-in)


class FixturesCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    project_path: str = "../synthetic-data-factory"
    domain: str = ""
    rows: int = 8


class TriageCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    llm_for_ambiguous: bool = True    # false = deterministic-only (zero triage tokens)
    confidence_threshold: float = 0.6


class TuningCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str = "propose"             # off | propose | auto (auto = propose + accepted-scenario few-shot)
    min_runs_for_selection: int = 5
    escalate_below_accept: float = 0.5
    fewshot_min_rate: float = 0.6     # auto: only reuse exemplars from runs with acceptance >= this
    fewshot_max_examples: int = 3     # auto: max accepted scenarios to inject as exemplars
    fewshot_max_chars: int = 1500     # auto: token-budget bound on the exemplar block


class LedgerCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    backend: str = "duckdb"
    path: str = "scripts/outputs/runs.duckdb"


class BudgetCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_tokens_per_run: int = 0       # P4: 0 = no cap; else estimate over this aborts/warns
    max_tokens_per_sweep: int = 0     # cap across a multi-target sweep (0 = no cap)
    on_over: str = "warn"             # warn | abort — default never blocks (opt-in caps)
    price_per_mtok_in: float = 0.0    # USD per 1M input tokens (for cost_est; 0 = unknown)
    price_per_mtok_out: float = 0.0   # USD per 1M output tokens
    default_out_per_scenario: int = 200   # output-token estimate when no ledger history exists


class TypedInputsCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # A3(c) fixture-injection hatch: map a type name → "dotted.module:builder_func". When a target
    # param is typed with one of these, the tool builds it by calling YOUR builder (imported into the
    # test) instead of trying to construct it — the safe escape for cyclic graphs / validator-heavy
    # configs the ast strategies can't build. The model still only chooses the builder's $type args.
    builders: dict[str, str] = {}


class FeedbackCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Approach 1 — feedback-driven regeneration (close the loop with the coverage signal).
    # OPT-IN: each round is an extra LLM call → real tokens. Off by default.
    enabled: bool = False             # turn the coverage-feedback loop on (also via --feedback)
    max_rounds: int = 1               # extra regeneration rounds (1-2; caps token cost)
    count: int = 3                    # additional scenarios requested per feedback round
    min_uncovered_lines: int = 1      # skip feedback when fewer than this many target lines are uncovered
    max_uncovered_shown: int = 25     # token-budget bound on the uncovered-lines block fed to the LLM
    probe_timeout: float = 45.0       # HARD cap (s) on the coverage probe; a pathological test under
                                      # `trace` (e.g. exponential recursion) is killed + skipped, not hung


class DetectionCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    corpus: str = "both"                   # both | quixbugs | mutation
    subset_size: int = 20                  # QuixBugs programs in a bounded run (None-able via CLI)
    max_mutants: int = 6                   # per-target mutation cap (bounds the in-repo corpus)
    mutation_seed: int = 0                 # deterministic mutant subset selection
    per_test_timeout: float = 4.0          # TIGHT per-test cap for kill-checks: healthy tests run in <1s,
                                           # so a mutant exceeding this is looping -> killed fast (an
                                           # infinite-loop mutant at the 15s generation cap crawls)
    ablation: bool = True                  # run the {naive,+cut_source,+golden,full} feature study
    detect_equivalent: bool = True         # 4-lite: drop behaviourally-equivalent survived mutants from
                                           # the denominator (differential fuzz) so kill rate is honest
    quixbugs_url: str = "https://github.com/jkoppel/QuixBugs.git"
    humaneval_url: str = "https://github.com/openai/human-eval.git"   # LOCKED holdout corpus source


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run: RunCfg = RunCfg()
    generation: GenCfg = GenCfg()
    fixtures: FixturesCfg = FixturesCfg()
    triage: TriageCfg = TriageCfg()
    tuning: TuningCfg = TuningCfg()
    ledger: LedgerCfg = LedgerCfg()
    budget: BudgetCfg = BudgetCfg()
    feedback: FeedbackCfg = FeedbackCfg()
    typed_inputs: TypedInputsCfg = TypedInputsCfg()
    detection: DetectionCfg = DetectionCfg()


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)
    return AppConfig(**data)
