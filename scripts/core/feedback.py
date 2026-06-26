"""Approach 1 — feedback-driven regeneration (close the loop with the coverage signal).

Single-shot generation picks happy-path inputs; the bug-detection eval showed the remaining
misses are an INPUT-SELECTION problem (`lis`, `longest_common_subsequence`: full coverage of the
function but the seeded edge is never reached). This module stops single-shotting: after the first
generation it measures which lines of the target the suite NEVER executes (via `benchmark/cov.py`,
the project's stdlib-`trace` coverage tool — quality reuses it too), then re-prompts the LLM for
ADDITIONAL scenarios whose inputs force those lines to run. It is [CoverUp]'s coverage-feedback loop,
driven by the coverage signal we already compute.

Anti-overfit guard (decided in `design/IMPROVEMENT_APPROACHES.md`): only the COVERAGE gap is ever fed
back — never any bug/mutant information. The generator cannot see the bugs the `detect` harness scores
it on, so the loop cannot memorise a held-out bug set. It only learns to exercise more of the target.

Determinism / cost: each round is ONE extra `generate_scenarios` call, routed through the same P1
scenario cache, so re-runs of an unchanged target replay for free. The loop is capped at a small
number of rounds and is opt-in (a flag / config), since it spends real tokens.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

from scripts.core.materialize import materialize
from scripts.core.models import ScenarioSet, TargetContract, TestScenario
from scripts.logger import get_logger

logger = get_logger("feedback")

_PROBE_PREFIX = "_fb_probe_"            # marks the throwaway probe test (excluded from coverage match)


# ── coverage measurement (deterministic, zero tokens) ──────────────────────────
def measure_uncovered(
    target_file: Path,
    test_file: Path,
    project_root: Path,
    *,
    grep: str | None = None,
    python_exe: str | None = None,
    cover_dir: Path | None = None,
    timeout: float = 45.0,
) -> list[int]:
    """Run `benchmark/cov.py` over the suite and return the target's uncovered (executable) lines.

    Reuses the project's existing line-coverage harness (stdlib `trace`) as a subprocess so the
    feedback loop shares ONE coverage implementation with the quality gate. Returns the sorted
    source line numbers of the target file that are executable but were never run by the suite.

    Bounded + non-fatal: a generated test running under `trace` can be pathological (e.g. an
    exponential-recursion target like naive `levenshtein` blows up time/memory under per-line
    instrumentation). The probe runs in its own process group and is HARD-killed (whole tree) on
    timeout — abandoning it returns [] (feedback just stops for that target) rather than letting it
    wedge or OOM the batch. Any failure (timeout, suite won't import, cov.py error) → [].
    """
    stem = target_file.stem
    cov_script = project_root / "benchmark" / "cov.py"
    cover_dir = cover_dir or (test_file.parent / "_fb_cover")
    cmd = [
        python_exe or sys.executable, str(cov_script),
        "--test", str(test_file), "--cwd", str(project_root),
        "--grep", grep or stem, "--cover", str(cover_dir),
    ]
    stdout = _run_capped(cmd, cwd=str(project_root), env=_subprocess_env(),
                         timeout=timeout, label=stem)
    if stdout is None:
        return []

    payload = _last_covjson(stdout)
    if payload is None:
        logger.warning("Coverage probe produced no COVJSON for %s.", stem)
        return []

    target_cover = f"{stem}.cover"
    missed: set[int] = set()
    for fname, info in payload.get("files", {}).items():
        # Match the target's own cover file ("pkg.sub.<stem>.cover" or "<stem>.cover"); never the
        # probe test itself (whose name also contains the stem).
        if _PROBE_PREFIX in fname:
            continue
        if fname == target_cover or fname.endswith("." + target_cover):
            missed.update(info.get("missed_lines", []))
    return sorted(missed)


def _subprocess_env() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"     # cp1252 console otherwise breaks on non-ASCII (gotcha)
    return env


def _run_capped(cmd: list[str], *, cwd: str, env: dict, timeout: float, label: str) -> str | None:
    """Run the probe subprocess, HARD-killing its whole process tree on timeout.

    `subprocess.run(timeout=)` alone proved insufficient for a pathological probe (a runaway test can
    OOM-thrash the box before a long timeout fires and may orphan children). So: own process group +
    a tree kill (`taskkill /T` on Windows, `killpg` elsewhere) on expiry. Returns stdout, or None on
    timeout / spawn error (caller treats None as "no coverage signal" → feedback stops, non-fatal).
    """
    import os
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    new_session = os.name != "nt"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                cwd=cwd, env=env, creationflags=flags, start_new_session=new_session)
    except OSError as exc:
        logger.warning("Coverage probe could not start for %s: %s", label, exc)
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out
    except subprocess.TimeoutExpired:
        logger.warning("Coverage probe for %s exceeded %.0fs -- hard-killing (pathological test?).",
                       label, timeout)
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return None


def _kill_tree(proc: subprocess.Popen) -> None:
    import os
    import signal
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _last_covjson(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("COVJSON:"):
            try:
                return json.loads(line[len("COVJSON:"):])
            except json.JSONDecodeError:
                return None
    return None


# ── scope uncovered lines to the units actually under test ─────────────────────
def unit_line_ranges(target_source: str, unit_names: set[str]) -> list[tuple[int, int]]:
    """AST line ranges (1-based, inclusive) of the selected functions in the target source.

    `cov.py` reports uncovered lines across the WHOLE file, but a selector-scoped run only tests a
    few functions — feeding back lines from untested functions is noise (and the LLM can't propose
    scenarios for units outside the contract anyway). Restricting to these ranges makes the feedback
    the precise input-selection signal: 'these branches OF THE FUNCTIONS YOU TEST are never reached'.
    """
    ranges: list[tuple[int, int]] = []
    try:
        tree = ast.parse(target_source)
    except SyntaxError:
        return ranges
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in unit_names:
            ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def _scope_missed(missed: list[int], ranges: list[tuple[int, int]]) -> list[int]:
    if not ranges:
        return missed                     # no ranges resolved (e.g. methods) → don't over-filter
    return [m for m in missed if any(a <= m <= b for a, b in ranges)]


# ── prompt block (coverage gap only — NEVER any bug/mutant info) ───────────────
def uncovered_block(
    target_source: str,
    missed: list[int],
    existing: list[TestScenario],
    *,
    round_no: int,
    max_lines: int = 25,
    max_existing: int = 12,
) -> str:
    """Build the feedback prompt block: the uncovered source lines + the inputs already used.

    The model is asked for NEW scenarios whose inputs reach the listed lines, and explicitly told
    not to repeat the existing inputs. No bug, mutant, or expected-failure information is included.
    """
    src_lines = target_source.splitlines()
    shown = missed[:max_lines]
    code = []
    for n in shown:
        text = src_lines[n - 1] if 0 < n <= len(src_lines) else ""
        code.append(f"{n:>5}: {text}")
    more = f"\n  (+{len(missed) - len(shown)} more uncovered line(s) omitted)" if len(missed) > len(shown) else ""

    used = []
    for s in existing[:max_existing]:
        try:
            args = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in s.inputs.items())
        except (TypeError, ValueError):
            args = "..."
        used.append(f"- {s.unit}({args})")
    used_block = "\n".join(used) if used else "- (none)"

    return (
        f"\n## Coverage feedback — round {round_no} (close the gap)\n"
        "The current suite runs, but these REACHABLE lines of the target are never executed by any "
        "scenario's inputs, so the suite can't tell correct from incorrect behaviour there. Propose "
        "ADDITIONAL scenarios whose inputs force these specific lines to run (e.g. trigger the "
        "branch/loop/early-return that guards them). Use the same value grammar and the same "
        "assertion rules as before.\n\n"
        "Uncovered lines (line: source):\n"
        "```python\n" + "\n".join(code) + more + "\n```\n"
        "Inputs ALREADY covered — do NOT repeat these, choose inputs that differ:\n"
        f"{used_block}\n"
        "Return ONLY a JSON array of the NEW, non-duplicate scenarios.\n\n"
    )


# ── de-duplication + id uniqueness ─────────────────────────────────────────────
def _sig(s: TestScenario) -> tuple:
    try:
        inputs = json.dumps(s.inputs, sort_keys=True, default=str)
    except (TypeError, ValueError):
        inputs = repr(s.inputs)
    return (s.unit, inputs, s.assertion or "", s.expect_error or "")


def dedupe_new(
    candidates: list[TestScenario],
    existing: list[TestScenario],
    *,
    round_no: int,
) -> list[TestScenario]:
    """Keep only candidates with inputs not already present; guarantee unique, traceable ids."""
    seen_sigs = {_sig(s) for s in existing}
    seen_ids = {s.id for s in existing}
    kept: list[TestScenario] = []
    for c in candidates:
        sig = _sig(c)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        if c.id in seen_ids:
            c.id = f"fb{round_no}_{c.id}"
        suffix = 1
        while c.id in seen_ids:
            c.id = f"fb{round_no}_{suffix}_{c.id}"
            suffix += 1
        seen_ids.add(c.id)
        if "feedback" not in c.tags:        # provenance marker (surfaces in the report)
            c.tags.append("feedback")
        kept.append(c)
    return kept


# ── orchestration ──────────────────────────────────────────────────────────────
def run_feedback(
    *,
    adapter: ModuleType,
    contract: TargetContract,
    scenario_set: ScenarioSet,
    target_file: Path,
    project_root: Path,
    probe_dir: Path,
    generate_fn: Callable[[str, int], ScenarioSet],
    max_rounds: int,
    count: int,
    min_uncovered: int,
    max_uncovered_shown: int,
    rebind: Callable[[ScenarioSet], None] | None = None,
    measure_fn: Callable[..., list[int]] = measure_uncovered,
) -> dict:
    """Run up to `max_rounds` coverage-feedback rounds, appending non-duplicate scenarios in place.

    `generate_fn(feedback_block, count)` returns a ScenarioSet of candidate additions (it owns the
    cache/budget/model knobs). `rebind`, if given, re-binds fixture files after new scenarios are
    appended. Returns a summary dict (rounds, added, tokens, uncovered before/after).
    """
    target_source = target_file.read_text(encoding="utf-8")
    ranges = unit_line_ranges(target_source, {u.name for u in contract.units})
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe = probe_dir / f"{_PROBE_PREFIX}{target_file.stem}.py"

    def _measure() -> list[int]:
        return _scope_missed(measure_fn(target_file, probe, project_root, grep=target_file.stem), ranges)

    summary = {"rounds": 0, "added": 0, "tokens_in": 0, "tokens_out": 0,
               "uncovered_before": None, "uncovered_after": None}
    try:
        for r in range(1, max_rounds + 1):
            materialize(adapter, contract, scenario_set, probe)
            missed = _measure()
            if summary["uncovered_before"] is None:
                summary["uncovered_before"] = len(missed)
            summary["uncovered_after"] = len(missed)
            if len(missed) < min_uncovered:
                logger.info("Feedback: %d uncovered line(s) (< %d) — stopping.", len(missed), min_uncovered)
                break

            block = uncovered_block(target_source, missed, scenario_set.scenarios,
                                    round_no=r, max_lines=max_uncovered_shown)
            try:
                extra = generate_fn(block, count)
            except Exception as exc:                          # noqa: BLE001 — non-fatal: keep round-0 suite
                logger.warning("Feedback round %d generation failed: %s", r, exc)
                break
            summary["tokens_in"] += extra.tokens_in
            summary["tokens_out"] += extra.tokens_out

            new = dedupe_new(extra.scenarios, scenario_set.scenarios, round_no=r)
            if not new:
                logger.info("Feedback round %d produced no new (non-duplicate) scenarios — stopping.", r)
                break
            scenario_set.scenarios.extend(new)
            if rebind is not None:
                rebind(scenario_set)
            summary["added"] += len(new)
            summary["rounds"] = r
            logger.info("Feedback round %d: +%d scenario(s) targeting %d uncovered line(s).",
                        r, len(new), len(missed))

        # Final uncovered count after the last addition (best-effort; informational).
        if summary["added"] > 0:
            materialize(adapter, contract, scenario_set, probe)
            summary["uncovered_after"] = len(_measure())
    finally:
        _cleanup(probe, probe_dir / "_fb_cover")
    return summary


def _cleanup(probe: Path, cover_dir: Path) -> None:
    probe.unlink(missing_ok=True)
    probe.with_suffix(".junit.xml").unlink(missing_ok=True)
    if cover_dir.is_dir():
        for p in cover_dir.glob("*.cover"):
            p.unlink(missing_ok=True)
        try:
            cover_dir.rmdir()
        except OSError:
            pass
