"""A/M2 — Pynguin head-to-head baseline on the SAME QuixBugs subset.

Every comparable test-generation paper reports against a search-based (SBST) peer — EvoSuite for Java,
Pynguin for Python. This harness runs **Pynguin** (DynaMOSA/SBST, no LLM) on the same QuixBugs programs
our `detect` eval uses, measured with the SAME kill-rate mechanic (generate from CORRECT code → a test
green on correct that fails on the buggy version is a kill), so the README can state
*"our suite kills X% vs Pynguin Y% on the same N QuixBugs targets."*

Fair-comparison choices:
  - Same corpus + prefix subset as `detect` (`benchmark/corpora/quixbugs.py`), so the targets line up.
  - Pynguin at its **defaults** (mirroring how we report OUR default-config QuixBugs number), with a
    per-program search-time budget; assertion generation on (so the suite encodes current behaviour and
    can actually catch a behaviour-changing bug).
  - Kill semantics identical to `detection`: only tests GREEN on the correct module are credited; a
    program whose generated suite isn't green on correct is "unscored" (excluded from the scored rate).

Pynguin executes the code under test, so it requires `PYNGUIN_DANGER_AWARE=1` (set here per-subprocess).
Writes ONLY to `benchmark/eval/baselines/`. Run (after a `detect` baseline exists):

    python -m benchmark.baselines.pynguin_runner --subset 20 --search-time 30
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmark.corpora import quixbugs as qb  # noqa: E402
from benchmark.stats import fmt_rate_ci, wilson_ci  # noqa: E402
from scripts.logger import get_logger  # noqa: E402

logger = get_logger("pynguin_baseline")

_PYNGUIN = [sys.executable, "-m", "pynguin"]


def _run_pynguin(module_dir: Path, module_name: str, out_dir: Path, *, search_time: int,
                 seed: int) -> Path | None:
    """Generate a Pynguin suite for `module_name` (found in module_dir). Return the test file or None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYNGUIN_DANGER_AWARE": "1", "PYTHONHASHSEED": "0"}
    cmd = _PYNGUIN + [
        "--project-path", str(module_dir), "--module-name", module_name,
        "--output-path", str(out_dir), "--maximum-search-time", str(search_time),
        "--seed", str(seed), "--assertion-generation", "SIMPLE",
        "-v",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=search_time + 120)
    except subprocess.TimeoutExpired:
        logger.warning("Pynguin timed out on %s", module_name)
        return None
    test_file = out_dir / f"test_{module_name}.py"
    if test_file.is_file():
        return test_file
    logger.warning("Pynguin produced no suite for %s (rc=%s): %s",
                   module_name, proc.returncode, (proc.stderr or proc.stdout or "")[-300:])
    return None


def _passed_nodeids(test_file: Path, module_dir: Path, per_test_timeout: float) -> set[str] | None:
    """Run a generated pytest file with `module_dir` importable; return the set of PASSED node names.

    None means the run could not be collected at all (import/syntax error) — treated as "no green".
    """
    junit = test_file.with_suffix(".junit.xml")
    env = {**os.environ, "PYTHONPATH": str(module_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    cmd = [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider",
           f"--junit-xml={junit}"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, cwd=str(module_dir), env=env,
                       timeout=per_test_timeout)
    except subprocess.TimeoutExpired:
        junit.unlink(missing_ok=True)
        return set()
    if not junit.is_file():
        return None
    passed: set[str] = set()
    root = ET.parse(junit).getroot()
    for case in root.iter("testcase"):
        name = case.get("name", "")
        if case.find("failure") is None and case.find("error") is None:
            passed.add(name)
    junit.unlink(missing_ok=True)
    return passed


def _killed(test_file: Path, module_file: Path, correct_src: str, buggy_src: str,
            green: set[str], per_test_timeout: float) -> bool:
    """Swap the buggy source in, re-run; killed iff a GREEN-on-correct test now fails. Restore after."""
    module_file.write_text(buggy_src, encoding="utf-8")
    try:
        passed_buggy = _passed_nodeids(test_file, module_file.parent, per_test_timeout)
    finally:
        module_file.write_text(correct_src, encoding="utf-8")
    if passed_buggy is None:                       # buggy suite didn't even collect → behaviour changed
        return True
    return bool(green - passed_buggy)              # some green test no longer passes


def _measure_one(pair, work_root: Path, *, search_time: int, seed: int,
                 per_test_timeout: float) -> dict:
    """Generate a Pynguin suite for one program and score it (kill mechanic). Returns its case dict."""
    correct_src = pair.correct_path.read_text(encoding="utf-8")
    buggy_src = pair.buggy_path.read_text(encoding="utf-8")
    work = work_root / pair.name
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    src_dir, out_dir = work / "src", work / "out"
    src_dir.mkdir(parents=True)
    module_file = src_dir / f"{pair.name}.py"
    module_file.write_text(correct_src, encoding="utf-8")
    try:
        test_file = _run_pynguin(src_dir, pair.name, out_dir, search_time=search_time, seed=seed)
        if test_file is None:
            return {"name": pair.name, "scored": False, "killed": False, "green": 0,
                    "note": "pynguin produced no suite"}
        shutil.copyfile(test_file, src_dir / test_file.name)        # run beside the module
        run_test = src_dir / test_file.name
        green = _passed_nodeids(run_test, src_dir, per_test_timeout)
        if not green:
            return {"name": pair.name, "scored": False, "killed": False, "green": 0,
                    "note": "no green baseline (suite not green on correct)"}
        killed = _killed(run_test, module_file, correct_src, buggy_src, green, per_test_timeout)
        logger.info("%s — pynguin %s (green %d)", pair.name, "KILLED" if killed else "survived",
                    len(green))
        return {"name": pair.name, "scored": True, "killed": killed, "green": len(green), "note": ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _load_checkpoint(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    c = json.loads(line)
                    out[c["name"]] = c
                except json.JSONDecodeError:
                    pass
    return out


def run(subset: int | None, *, clone: bool, search_time: int, seed: int,
        per_test_timeout: float, fresh: bool = False) -> dict:
    cache_dir = _PROJECT_ROOT / "benchmark" / "corpora" / "_cache"
    repo = qb.ensure_corpus(cache_dir, clone=clone)
    if repo is None:
        return {"available": False, "note": "QuixBugs corpus unavailable"}
    pairs = qb.load_pairs(repo, max_count=subset)

    base = _PROJECT_ROOT / "benchmark" / "eval" / "baselines"
    work_root = base / "_work"
    work_root.mkdir(parents=True, exist_ok=True)

    # RESUMABLE (Pynguin runs are long; same teardown-survival pattern as the cosmic harness).
    ckpt = base / "_pynguin_checkpoint.jsonl"
    if fresh:
        ckpt.unlink(missing_ok=True)
    done = _load_checkpoint(ckpt)
    if done:
        logger.info("Resuming: %d/%d program(s) already checkpointed.", len(done), len(pairs))

    cases: list[dict] = []
    for pair in pairs:
        if pair.name in done:
            cases.append(done[pair.name])
            continue
        case = _measure_one(pair, work_root, search_time=search_time, seed=seed,
                            per_test_timeout=per_test_timeout)
        with ckpt.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(case) + "\n")
            fh.flush()
        cases.append(case)

    result = _aggregate(cases, subset, search_time, seed)
    ckpt.unlink(missing_ok=True)                            # full run complete -> clear the checkpoint
    return result


def _ours_quixbugs() -> float | None:
    """Our committed QuixBugs kill rate from the detection baseline (the head-to-head reference)."""
    bp = _PROJECT_ROOT / "benchmark" / "detection_baseline.json"
    if not bp.is_file():
        return None
    return json.loads(bp.read_text(encoding="utf-8")).get("panel", {}).get("quixbugs_kill_rate")


def _aggregate(cases: list[dict], subset, search_time: int, seed: int) -> dict:
    n = len(cases)
    scored = [c for c in cases if c["scored"]]
    killed = sum(c["killed"] for c in cases)
    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": "pynguin",
        "corpus": "quixbugs",
        "subset": subset, "search_time_s": search_time, "seed": seed,
        "n": n, "scored": len(scored), "killed": killed,
        "kill_rate": round(killed / n, 3) if n else None,                       # over all programs
        "kill_rate_scored": round(killed / len(scored), 3) if scored else None,  # over usable suites
        "kill_rate_ci": wilson_ci(killed, n),
        "ours_quixbugs_kill_rate": _ours_quixbugs(),
        "cases": cases,
    }
    out_dir = _PROJECT_ROOT / "benchmark" / "eval" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"pynguin_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["report_json"] = str(out_dir / f"pynguin_{stamp}.json")
    _write_md(result, out_dir / f"pynguin_{stamp}.md")
    return result


def _write_md(result: dict, path: Path) -> None:
    ours = result["ours_quixbugs_kill_rate"]
    lines = [
        f"# Pynguin head-to-head (SBST baseline) — QuixBugs — {result['ts']}", "",
        "> Search-based peer (Pynguin, no LLM) on the SAME QuixBugs subset + SAME kill-rate mechanic as "
        "our `detect` eval. Pynguin at defaults, per-program search-time budget.", "",
        f"- **Pynguin kill rate: {fmt_rate_ci(result['killed'], result['n'])}** (95% Wilson CI; "
        f"{result['killed']}/{result['n']} programs; scored {result['scored']})",
        f"- **Ours (AI Test Pilot, same subset): {ours if ours is not None else 'n/a'}**",
        f"- Search time: {result['search_time_s']}s/program · seed {result['seed']}", "",
        "| program | scored | killed | green | note |", "|---|---|---|---|---|",
    ]
    for c in result["cases"]:
        lines.append(f"| {c['name']} | {'✓' if c['scored'] else '·'} | "
                     f"{'✓' if c['killed'] else '·'} | {c['green']} | {c.get('note', '')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pynguin_runner")
    ap.add_argument("--subset", type=int, default=20, help="QuixBugs programs (prefix; default 20)")
    ap.add_argument("--search-time", type=int, default=30, help="Pynguin search seconds per program")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--per-test-timeout", type=float, default=60.0)
    ap.add_argument("--no-clone", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore + clear any checkpoint and start over (default: resume where it stopped)")
    a = ap.parse_args(argv)
    result = run(a.subset, clone=not a.no_clone, search_time=a.search_time, seed=a.seed,
                 per_test_timeout=a.per_test_timeout, fresh=a.fresh)
    if not result.get("available", True):
        print(f"\n{result['note']}")
        return 1
    print(f"\nPynguin head-to-head (QuixBugs subset {result['subset']}):\n"
          f"  Pynguin kill rate: {fmt_rate_ci(result['killed'], result['n'])} "
          f"(scored {result['scored']}/{result['n']})\n"
          f"  Ours:              {result['ours_quixbugs_kill_rate']}\n"
          f"  report: {result.get('report_json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
