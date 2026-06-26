"""Bug-detection (kill-rate) evaluation — the proof the generated tests CATCH bugs.

The P5 `quality` gate proves the suite runs and covers lines; it does NOT prove the tests detect
bugs. This module closes that gap with the literature-standard metric — **mutation kill rate**:
generate a suite from CORRECT code, then re-run it against a buggy version; a bug is *killed* when a
test that passed on the correct code now fails. Coverage can be 100% while kill rate is 0.

Two corpora (chosen with the user):
  - **QuixBugs** (external, verified): 40 algorithms with human-curated correct<->buggy pairs. The
    headline external number, run at the tool's DEFAULT config (the honest "what it does out of the
    box" figure).
  - **Mutation** (in-repo, controlled): the deterministic AST injector (`benchmark/mutation.py`)
    seeds bugs into curated own-targets. This is the **ablation** substrate — we regenerate the suite
    under {naive, +cut_source, +golden, full} and compare kill rates, so each "complex" feature has
    to earn its detection rate (directly answering "is this overengineered?").

The mechanic reuses the pipeline wholesale: `run_pipeline` (no_run) generates+materializes against a
CORRECT copy in an isolated temp dir; then we swap the source file for each bug variant and re-call
`run_tests` (scripts/core/runner.py) — zero extra tokens per mutant. The original project files are
never mutated in place (always a temp copy). Detection generations route to a throwaway ledger so the
real `runs.duckdb` and tuning history stay clean, and tuning is forced off to remove confounds.

Trackable: each run writes `benchmark/eval/runs/detection_<ts>.json` + a per-run `.md`, appends a row
to `benchmark/eval/EVAL_DETECTION.md`, and gates the kill-rate panel against
`benchmark/detection_baseline.json` (regression -> exit 7) via `commons/evals/regression.py`.
"""
from __future__ import annotations

import json
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.config import AppConfig
from scripts.core import registry
from scripts.core.errors import BudgetError, LLMError, TargetError
from scripts.core.models import RunReport, RunRequest, ScenarioSet
from scripts.core.runner import run_tests
from scripts.llm_client import resolve_model
from scripts.logger import get_logger

logger = get_logger("detection")

# Ablation variants — driven purely through the generation knobs the pipeline already exposes.
# cut_source (P3a, feed the unit's own source) and golden (characterization, lock asserts to captured
# results) are the assertion-STRENGTH features most likely to drive kill rate, so they're the sharpest
# test of whether the complexity pays off. Leave-one-out falls out of comparing full vs each single.
ABLATION_VARIANTS: dict[str, dict[str, bool]] = {
    "naive":      {"cut_source": False, "golden": False},
    "cut_source": {"cut_source": True,  "golden": False},
    "golden":     {"cut_source": False, "golden": True},
    "full":       {"cut_source": True,  "golden": True},
}
# The tool's out-of-the-box config for the external headline number (golden is off by default).
DEFAULT_VARIANT = {"cut_source": True, "golden": False}

_DETECTION_LEDGER = "scripts/outputs/_detection_ledger.duckdb"   # throwaway; keeps runs.duckdb clean


@dataclass
class CaseResult:
    name: str
    killed: bool
    green_baseline: int          # how many generated tests passed on the correct code
    generated: int
    killer: str | None           # scenario id that caught the bug (or None)
    note: str = ""


# ── generation + swap-and-rerun mechanic ──────────────────────────────────────
def gen_request(target: str, selector: str | None, *, cut_source: bool, golden: bool,
                context_path: str | None) -> RunRequest:
    """A run_pipeline RunRequest for a detection generation, with only the ablation knobs varied.

    Public so reliability harnesses (e.g. `benchmark/baselines/cosmic_ray_holdout.py`) reuse the exact
    detection generation config instead of reaching into private internals. `no_run=True` (the kill
    checks drive the runs); feedback is driven via `cfg.feedback.enabled` on the detection config clone,
    not the request, so it stays off here unless explicitly enabled for the whole eval.
    """
    return RunRequest(target=target, selector=selector, no_run=True, golden=golden,
                      no_cut_source=not cut_source,
                      context=context_path, no_context=context_path is None)


def detection_config(cfg: AppConfig) -> AppConfig:
    """A config clone for detection: throwaway ledger + tuning off (no confounds)."""
    d = cfg.model_copy(deep=True)
    d.tuning.mode = "off"
    d.ledger.path = _DETECTION_LEDGER
    return d


def load_suite(report: RunReport) -> tuple[object, ScenarioSet, Path]:
    adapter = registry.get_adapter(report.adapter)
    scenario_set = ScenarioSet.model_validate_json(Path(report.scenarios_file).read_text(encoding="utf-8"))
    return adapter, scenario_set, Path(report.test_file)


def green_baseline(adapter, test_path: Path, scenario_set: ScenarioSet, project_root: Path,
                    per_test_timeout: float) -> set[str]:
    """Scenario ids that PASS on the correct source — only these are eligible to kill a mutant."""
    results = run_tests(adapter, test_path, scenario_set, cwd=project_root,
                        per_test_timeout=per_test_timeout)
    return {r.scenario_id for r in results if r.status == "passed"}


def _is_killed(adapter, test_path: Path, scenario_set: ScenarioSet, target_file: Path,
               correct_src: str, variant_src: str, project_root: Path, green: set[str],
               per_test_timeout: float) -> str | None:
    """Swap the buggy source in, re-run the suite, restore. Return the killing scenario id or None.

    A mutant is killed iff some GREEN-baseline test now fails/errors (standard mutation semantics —
    a test that was already red on the correct code can't be credited with catching the bug).
    """
    target_file.write_text(variant_src, encoding="utf-8")
    try:
        results = run_tests(adapter, test_path, scenario_set, cwd=project_root,
                            per_test_timeout=per_test_timeout)
    finally:
        target_file.write_text(correct_src, encoding="utf-8")    # ALWAYS restore the correct source
    return next((r.scenario_id for r in results
                 if r.scenario_id in green and r.status != "passed"), None)


_INPLACE_PREFIX = "_aitp_mut_"   # in-package temp target marker (for cleanup / crash recovery)


@contextmanager
def _inpackage_target(original: Path):
    """A uniquely-named temp copy placed in the ORIGINAL file's directory.

    Mutation-corpus targets live inside real projects and often import project-local packages
    (`from scripts import config`, `from commons import ...`). Copying them to an unrelated temp dir
    would mis-resolve those imports (golden probe + tests silently break), so the temp copy must sit
    in the same package directory. Auto-removed; the original is never touched. (QuixBugs programs are
    self-contained and use a plain temp dir instead.)

    The name is DETERMINISTIC (no uuid): the module name is embedded in the generation prompt
    (`Module: ...`), so a per-run-random name would change the P1 cache key every run and defeat the
    "re-runs are free" guarantee. Runs are sequential + single-user, so a stable name is safe.
    """
    tmp = original.parent / f"{_INPLACE_PREFIX}{original.stem}.py"
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def _sweep_stale_inpackage(targets: list[dict]) -> None:
    """Remove any leftover in-package temp targets from a previously crashed run (crash recovery)."""
    for d in {Path(t["abs_path"]).parent for t in targets}:
        for stale in d.glob(f"{_INPLACE_PREFIX}*.py"):
            stale.unlink(missing_ok=True)


def generate_or_skip(cfg: AppConfig, req: RunRequest) -> RunReport | None:
    from scripts.pipeline import run_pipeline                      # lazy: avoid import cycle
    try:
        return run_pipeline(cfg, req)
    except (TargetError, LLMError, BudgetError) as exc:
        logger.warning("Generation skipped for %s: %s", req.target, exc)
        return None


# ── QuixBugs corpus (external, default config) ─────────────────────────────────
def _run_quixbugs(cfg: AppConfig, project_root: Path, subset: int | None, *, clone: bool,
                  quixbugs_url: str, spend: dict) -> dict:
    from benchmark.corpora import quixbugs as qb

    cache_dir = project_root / "benchmark" / "corpora" / "_cache"
    repo = qb.ensure_corpus(cache_dir, url=quixbugs_url, clone=clone)
    if repo is None:
        return {"available": False, "cases": [], "n": 0, "killed": 0, "kill_rate": None}

    pairs = qb.load_pairs(repo, max_count=subset)
    det_cfg = detection_config(cfg)
    ptt = cfg.detection.per_test_timeout
    cases: list[CaseResult] = []
    for pair in pairs:
        correct_src = pair.correct_path.read_text(encoding="utf-8")
        buggy_src = pair.buggy_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="aitp_qb_") as td:
            tgt = Path(td) / f"{pair.name}.py"
            tgt.write_text(correct_src, encoding="utf-8")
            args = gen_request(str(tgt), pair.selector, context_path=None, **DEFAULT_VARIANT)
            rep = generate_or_skip(det_cfg, args)
            if rep is None:
                cases.append(CaseResult(pair.name, False, 0, 0, None, "generation failed"))
                continue
            spend["in"] += rep.tokens_in
            spend["out"] += rep.tokens_out
            adapter, scenario_set, test_path = load_suite(rep)
            green = green_baseline(adapter, test_path, scenario_set, project_root, ptt)
            if not green:
                cases.append(CaseResult(pair.name, False, 0, rep.generated, None,
                                        "no green baseline (suite failed on correct code)"))
                continue
            killer = _is_killed(adapter, test_path, scenario_set, tgt, correct_src, buggy_src,
                                project_root, green, ptt)
            cases.append(CaseResult(pair.name, killer is not None, len(green), rep.generated, killer))

    scored = [c for c in cases if c.green_baseline > 0]          # cases that produced a usable suite
    killed = sum(c.killed for c in cases)
    return {
        "available": True, "n": len(cases), "scored": len(scored), "killed": killed,
        "kill_rate": round(killed / len(cases), 3) if cases else None,
        "kill_rate_scored": round(killed / len(scored), 3) if scored else None,
        "cases": [c.__dict__ for c in cases],
    }


# ── Mutation corpus (in-repo, ablation) ────────────────────────────────────────
def _load_targets(manifest: Path, project_root: Path) -> list[dict]:
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    out: list[dict] = []
    for t in data.get("target", []):
        p = (project_root / t["path"]).resolve()
        if not p.is_file():
            logger.warning("Mutation target %s skipped: not found (%s).", t.get("name"), p)
            continue
        out.append({**t, "abs_path": str(p)})
    return out


def _run_mutation(cfg: AppConfig, project_root: Path, manifest: Path, *, ablation: bool,
                  max_mutants: int, seed: int, spend: dict) -> dict:
    from benchmark.mutation import generate_mutants

    targets = _load_targets(manifest, project_root)
    if not targets:
        return {"variants": {}, "targets": []}
    _sweep_stale_inpackage(targets)                  # clear any leftovers from a crashed prior run
    variants = ABLATION_VARIANTS if ablation else {"full": ABLATION_VARIANTS["full"]}
    det_cfg = detection_config(cfg)
    ptt = cfg.detection.per_test_timeout

    # Pre-compute mutants per target once (deterministic; identical across variants). Mutation is
    # SCOPED to the selected functions so every mutant is in code the generated suite targets.
    target_mutants: dict[str, list] = {}
    for t in targets:
        src = Path(t["abs_path"]).read_text(encoding="utf-8")
        sel = {s.strip() for s in t["selector"].split(",")} if t.get("selector") else None
        target_mutants[t["name"]] = generate_mutants(src, selector=sel, max_count=max_mutants, seed=seed)

    agg = {v: {"killed": 0, "mutants": 0, "unscored": 0, "equivalent": 0} for v in variants}
    per_target_rows: list[dict] = []
    for t in targets:
        src = Path(t["abs_path"]).read_text(encoding="utf-8")
        mutants = target_mutants[t["name"]]
        ctx = _nearest_context(Path(t["abs_path"]))     # project.md so the context feature can engage

        # 1 — per variant: generate (cached) + green baseline + the SET of mutant indices it kills.
        per_variant: dict[str, dict] = {}
        pool_by_fn: dict[str, list[dict]] = {}          # union fuzz pool for equivalence (variant-independent)
        for vname, flags in variants.items():
            with _inpackage_target(Path(t["abs_path"])) as tgt:
                tgt.write_text(src, encoding="utf-8")
                rep = generate_or_skip(det_cfg, gen_request(str(tgt), t.get("selector"), context_path=ctx, **flags))
                if rep is None:
                    per_variant[vname] = {"scored": False}
                    continue
                spend["in"] += rep.tokens_in
                spend["out"] += rep.tokens_out
                adapter, scenario_set, test_path = load_suite(rep)
                green = green_baseline(adapter, test_path, scenario_set, project_root, ptt)
                if not green:                            # no runnable suite (e.g. unconstructible args)
                    per_variant[vname] = {"scored": False}
                    continue
                killed_idx = {i for i, mut in enumerate(mutants)
                              if _is_killed(adapter, test_path, scenario_set, tgt, src, mut.source,
                                            project_root, green, ptt)}
                per_variant[vname] = {"scored": True, "green": len(green), "killed": killed_idx}
                for fn, samples in _equiv_samples(scenario_set, green).items():
                    pool_by_fn.setdefault(fn, []).extend(samples)

        # 2 — classify mutants ONCE per target (variant-independent): killed-by-ANY ⇒ distinct; the rest
        # get the differential fuzz check. So a weak suite can't mislabel a real bug as "equivalent".
        killed_by_any: set[int] = set()
        for pv in per_variant.values():
            killed_by_any |= pv.get("killed", set())
        equivalent_idx: set[int] = set()
        if cfg.detection.detect_equivalent and pool_by_fn and len(killed_by_any) < len(mutants):
            with _inpackage_target(Path(t["abs_path"])) as tgt:
                tgt.write_text(src, encoding="utf-8")
                for i, mut in enumerate(mutants):
                    if i not in killed_by_any and _is_equivalent_mutant(src, mut, pool_by_fn,
                                                                        tgt.parent, project_root):
                        equivalent_idx.add(i)
        denom = len(mutants) - len(equivalent_idx)       # same denominator for every variant

        # 3 — aggregate each variant against the shared denominator.
        for vname in variants:
            pv = per_variant[vname]
            if not pv["scored"]:
                agg[vname]["unscored"] += len(mutants)
                per_target_rows.append({"variant": vname, "target": t["name"], "killed": 0,
                                        "mutants": len(mutants), "green": 0, "note": "no green baseline"})
                continue
            killed = len(pv["killed"])                   # killed are distinct by definition
            agg[vname]["killed"] += killed
            agg[vname]["mutants"] += denom
            agg[vname]["equivalent"] += len(equivalent_idx)
            per_target_rows.append({"variant": vname, "target": t["name"], "killed": killed,
                                    "mutants": denom, "equivalent": len(equivalent_idx),
                                    "green": pv["green"]})

    variant_out = {v: {**a, "kill_rate": round(a["killed"] / a["mutants"], 3) if a["mutants"] else None}
                   for v, a in agg.items()}
    return {"variants": variant_out, "targets": per_target_rows}


def _equiv_samples(scenario_set, green: set[str]) -> dict[str, list[dict]]:
    """Per-function fuzzed input pool from the green scenarios with plain-literal inputs."""
    from benchmark import equivalence
    out: dict[str, list[dict]] = {}
    for s in scenario_set.scenarios:
        if s.id in green and s.inputs and all(equivalence._is_literal(v) for v in s.inputs.values()):
            pool = out.setdefault(s.unit, [])
            for fz in equivalence.fuzz_inputs(s.inputs):
                if len(pool) < 40:
                    pool.append(fz)
    return out


def _fn_of_line(src: str, lineno: int) -> str | None:
    import ast
    for n in ast.parse(src).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= lineno <= (n.end_lineno or n.lineno):
            return n.name
    return None


def _is_equivalent_mutant(src: str, mut, fn_samples: dict[str, list[dict]],
                          work_dir: Path, project_root: Path) -> bool:
    """A survived mutant is equivalent iff correct vs mutant agree on the fuzzed inputs of the
    function it mutated (differential test). Unknown / no samples → not equivalent (conservative)."""
    from benchmark import equivalence
    try:
        lineno = int(mut.description.split("@line ")[-1])
    except (ValueError, IndexError):
        return False
    fn = _fn_of_line(src, lineno)
    samples = fn_samples.get(fn) if fn else None
    if not samples:
        return False
    return equivalence.is_equivalent(src, mut.source, fn, samples,
                                     work_dir=work_dir, project_root=project_root) == "equivalent"


def _nearest_context(target: Path) -> str | None:
    """Find an agent/project.md or README near the ORIGINAL target (for the context feature)."""
    for parent in [target.parent, *target.parents]:
        for cand in (parent / "agent" / "project.md", parent / "README.md", parent / "project.md"):
            if cand.is_file():
                return str(cand)
        if (parent / ".git").exists() or (parent / "scripts").is_dir():
            break
    return None


# ── HumanEval holdout corpus (LOCKED — anti-overfit generalization number) ─────
# Held-out CODE the generator never sees during development (decided 2026-06-25, see
# design/IMPROVEMENT_APPROACHES.md). Reference solution = correct; bugs = our AST mutator (so the
# held-out axis is the CODE distribution, not the bug generator). Run ONCE at default config; the
# result is written to benchmark/eval/holdout/ ONLY — never to detection_baseline.json or
# EVAL_DETECTION.md (those are the dev series; contaminating them would defeat the holdout).
def _run_humaneval(cfg: AppConfig, project_root: Path, subset: int | None, *, clone: bool,
                   max_mutants: int, seed: int, humaneval_url: str, spend: dict) -> dict:
    from benchmark.corpora import humaneval as he
    from benchmark.mutation import generate_mutants

    cache_dir = project_root / "benchmark" / "corpora" / "_cache"
    data_file = he.ensure_corpus(cache_dir, url=humaneval_url, clone=clone)
    if data_file is None:
        return {"available": False, "cases": [], "problems": 0, "mutants": 0, "killed": 0,
                "kill_rate": None}

    problems = he.load_problems(data_file, max_count=subset, seed=seed)
    det_cfg = detection_config(cfg)
    ptt = cfg.detection.per_test_timeout
    cases: list[dict] = []
    tot_killed = tot_denom = tot_equiv = 0
    n_green = 0
    for prob in problems:
        correct_src = prob.source
        mutants = generate_mutants(correct_src, selector={prob.name}, max_count=max_mutants, seed=seed)
        if not mutants:
            cases.append({"task_id": prob.task_id, "name": prob.name, "green": 0,
                          "mutants": 0, "killed": 0, "equivalent": 0, "note": "no mutation sites"})
            continue
        with tempfile.TemporaryDirectory(prefix="aitp_he_") as td:
            tgt = Path(td) / f"he_{he._task_num(prob.task_id)}.py"
            tgt.write_text(correct_src, encoding="utf-8")
            # Default config (cut_source on, golden off) — the honest out-of-the-box generalization
            # number, mirroring the QuixBugs external corpus. feedback flows via det_cfg if enabled.
            args = gen_request(str(tgt), prob.selector, context_path=None, **DEFAULT_VARIANT)
            rep = generate_or_skip(det_cfg, args)
            if rep is None:
                cases.append({"task_id": prob.task_id, "name": prob.name, "green": 0,
                              "mutants": len(mutants), "killed": 0, "equivalent": 0,
                              "note": "generation failed"})
                continue
            spend["in"] += rep.tokens_in
            spend["out"] += rep.tokens_out
            adapter, scenario_set, test_path = load_suite(rep)
            green = green_baseline(adapter, test_path, scenario_set, project_root, ptt)
            if not green:
                cases.append({"task_id": prob.task_id, "name": prob.name, "green": 0,
                              "mutants": len(mutants), "killed": 0, "equivalent": 0,
                              "note": "no green baseline (suite failed on correct code)"})
                continue
            n_green += 1
            killed_idx = {i for i, mut in enumerate(mutants)
                          if _is_killed(adapter, test_path, scenario_set, tgt, correct_src,
                                        mut.source, project_root, green, ptt)}
            # Honest denominator: drop behaviourally-equivalent survivors (same machinery as the
            # mutation corpus). killed ⇒ provably distinct; only never-killed mutants are fuzz-checked.
            equivalent_idx: set[int] = set()
            if cfg.detection.detect_equivalent and len(killed_idx) < len(mutants):
                pool_by_fn: dict[str, list[dict]] = {}
                for fn, samples in _equiv_samples(scenario_set, green).items():
                    pool_by_fn.setdefault(fn, []).extend(samples)
                if pool_by_fn:
                    for i, mut in enumerate(mutants):
                        if i not in killed_idx and _is_equivalent_mutant(
                                correct_src, mut, pool_by_fn, Path(td), project_root):
                            equivalent_idx.add(i)
            denom = len(mutants) - len(equivalent_idx)
            killed = len(killed_idx)
            tot_killed += killed
            tot_denom += denom
            tot_equiv += len(equivalent_idx)
            cases.append({"task_id": prob.task_id, "name": prob.name, "green": len(green),
                          "mutants": denom, "killed": killed, "equivalent": len(equivalent_idx)})

    return {
        "available": True, "problems": len(problems),
        "problems_with_green": n_green,                  # construction-success rate (A3-relevant)
        "mutants": tot_denom, "killed": tot_killed, "equivalent": tot_equiv,
        "kill_rate": round(tot_killed / tot_denom, 3) if tot_denom else None,
        "cases": cases,
    }


def run_holdout(cfg: AppConfig, project_root: Path, *, subset: int | None = None,
                clone: bool = True, max_mutants: int = 6, seed: int = 0,
                humaneval_url: str | None = None, feedback: bool = False) -> dict:
    """The LOCKED holdout run — single generalization number on held-out HumanEval code.

    Writes to benchmark/eval/holdout/ ONLY. Deliberately does NOT read or write
    detection_baseline.json or EVAL_DETECTION.md (the dev series) — this corpus must never become a
    development target. There is no regression gate; the number stands on its own.
    """
    if feedback:                                          # optional: measure A1 generalization too
        cfg = cfg.model_copy(deep=True)
        cfg.feedback.enabled = True
    url = humaneval_url or "https://github.com/openai/human-eval.git"
    spend = {"in": 0, "out": 0}
    he = _run_humaneval(cfg, project_root, subset, clone=clone, max_mutants=max_mutants,
                        seed=seed, humaneval_url=url, spend=spend)

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "corpus": "humaneval",
        "locked": True,                                   # marker: not gated, not a dev baseline
        "feedback": feedback,
        "model": resolve_model(None),
        "kill_rate": he.get("kill_rate"),
        "humaneval": he,
        "tokens": {"in": spend["in"], "out": spend["out"]},
    }

    holdout_dir = project_root / "benchmark" / "eval" / "holdout"
    holdout_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (holdout_dir / f"holdout_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_json"] = str(holdout_dir / f"holdout_{stamp}.json")
    _write_holdout_md(result, holdout_dir / f"holdout_{stamp}.md")
    return result


def _write_holdout_md(result: dict, path: Path) -> None:
    from benchmark.stats import fmt_rate_ci
    he = result["humaneval"]
    lines = [f"# HumanEval holdout (LOCKED) — {result['ts']}", "",
             "> Held-out code the generator never saw during development. Single generalization run; "
             "**not** gated and **not** part of the dev baseline series. Do not develop against it.", "",
             "> **What this number is (and isn't):** a **kill rate over our lightweight, capped AST "
             "mutation operators** (`benchmark/mutation.py`) on held-out code — a *generalization "
             "check*, NOT a PIT-style mutation score. HumanEval is an easier, well-documented "
             "distribution, so the figure is high; a standard-tool (cosmic-ray, richer operator set) "
             "comparable on the same targets is in `benchmark/eval/holdout/cosmic/`.", "",
             f"- **Model:** {result['model'] or '(default)'}",
             f"- **Feedback (A1):** {'on' if result['feedback'] else 'off'}",
             f"- **Tokens:** {result['tokens']['in']}+{result['tokens']['out']}", ""]
    if not he.get("available"):
        lines += ["**Corpus unavailable** (clone failed or `--no-clone` with no cache).", ""]
    else:
        lines += [f"- **Kill rate (fast operators): {fmt_rate_ci(he['killed'], he['mutants'])}** "
                  f"(95% Wilson CI; {he['killed']}/{he['mutants']} non-equivalent mutants killed)",
                  f"- Problems: **{he['problems']}** · with runnable suite (green baseline): "
                  f"**{he['problems_with_green']}** · equivalent mutants excluded: {he['equivalent']}", "",
                  "| task | fn | killed | mutants | green | note |", "|---|---|---|---|---|---|"]
        for c in he["cases"]:
            lines.append(f"| {c['task_id']} | {c['name']} | {c['killed']} | {c['mutants']} | "
                         f"{c['green']} | {c.get('note', '')} |")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── regression gate (reuse commons kernel; all kill-rate metrics are higher-better) ──
def _panel_regressions(baseline: dict, current: dict, *, tol: float) -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))   # repo root → commons
    from commons.evals.regression import metric_regressions
    common = {k for k in baseline if k in current and baseline[k] is not None and current[k] is not None}
    return metric_regressions({k: baseline[k] for k in common},
                              {k: current[k] for k in common}, min_delta=tol)


def _panel(quix: dict, mut: dict) -> dict:
    """The gated headline metrics (higher is better)."""
    panel: dict[str, float | None] = {}
    if quix.get("available"):
        panel["quixbugs_kill_rate"] = quix.get("kill_rate")
    for vname, v in mut.get("variants", {}).items():
        panel[f"mutation_kill_rate_{vname}"] = v.get("kill_rate")
    return {k: v for k, v in panel.items() if v is not None}


# ── orchestration ──────────────────────────────────────────────────────────────
def run_detection(cfg: AppConfig, project_root: Path, *, manifest: Path | None = None,
                  subset: int | None = None, corpus: str = "both", ablation: bool = True,
                  update_baseline: bool = False, tol: float = 0.0, clone: bool = True,
                  max_mutants: int = 6, seed: int = 0, quixbugs_url: str | None = None,
                  feedback: bool = False) -> dict:
    # Approach 1: toggle the coverage-feedback loop ON for every generation in this eval (the
    # det_cfg clones below inherit it) — the before/after experiment is two `detect` runs.
    if feedback:
        cfg = cfg.model_copy(deep=True)
        cfg.feedback.enabled = True
    manifest = manifest or project_root / "benchmark" / "detection_targets.toml"
    baseline_path = project_root / "benchmark" / "detection_baseline.json"
    out_dir = project_root / "benchmark" / "eval"
    runs_dir = out_dir / "runs"
    url = quixbugs_url or "https://github.com/jkoppel/QuixBugs.git"

    spend = {"in": 0, "out": 0}
    quix = {"available": False}
    if corpus in ("both", "quixbugs"):
        quix = _run_quixbugs(cfg, project_root, subset, clone=clone, quixbugs_url=url, spend=spend)
    mut = {"variants": {}, "targets": []}
    if corpus in ("both", "mutation"):
        mut = _run_mutation(cfg, project_root, manifest, ablation=ablation,
                            max_mutants=max_mutants, seed=seed, spend=spend)

    panel = _panel(quix, mut)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    comparison = _panel_regressions(baseline["panel"], panel, tol=tol) if baseline else None
    gate_pass = not (comparison and comparison["regressed"])

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": resolve_model(None),
        "panel": panel,
        "quixbugs": quix,
        "mutation": mut,
        "ablation_deltas": _ablation_deltas(mut),
        "tokens": {"in": spend["in"], "out": spend["out"]},
        "baseline_compared": baseline is not None,
        "comparison": comparison,
        "gate_pass": gate_pass,
    }

    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (runs_dir / f"detection_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_json"] = str(runs_dir / f"detection_{stamp}.json")
    _write_md(result, runs_dir / f"detection_{stamp}.md")
    _append_ledger(result, out_dir / "EVAL_DETECTION.md")

    if update_baseline:
        baseline_path.write_text(json.dumps({"ts": result["ts"], "model": result["model"],
                                             "panel": panel}, indent=2), encoding="utf-8")
        result["baseline_updated"] = True
        logger.info("Detection baseline updated -> %s", baseline_path)
    return result


def _ablation_deltas(mut: dict) -> dict:
    """Marginal kill-rate contribution of each feature (full minus leave-one-out)."""
    v = {name: d.get("kill_rate") for name, d in mut.get("variants", {}).items()}
    out: dict[str, float] = {}
    if v.get("full") is not None:
        if v.get("cut_source") is not None:      # full vs (cut_source only) isolates golden
            out["golden_contributes"] = round(v["full"] - v["cut_source"], 3)
        if v.get("golden") is not None:          # full vs (golden only) isolates cut_source
            out["cut_source_contributes"] = round(v["full"] - v["golden"], 3)
        if v.get("naive") is not None:
            out["full_over_naive"] = round(v["full"] - v["naive"], 3)
    return out


def _write_md(result: dict, path: Path) -> None:
    q = result["quixbugs"]
    lines = [f"# Bug-detection eval — {result['ts']}", "",
             f"- **Model:** {result['model'] or '(default)'}",
             f"- **Gate:** {'PASS' if result['gate_pass'] else 'REGRESSION'}"
             f"{' (no baseline)' if not result['baseline_compared'] else ''}",
             f"- **Tokens:** {result['tokens']['in']}+{result['tokens']['out']}", ""]
    if q.get("available"):
        lines += ["## QuixBugs (external, default config)", "",
                  f"- Programs: **{q['n']}** · scored: {q['scored']} · "
                  f"killed: **{q['killed']}** · kill rate: **{q['kill_rate']}** "
                  f"(over scored: {q.get('kill_rate_scored')})", "",
                  "| program | killed | green | killer |", "|---|---|---|---|"]
        for c in q["cases"]:
            lines.append(f"| {c['name']} | {'✓' if c['killed'] else '·'} | "
                         f"{c['green_baseline']} | {c['killer'] or c['note'] or ''} |")
        lines.append("")
    if result["mutation"].get("variants"):
        lines += ["## Mutation corpus (in-repo) — ablation", "",
                  "| variant | killed | mutants | kill rate |", "|---|---|---|---|"]
        for name, d in result["mutation"]["variants"].items():
            lines.append(f"| {name} | {d['killed']} | {d['mutants']} | **{d['kill_rate']}** |")
        lines += ["", "### Feature contribution (Δ kill rate)", ""]
        for k, dv in result["ablation_deltas"].items():
            lines.append(f"- **{k}:** {dv:+.3f}")
        lines.append("")
    if result["comparison"] and result["comparison"]["regressed"]:
        lines += [f"**REGRESSED:** {', '.join(result['comparison']['regressed'])}", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_ledger(result: dict, path: Path) -> None:
    """One row per run — the trackable time series (run it a couple of times, compare rows)."""
    q = result["quixbugs"]
    mv = result["mutation"].get("variants", {})
    header = ("| ts | model | quixbugs_kill | mut_naive | mut_full | full_over_naive | gate |\n"
              "|---|---|---|---|---|---|---|\n")
    if not path.is_file():
        path.write_text("# Bug-detection eval — tracked runs\n\n"
                        "Kill rate = fraction of bugs caught (a test green on correct code fails on "
                        "the buggy version). Higher is better. Re-run: `python scripts/main.py detect`.\n\n"
                        + header, encoding="utf-8")
    row = (f"| {result['ts']} | {result['model'] or '(default)'} "
           f"| {q.get('kill_rate') if q.get('available') else '—'} "
           f"| {mv.get('naive', {}).get('kill_rate', '—')} "
           f"| {mv.get('full', {}).get('kill_rate', '—')} "
           f"| {result['ablation_deltas'].get('full_over_naive', '—')} "
           f"| {'PASS' if result['gate_pass'] else 'REGRESSION'} |\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(row)
