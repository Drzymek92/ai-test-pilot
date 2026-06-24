"""Stage 4 — RUN. Deterministic: execute the generated tests, parse outcomes.

Zero tokens. Runs pytest as a subprocess of the current interpreter (so the active interpreter's
env is used) and reads pytest's built-in JUnit XML for a per-test pass/fail/error
mapping — no extra plugin needed. Results map back to scenarios by function name.
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

from scripts.core.models import RunResult, ScenarioSet
from scripts.logger import get_logger

logger = get_logger("runner")

_CAP = 1500          # truncate captured output per test
_TIMEOUT_BUFFER = 10.0   # seconds added to the per-test budget for pytest startup/collection


def _build_cmd(adapter: ModuleType, test_path: Path, junit: Path) -> list[str]:
    base = list(adapter.runner_cmd(test_path))
    if base and base[0] == "pytest":               # run via this interpreter's pytest
        base = [sys.executable, "-m", *base]
    base += ["-p", "no:cacheprovider", f"--junit-xml={junit}"]
    return base


def _parse_junit(junit: Path) -> dict[str, tuple[str, str, str]]:
    """name → (status, signal, message). status ∈ passed|failed|error."""
    out: dict[str, tuple[str, str, str]] = {}
    if not junit.is_file():
        return out
    root = ET.parse(junit).getroot()
    for case in root.iter("testcase"):
        name = case.get("name", "")
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if error is not None:
            node, status = error, "error"
        elif failure is not None:
            node, status = failure, "failed"
        elif skipped is not None:
            node, status = skipped, "passed"        # explicit skip is not a failure
        else:
            out[name] = ("passed", "ok", "")
            continue
        signal = (node.get("type") or "assertion").split(".")[-1]
        message = (node.get("message") or node.text or "")[:_CAP]
        out[name] = (status, signal, message)
    return out


def run_tests(
    adapter: ModuleType,
    test_path: Path,
    scenario_set: ScenarioSet,
    *,
    cwd: Path | None = None,
    per_test_timeout: float | None = None,
) -> list[RunResult]:
    junit = test_path.with_suffix(".junit.xml")
    cmd = _build_cmd(adapter, test_path, junit)
    logger.info("Running: %s", " ".join(cmd))

    # P2 — bound the run so one hanging generated test can't hang the whole pipeline.
    # No pytest-timeout plugin here (no admin), so cap the subprocess at the per-test budget
    # times the scenario count (+ startup buffer); on expiry every scenario is error/timeout.
    timeout = None
    if per_test_timeout and per_test_timeout > 0:
        timeout = per_test_timeout * max(1, len(scenario_set.scenarios)) + _TIMEOUT_BUFFER
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(cwd) if cwd else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        junit.unlink(missing_ok=True)
        logger.error("Run exceeded the %.0fs budget -- killed; marking all scenarios timed-out.",
                     timeout)
        return [RunResult(scenario_id=s.id, status="error", signal="timeout",
                          captured=f"Run exceeded the {timeout:.0f}s time budget and was killed.")
                for s in scenario_set.scenarios]
    parsed = _parse_junit(junit)
    junit.unlink(missing_ok=True)

    results: list[RunResult] = []
    for scenario in scenario_set.scenarios:
        fname = adapter.test_function_name(scenario)
        if fname in parsed:
            status, signal, message = parsed[fname]
            results.append(RunResult(
                scenario_id=scenario.id, status=status, signal=signal, captured=message,
            ))
        else:
            # No testcase emitted → collection/import error before this test ran.
            tail = (proc.stderr or proc.stdout or "")[-_CAP:]
            results.append(RunResult(
                scenario_id=scenario.id, status="error",
                signal="not_collected", captured=tail,
            ))
    passed = sum(r.status == "passed" for r in results)
    logger.info("Run complete: %d/%d passed (exit %d).", passed, len(results), proc.returncode)
    return results
