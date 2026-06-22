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
    scenario_count: int = 6
    model_tier: str = "bulk"
    prompt_version: str = "v1"
    output_dir: str = "scripts/outputs"


class GenCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    temperature: float = 0.2
    compress_prompt: bool = False
    repair_retries: int = 1
    use_context: bool = True          # auto-detect agent/project.md|README near the target
    context_max_chars: int = 2000     # bound the injected excerpt (token budget)
    golden: bool = False              # characterization mode: lock asserts to captured results


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


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run: RunCfg = RunCfg()
    generation: GenCfg = GenCfg()
    fixtures: FixturesCfg = FixturesCfg()
    triage: TriageCfg = TriageCfg()
    tuning: TuningCfg = TuningCfg()
    ledger: LedgerCfg = LedgerCfg()


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)
    return AppConfig(**data)
