"""A/M1(b) — tool-comparable mutation score on the HumanEval holdout via cosmic-ray.

The locked holdout (`detection.run_holdout`) reports a kill rate over our OWN lightweight, capped AST
mutation operators (`benchmark/mutation.py`) — 0.98 on subset-50. That figure is **not** comparable to
a PIT-style mutation score from a standard tool with a richer operator set, so it must not be published
as "a mutation score". This harness re-measures the SAME held-out targets with a STANDARD mutation tool
(cosmic-ray, `core/*` operator set) using OUR generated suites (green subset) as the test set, yielding
a tool-comparable number to publish alongside (and the fast-operator 0.98 stays as a secondary figure).

Method, per HumanEval problem (reference solution = correct code):
  1. Reconstruct ``he_<num>.py`` with the SAME module stem the holdout used, so the P1 scenario cache
     replays our generation for free (no gateway spend when the cache is intact).
  2. Generate our suite via the pipeline at the holdout config (``detection.detection_config`` + the default
     variant: cut_source on, golden off).
  3. Keep only GREEN tests (those that pass on the correct code) — a test that is red on correct code
     would make every mutant look "killed" (cosmic-ray's kill signal is just a non-zero test exit).
  4. cosmic-ray ``init`` -> ``baseline`` -> ``exec``, mutating ``he_<num>.py`` with the core operator
     set; the test-command runs only the green node-ids (``-x``, fail fast).
  5. Parse the dump: a mutant is *killed* iff ``test_outcome == "killed"``; ``incompetent`` workers are
     excluded from the denominator. Report BOTH a function-scoped kill rate (mutants whose
     ``definition_name`` is the entry point — apples-to-apples with the 0.98) and a whole-module rate.

Writes ONLY to ``benchmark/eval/holdout/cosmic/`` — never the dev series, ``detection_baseline.json``,
or ``EVAL_DETECTION.md``. **Standard-tool caveat:** cosmic-ray does not exclude equivalent mutants, so
this is a conservative (lower-bound) figure relative to our equivalent-excluded 0.98.

Run (from the project root)::

    python -m benchmark.baselines.cosmic_ray_holdout --subset 20
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Allow `python benchmark/baselines/cosmic_ray_holdout.py` as well as `-m` (project root on path).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmark.corpora import humaneval as he  # noqa: E402
from benchmark.stats import fmt_rate_ci  # noqa: E402
from scripts.config import load_config  # noqa: E402
from scripts.core import detection  # noqa: E402  (reuse the holdout generation mechanic)
from scripts.llm_client import resolve_model  # noqa: E402
from scripts.logger import get_logger  # noqa: E402

logger = get_logger("cosmic_ray_holdout")

# cosmic-ray is driven via `python -m` so it works without its console scripts being on PATH.
_CR_CLI = [sys.executable, "-m", "cosmic_ray.cli"]


def _green_node_ids(adapter, scenario_set, green: set[str], test_file_name: str) -> list[str]:
    """pytest node-ids (``file::test_fn``) for the green scenarios only."""
    return [f"{test_file_name}::{adapter.test_function_name(s)}"
            for s in scenario_set.scenarios if s.id in green]


def _run_cosmic_ray(work: Path, module_name: str, node_ids: list[str], *, timeout: float) -> dict:
    """init -> baseline -> exec -> dump for one target; return per-mutant outcome records.

    Returns ``{"ok": bool, "note": str, "records": [{outcome, definition, operator}, ...]}``.
    """
    cfg_path = work / "cr.toml"
    session = work / "session.sqlite"
    test_cmd = " ".join([_q(sys.executable), "-m", "pytest", "-x", "-q", "--no-header", *map(_q, node_ids)])
    cfg_path.write_text(
        "[cosmic-ray]\n"
        f'module-path = "{module_name}"\n'
        f"timeout = {float(timeout)}\n"
        "excluded-modules = []\n"
        f'test-command = "{test_cmd}"\n\n'
        "[cosmic-ray.distributor]\n"
        'name = "local"\n',
        encoding="utf-8",
    )
    # init (enumerate mutation sites; libcst-based, no import of the target needed)
    init = _cr(["init", "cr.toml", "session.sqlite"], work)
    if init.returncode != 0:
        return {"ok": False, "note": f"init failed: {_tail(init)}", "records": []}
    # baseline: the green suite MUST pass on unmutated code, else every mutant is a false kill
    base = _cr(["baseline", "cr.toml"], work)
    if base.returncode != 0:
        return {"ok": False, "note": f"baseline failed (green suite not green?): {_tail(base)}",
                "records": []}
    # exec: run every mutant
    ex = _cr(["exec", "cr.toml", "session.sqlite"], work)
    if ex.returncode != 0:
        return {"ok": False, "note": f"exec failed: {_tail(ex)}", "records": []}
    dump = _cr(["dump", "session.sqlite"], work)
    records = _parse_dump(dump.stdout)
    if not records:
        return {"ok": False, "note": "no mutation records (no sites?)", "records": []}
    return {"ok": True, "note": "", "records": records}


def _parse_dump(text: str) -> list[dict]:
    """Flatten cosmic-ray's `dump` (one ``[work_item, result]`` JSON list per line)."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        work_item = rec[0]
        result = rec[1] if len(rec) > 1 else None
        muts = work_item.get("mutations") or [{}]
        out.append({
            "operator": muts[0].get("operator_name"),
            "definition": muts[0].get("definition_name"),
            "outcome": (result or {}).get("test_outcome"),       # killed | survived | incompetent
            "worker": (result or {}).get("worker_outcome"),
        })
    return out


def _score(records: list[dict], entry: str) -> dict:
    """Kill rate over (a) the entry function only and (b) the whole module. `incompetent` excluded."""
    def rate(recs: list[dict]) -> dict:
        scored = [r for r in recs if r["outcome"] in ("killed", "survived")]
        killed = sum(r["outcome"] == "killed" for r in scored)
        incompetent = sum(r["outcome"] == "incompetent" for r in recs)
        return {"killed": killed, "mutants": len(scored), "incompetent": incompetent,
                "kill_rate": round(killed / len(scored), 3) if scored else None}
    fn = rate([r for r in records if r["definition"] == entry])
    mod = rate(records)
    return {"function": fn, "module": mod}


def _cr(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(_CR_CLI + args, cwd=str(cwd), capture_output=True, text=True)


def _q(s: str) -> str:
    """Quote a token for the cosmic-ray TOML test-command (shlex-split, forward-slashes safe)."""
    s = s.replace("\\", "/")
    return f'"{s}"' if " " in s else s


def _tail(proc: subprocess.CompletedProcess, n: int = 300) -> str:
    return ((proc.stderr or proc.stdout or "").strip())[-n:]


def _measure_one(prob, det_cfg, ptt: float, work_root: Path, *, timeout: float, keep: bool) -> dict:
    """Measure ONE held-out target → its case dict (with per-target operator counts). Self-contained
    so a resumable run can checkpoint it the moment it completes."""
    num = he._task_num(prob.task_id)
    work = work_root / f"he_{num}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    module_name = f"he_{num}.py"                            # SAME stem as the holdout -> cache hit
    (work / module_name).write_text(prob.source, encoding="utf-8")
    try:
        # 1 — generate our suite (cached: 0 tokens when the holdout cache is intact).
        req = detection.gen_request(str(work / module_name), prob.selector,
                                    context_path=None, **detection.DEFAULT_VARIANT)
        rep = detection.generate_or_skip(det_cfg, req)
        if rep is None:
            return _skip(prob, "generation failed")
        adapter, scenario_set, test_path = detection.load_suite(rep)

        # 2 — green subset (tests that pass on correct code).
        green = detection.green_baseline(adapter, test_path, scenario_set, _PROJECT_ROOT, ptt)
        if not green:
            return _skip(prob, "no green baseline (suite failed on correct code)")
        test_file = work / f"test_he_{num}.py"
        shutil.copyfile(test_path, test_file)              # sys.path bootstrap is absolute -> import ok
        node_ids = _green_node_ids(adapter, scenario_set, green, test_file.name)

        # 3 — cosmic-ray (standard operator set).
        cr = _run_cosmic_ray(work, module_name, node_ids, timeout=timeout)
        if not cr["ok"]:
            return _skip(prob, cr["note"], green=len(green))
        ops: dict[str, int] = {}
        for r in cr["records"]:
            if r["definition"] == prob.name and r["operator"]:
                ops[r["operator"]] = ops.get(r["operator"], 0) + 1
        sc = _score(cr["records"], prob.name)
        logger.info("%s %s — fn kill %s (%d/%d) · module kill %s",
                    prob.task_id, prob.name, sc["function"]["kill_rate"],
                    sc["function"]["killed"], sc["function"]["mutants"], sc["module"]["kill_rate"])
        return {"task_id": prob.task_id, "name": prob.name, "green": len(green),
                "function": sc["function"], "module": sc["module"], "ops": ops, "note": ""}
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _load_checkpoint(path: Path) -> dict[str, dict]:
    """task_id -> case for every target already completed (resume across interruptions)."""
    out: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[c["task_id"]] = c
    return out


def _append_checkpoint(path: Path, case: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(case) + "\n")
        fh.flush()


def run(subset: int | None, *, clone: bool, timeout: float, keep: bool,
        max_targets: int | None, fresh: bool = False) -> dict:
    cfg = load_config(None)
    det_cfg = detection.detection_config(cfg)
    ptt = cfg.detection.per_test_timeout

    cache_dir = _PROJECT_ROOT / "benchmark" / "corpora" / "_cache"
    data_file = he.ensure_corpus(cache_dir, url=cfg.detection.humaneval_url, clone=clone)
    if data_file is None:
        return {"available": False, "note": "HumanEval corpus unavailable"}
    problems = he.load_problems(data_file, max_count=subset)
    if max_targets:
        problems = problems[:max_targets]

    out_dir = _PROJECT_ROOT / "benchmark" / "eval" / "holdout" / "cosmic"
    work_root = out_dir / "work"
    work_root.mkdir(parents=True, exist_ok=True)

    # RESUMABLE: each target is checkpointed the moment it completes, so an interrupted run (these are
    # long; the per-mutant cosmic exec is NOT cached) resumes where it stopped instead of restarting.
    ckpt = out_dir / "_checkpoint.jsonl"
    if fresh:
        ckpt.unlink(missing_ok=True)
    done = _load_checkpoint(ckpt)
    if done:
        logger.info("Resuming: %d/%d target(s) already checkpointed (%s).",
                    len(done), len(problems), ckpt.name)

    cases: list[dict] = []
    for prob in problems:
        if prob.task_id in done:
            cases.append(done[prob.task_id])
            continue
        case = _measure_one(prob, det_cfg, ptt, work_root, timeout=timeout, keep=keep)
        _append_checkpoint(ckpt, case)
        cases.append(case)

    result = _aggregate(cases, subset)
    _write_artifacts(result, out_dir)
    ckpt.unlink(missing_ok=True)                            # full run complete -> clear the checkpoint
    return result


def _skip(prob, note: str, *, green: int = 0) -> dict:
    return {"task_id": prob.task_id, "name": prob.name, "green": green,
            "function": {"killed": 0, "mutants": 0, "incompetent": 0, "kill_rate": None},
            "module": {"killed": 0, "mutants": 0, "incompetent": 0, "kill_rate": None},
            "ops": {}, "note": note}


def _aggregate(cases: list[dict], subset: int | None) -> dict:
    op_counter: Counter[str] = Counter()
    for c in cases:
        for op, n in c.get("ops", {}).items():
            op_counter[op] += n
    def totals(scope: str) -> dict:
        killed = sum(c[scope]["killed"] for c in cases)
        mutants = sum(c[scope]["mutants"] for c in cases)
        incompetent = sum(c[scope]["incompetent"] for c in cases)
        return {"killed": killed, "mutants": mutants, "incompetent": incompetent,
                "kill_rate": round(killed / mutants, 3) if mutants else None}
    scored = [c for c in cases if c["function"]["mutants"] > 0]
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": "cosmic-ray",
        "tool_version": _cr_version(),
        "model": resolve_model(None),
        "corpus": "humaneval",
        "subset": subset,
        "problems": len(cases),
        "problems_scored": len(scored),
        "function_scope": totals("function"),
        "module_scope": totals("module"),
        "operators": dict(op_counter.most_common()),
        "cases": cases,
    }


def _cr_version() -> str:
    try:
        import cosmic_ray
        return getattr(cosmic_ray, "__version__", "unknown")
    except Exception:
        return "unknown"


def _write_artifacts(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"cosmic_ray_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_json"] = str(out_dir / f"cosmic_ray_{stamp}.json")
    _write_md(result, out_dir / f"cosmic_ray_{stamp}.md")


def _write_md(result: dict, path: Path) -> None:
    fn, mod = result["function_scope"], result["module_scope"]
    lines = [
        f"# Tool-comparable mutation score (cosmic-ray) — HumanEval holdout — {result['ts']}", "",
        "> Standard mutation tool (**cosmic-ray**, `core/*` operators) over OUR generated suites "
        "(green subset) on the SAME held-out HumanEval targets as the locked holdout. This is the "
        "tool-comparable figure to publish; the fast-operator holdout number (0.98) is secondary.", "",
        f"- **Tool:** cosmic-ray {result['tool_version']} · **Model (suite gen):** {result['model']}",
        f"- **Subset:** {result['subset']} · problems scored: "
        f"{result['problems_scored']}/{result['problems']}",
        f"- **Function-scoped kill rate: {fmt_rate_ci(fn['killed'], fn['mutants'])}** "
        f"(95% Wilson CI; {fn['incompetent']} incompetent excluded) — apples-to-apples with the 0.98",
        f"- Whole-module kill rate: {fmt_rate_ci(mod['killed'], mod['mutants'])}", "",
        "**Caveat:** cosmic-ray does not exclude equivalent mutants, so this is a conservative "
        "(lower-bound) figure vs the equivalent-excluded fast-operator number.", "",
        "## Operator set exercised (entry function)", "",
        "| operator | count |", "|---|---|",
    ]
    for op, n in result["operators"].items():
        lines.append(f"| {op} | {n} |")
    lines += ["", "## Per-problem", "",
              "| task | fn | green | fn killed | fn mutants | fn kill | module kill | note |",
              "|---|---|---|---|---|---|---|---|"]
    for c in result["cases"]:
        lines.append(f"| {c['task_id']} | {c['name']} | {c['green']} | {c['function']['killed']} | "
                     f"{c['function']['mutants']} | {c['function']['kill_rate']} | "
                     f"{c['module']['kill_rate']} | {c.get('note', '')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cosmic_ray_holdout")
    ap.add_argument("--subset", type=int, default=20,
                    help="max HumanEval problems (prefix subset; default 20)")
    ap.add_argument("--max-targets", type=int,
                    help="cap targets actually run (for a quick harness smoke; subset still bounds the prefix)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="cosmic-ray per-mutant test timeout in seconds (default 30)")
    ap.add_argument("--no-clone", action="store_true", help="don't clone HumanEval if absent")
    ap.add_argument("--keep", action="store_true", help="keep per-target work dirs (debugging)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore + clear any checkpoint and start over (default: resume where it stopped)")
    a = ap.parse_args(argv)

    result = run(a.subset, clone=not a.no_clone, timeout=a.timeout, keep=a.keep,
                 max_targets=a.max_targets, fresh=a.fresh)
    if not result.get("available", True):
        print(f"\n{result['note']}")
        return 1
    fn, mod = result["function_scope"], result["module_scope"]
    print(f"\ncosmic-ray HumanEval holdout — model {result['model']}\n"
          f"  function-scoped kill rate: {fn['kill_rate']} ({fn['killed']}/{fn['mutants']})\n"
          f"  whole-module kill rate:    {mod['kill_rate']} ({mod['killed']}/{mod['mutants']})\n"
          f"  problems scored: {result['problems_scored']}/{result['problems']}\n"
          f"  report: {result.get('report_json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
