"""P5 — Quality regression gate. The "are we done?" signal (DoD #1 + #5).

Runs a small CURATED, KNOWN-GOOD target set (`benchmark/quality_targets.toml`) through the pipeline
and measures a multi-metric panel — coverage, pass-rate, **false-positive rate** (a `failed` test on
known-good code is a false positive by construction — H3), error/compile rate, **test-smell density**
([Smells], H4), and the ledger acceptance trend — then flags any regression vs a stored baseline and
exits non-zero (CI-grade). Re-runnable on a model change (the model id is recorded); cost is bounded
by the P1 scenario cache (an unchanged target+prompt+model replays free).

Deterministic except the (cached) generation step. Coverage reuses `benchmark/cov.py`; the baseline
gate reuses `commons/evals/regression.py`.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from scripts.config import AppConfig
from scripts.core import ledger as ledger_mod
from scripts.llm_client import resolve_model
from scripts.logger import get_logger

logger = get_logger("quality")

# Panel metric directions. A regression is a DROP for higher-is-better, a RISE for lower-is-better
# (handled by negating the latter before the shared drop-detector).
_HIGHER_BETTER = {"coverage", "pass_rate", "acceptance"}
_LOWER_BETTER = {"fp_rate", "error_rate", "smell_density"}


# ── target manifest ───────────────────────────────────────────────────────────
def load_targets(manifest: Path, project_root: Path) -> list[dict]:
    """Parse the curated manifest; resolve paths against the root; drop (with a note) missing ones."""
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    out: list[dict] = []
    for t in data.get("target", []):
        p = (project_root / t["path"]).resolve()
        if not p.is_file():
            logger.warning("Quality target %s skipped: path not found (%s).", t.get("name"), p)
            continue
        out.append({**t, "abs_path": str(p)})
    return out


# ── test-smell density (deterministic, ast-only) ──────────────────────────────
# Smells chosen to NOT penalize the tool's intended behaviour: exact-value assertions are the GOAL
# (P3a), so [Smells]' "Magic Number Test" is deliberately excluded — it would flag `result == 50`.
def _is_trivial_assert(a: ast.Assert) -> bool:
    t = a.test
    if isinstance(t, ast.Constant):                       # assert True / assert 1
        return True
    if isinstance(t, ast.Name):                           # assert result (bare truthiness)
        return True
    if (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.IsNot)
            and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value is None):
        return True                                       # assert x is not None
    return False


def smell_report(test_file: Path) -> dict:
    """{tests, assertion_roulette, empty_or_trivial, duplicate_assert, smells, density}."""
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {"tests": 0, "assertion_roulette": 0, "empty_or_trivial": 0,
                "duplicate_assert": 0, "smells": 0, "density": 0.0}
    tests = roulette = trivial = dup = 0
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("test")):
            continue
        tests += 1
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        if len(asserts) >= 2 and any(a.msg is None for a in asserts):
            roulette += 1                                 # Assertion Roulette: multiple unlabeled asserts
        if not asserts or (len(asserts) == 1 and _is_trivial_assert(asserts[0])):
            trivial += 1                                  # empty / vacuous test
        exprs = [ast.dump(a.test) for a in asserts]
        if len(exprs) != len(set(exprs)):
            dup += 1                                       # the same assertion repeated
    smells = roulette + trivial + dup
    return {"tests": tests, "assertion_roulette": roulette, "empty_or_trivial": trivial,
            "duplicate_assert": dup, "smells": smells,
            "density": round(smells / tests, 3) if tests else 0.0}


# ── coverage (reuse benchmark/cov.py) ─────────────────────────────────────────
def _coverage(test_file: str, project_root: Path, grep: str, cover_dir: Path) -> float | None:
    """Mean line-coverage % of the target source via the stdlib-trace harness; None on failure.

    The cover dir is WIPED first: stdlib `trace` MERGES into existing `.cover` files, so a reused
    dir makes coverage creep upward run-to-run (a flaky metric → false regressions). A clean dir per
    measurement keeps it deterministic.
    """
    shutil.rmtree(cover_dir, ignore_errors=True)
    cov = project_root / "benchmark" / "cov.py"
    cmd = [sys.executable, str(cov), "--test", test_file, "--cwd", str(project_root),
           "--grep", grep, "--cover", str(cover_dir)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root), timeout=180)
    except subprocess.TimeoutExpired:
        logger.warning("Coverage run timed out for grep=%s.", grep)
        return None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("COVJSON:"):
            files = json.loads(line[len("COVJSON:"):]).get("files", {})
            pcts = [f["pct"] for f in files.values()]
            return round(sum(pcts) / len(pcts), 1) if pcts else None
    logger.warning("No COVJSON from coverage run (grep=%s).", grep)
    return None


# ── panel regression gate (reuse commons kernel) ──────────────────────────────
def panel_regressions(baseline: dict, current: dict, *, tol: float = 0.0) -> dict:
    """Per-metric regressions vs baseline. Lower-is-better metrics are negated so the shared
    drop-detector (`metric_regressions`) catches a harmful RISE as a drop."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # repo root → commons
    from commons.evals.regression import metric_regressions

    def _orient(panel: dict) -> dict[str, float]:
        out = {}
        for k, v in panel.items():
            if v is None:
                continue
            out[k] = -v if k in _LOWER_BETTER else v
        return out

    common = set(_orient(baseline)) & set(_orient(current))
    before = {k: _orient(baseline)[k] for k in common}
    after = {k: _orient(current)[k] for k in common}
    return metric_regressions(before, after, min_delta=tol)


# ── orchestration ─────────────────────────────────────────────────────────────
def _ns(target: dict, cfg: AppConfig) -> argparse.Namespace:
    """A complete run_pipeline args namespace for a curated target (cache ON → cheap re-runs)."""
    return argparse.Namespace(
        target=target["abs_path"], adapter=None, selector=target.get("selector"),
        count=None, model=None, prompt_version=None, no_run=False,
        fixtures=False, fixture_domain=None, fixture_entity=None, fixture_rows=None,
        context=None, no_context=False, golden=False, serve=False, web_async=False,
        no_cache=False, refresh_cache=False, no_cut_source=False,
    )


def run_quality(cfg: AppConfig, project_root: Path, *, manifest: Path | None = None,
                update_baseline: bool = False, tol: float = 0.0) -> dict:
    from scripts.main import run_pipeline                  # lazy: avoid import cycle

    manifest = manifest or project_root / "benchmark" / "quality_targets.toml"
    baseline_path = project_root / "benchmark" / "quality_baseline.json"
    ledger_path = project_root / cfg.ledger.path
    out_dir = project_root / cfg.run.output_dir / "quality"
    cover_dir = out_dir / "_cover"
    targets = load_targets(manifest, project_root)
    if not targets:
        raise SystemExit(f"No usable quality targets in {manifest}.")

    per_target: list[dict] = []
    tot = {"generated": 0, "passed": 0, "failed": 0, "errored": 0, "smells": 0, "tests": 0}
    covs: list[float] = []
    accepts: list[float] = []
    for t in targets:
        rep = run_pipeline(cfg, _ns(t, cfg))
        sm = smell_report(Path(rep.test_file))
        cov = _coverage(rep.test_file, project_root, t["grep"], cover_dir)
        acc = ledger_mod.target_acceptance(rep.adapter, t["abs_path"], ledger_path)
        per_target.append({
            "name": t["name"], "target": t["path"],
            "generated": rep.generated, "passed": rep.passed, "failed": rep.failed,
            "errored": rep.errored, "coverage": cov, "smell": sm,
            "acceptance": acc[0] if acc else None,
        })
        for k in ("generated", "passed", "failed", "errored"):
            tot[k] += getattr(rep, k)
        tot["smells"] += sm["smells"]
        tot["tests"] += sm["tests"]
        if cov is not None:
            covs.append(cov)
        if acc:
            accepts.append(acc[0])

    g = tot["generated"] or 1
    panel = {
        "coverage": round(sum(covs) / len(covs), 1) if covs else None,
        "pass_rate": round(tot["passed"] / g, 3),
        "fp_rate": round(tot["failed"] / g, 3),
        "error_rate": round(tot["errored"] / g, 3),
        "smell_density": round(tot["smells"] / tot["tests"], 3) if tot["tests"] else 0.0,
        "acceptance": round(sum(accepts) / len(accepts), 3) if accepts else None,
    }

    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    comparison = panel_regressions(baseline["panel"], panel, tol=tol) if baseline else None
    gate_pass = not (comparison and comparison["regressed"])

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": resolve_model(None), "panel": panel, "targets": per_target,
        "baseline_compared": baseline is not None, "comparison": comparison, "gate_pass": gate_pass,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"quality_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_file"] = str(out_dir / f"quality_{stamp}.json")
    _write_md(result, out_dir / f"quality_{stamp}.md")
    result["report_md"] = str(out_dir / f"quality_{stamp}.md")

    if update_baseline:
        baseline_path.write_text(json.dumps({"ts": result["ts"], "model": result["model"],
                                             "panel": panel}, indent=2), encoding="utf-8")
        logger.info("Quality baseline updated -> %s", baseline_path)
        result["baseline_updated"] = True
    return result


def _write_md(result: dict, path: Path) -> None:
    p = result["panel"]
    lines = [f"# Quality gate — {result['ts']}", "",
             f"- **Model:** {result['model'] or '(default)'}",
             f"- **Gate:** {'PASS' if result['gate_pass'] else 'REGRESSION'}"
             f"{' (no baseline)' if not result['baseline_compared'] else ''}", "",
             "## Panel", "", "| metric | value |", "|---|---|"]
    for k in ("coverage", "pass_rate", "fp_rate", "error_rate", "smell_density", "acceptance"):
        lines.append(f"| {k} | {p[k]} |")
    if result["comparison"] and result["comparison"]["regressed"]:
        lines += ["", f"**REGRESSED:** {', '.join(result['comparison']['regressed'])}"]
    lines += ["", "## Per target", "", "| target | gen | pass | fail | err | cov% | smells |",
              "|---|---|---|---|---|---|---|"]
    for t in result["targets"]:
        lines.append(f"| {t['name']} | {t['generated']} | {t['passed']} | {t['failed']} | "
                     f"{t['errored']} | {t['coverage']} | {t['smell']['smells']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
