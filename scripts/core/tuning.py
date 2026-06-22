"""Stage 7 — TUNE (propose mode). Deterministic, human-gated, never edits prompts.

`propose` (the default): pick the best historical prompt version for this adapter from the ledger,
and emit a Tuning Suggestions block (prompt-version wins, low-acceptance escalation, recurring
triage patterns). Every automatic step here is a pure ledger query — no LLM, no file edits, no
auto-commit. `auto` (deterministic application) is deferred to M5.
"""
from __future__ import annotations

from scripts.core import ledger
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
