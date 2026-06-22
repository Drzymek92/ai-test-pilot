"""Stage 7 — TUNE. Deterministic, human-gated, never edits prompts.

`propose` (the default): pick the best historical prompt version for this adapter from the ledger,
and emit a Tuning Suggestions block (prompt-version wins, low-acceptance escalation, recurring
triage patterns). Every automatic step here is a pure ledger query — no LLM, no file edits, no
auto-commit.

`auto` (M5) additionally CLOSES the loop deterministically: it injects previously-ACCEPTED
scenarios for the same target as few-shot exemplars into the single generation call (`fewshot_block`),
biasing the model toward the style the user kept. Still no extra LLM calls — the exemplars come from
persisted scenario JSONs, so the only cost is a few hundred extra prompt tokens.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.core import ledger
from scripts.core.models import ScenarioSet, TestScenario
from scripts.logger import get_logger

logger = get_logger("tuning")


def select_prompt_version(adapter: str, requested: str, *, ledger_path, min_runs: int) -> tuple[str, str | None]:
    """Resolve `prompt_version`. 'auto' → the ledger-best version (needs `min_runs`); else passthrough."""
    if requested != "auto":
        return requested, None
    stats = ledger.prompt_version_stats(adapter, ledger_path)
    eligible = [(v, a, n) for v, a, n in stats if n >= min_runs]
    if not eligible:
        return "v1", "prompt_version=auto but no accepted history yet → using v1"
    v, a, n = eligible[0]
    return v, f"prompt_version=auto → {v} (best historical acceptance {a:.0%}, n={n})"


def suggestions(*, adapter: str, target: str, prompt_version: str, triage_counts: dict[str, int],
                ledger_path, min_runs: int, escalate_below: float) -> list[str]:
    """Deterministic improvement proposals for the report. Never applied automatically."""
    out: list[str] = []

    if triage_counts.get("real_bug"):
        out.append(f"{triage_counts['real_bug']} failure(s) triaged **real_bug** — the target may have "
                   "a genuine defect; review before accepting these tests.")
    if triage_counts.get("bad_scenario", 0) >= 2:
        out.append(f"{triage_counts['bad_scenario']} scenario(s) triaged bad_scenario — refine the "
                   "target's docstring or the inputs/assertion guidance.")
    if triage_counts.get("env_issue"):
        out.append(f"{triage_counts['env_issue']} env_issue(s) — fix imports / sys.path before "
                   "judging the generated tests.")

    stats = ledger.prompt_version_stats(adapter, ledger_path)
    eligible = [(v, a, n) for v, a, n in stats if n >= min_runs]
    if len(eligible) >= 2 and eligible[0][0] != prompt_version:
        v, a, n = eligible[0]
        out.append(f"prompt **{v}** has higher historical acceptance ({a:.0%}, n={n}) than the current "
                   f"{prompt_version}; consider pinning it (or set prompt_version=auto).")

    ta = ledger.target_acceptance(adapter, target, ledger_path)
    if ta and ta[0] < escalate_below:
        out.append(f"acceptance on this target is {ta[0]:.0%} (< {escalate_below:.0%}, n={ta[1]}); "
                   "consider a stronger model tier (`--model` / model_tier=accurate).")

    return out


def _exemplar(s: TestScenario) -> dict:
    """A compact, prompt-friendly view of one accepted scenario (no rationale/prose noise)."""
    d: dict = {"id": s.id, "title": s.title, "unit": s.unit, "inputs": s.inputs}
    if s.tmp_files:
        d["tmp_files"] = [t.model_dump(exclude_defaults=True) for t in s.tmp_files]
    if s.assertion is not None:
        d["assertion"] = s.assertion
    if s.expect_error is not None:
        d["expect_error"] = s.expect_error
    if s.tags:
        d["tags"] = s.tags
    return d


def fewshot_block(*, adapter: str, target: str, ledger_path, out_base,
                  min_rate: float, max_examples: int, max_chars: int) -> tuple[str, str | None]:
    """`auto` mode: build a few-shot block of previously-ACCEPTED scenarios for THIS target.

    Deterministic — no LLM, no new tokens beyond the prompt itself. Reads the persisted scenario
    JSON of the best accepted run(s) for this exact target so the model matches the style/rigor the
    user kept. Returns (block, note); ("", None) when there is no accepted history yet.
    """
    runs = ledger.best_accepted_runs(adapter, target, ledger_path, min_rate=min_rate, limit=max_examples)
    if not runs:
        return "", None

    out_base = Path(out_base)
    candidates: list[TestScenario] = []
    best_run: str | None = None
    for run_id, _rate in runs:
        scen_file = out_base / "scenarios" / f"scenarios_{run_id}.json"
        if not scen_file.is_file():
            continue
        try:
            ss = ScenarioSet.model_validate_json(scen_file.read_text(encoding="utf-8"))
        except Exception as exc:           # a missing/garbled artifact must never break generation
            logger.warning("auto-tuning: could not load exemplars from %s: %s", scen_file.name, exc)
            continue
        best_run = best_run or run_id
        candidates.extend(ss.scenarios)

    if not candidates:
        return "", None

    candidates.sort(key=lambda s: "uncertain" in s.tags)   # confident exemplars first (stable)
    examples: list[dict] = []
    for s in candidates:
        cand = examples + [_exemplar(s)]
        if examples and len(json.dumps(cand, ensure_ascii=False)) > max_chars:
            break
        examples = cand
        if len(examples) >= max_examples:
            break
    if not examples:
        return "", None

    payload = json.dumps(examples, ensure_ascii=False, indent=1)
    block = (
        "## Accepted exemplars (auto-tuning)\n"
        "These scenarios for THIS target were reviewed and KEPT in earlier runs. Match their "
        "style, rigor, and assertion strength. Propose scenarios that cover any cases they miss "
        "(do NOT merely repeat them), and follow the same schema.\n"
        f"{payload}\n\n"
    )
    note = f"auto-tuning: injected {len(examples)} accepted exemplar(s) (best run {best_run})"
    return block, note
