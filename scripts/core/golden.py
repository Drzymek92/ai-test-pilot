"""Golden / characterization assertion mode (Phase 2).

For functions returning a structured/computed result the LLM can't predict (e.g. quantized
Decimal math), a `type(result).__name__ == 'X'` assertion is weak. Golden mode RUNS the
constructed call once, captures the real result's repr, and rewrites the assertion to lock it
in — turning the test into a regression guard (a characterization test, per Feathers).

Deterministic + honest: this asserts CURRENT behaviour (including any current bug), so it's
opt-in (`--golden`) and the scenario is tagged `characterization`. Only applies to plain
in-memory calls (no tmp_files, no expected error) whose result has a STABLE repr.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

from scripts.core.models import ScenarioSet, TargetContract
from scripts.logger import get_logger

logger = get_logger("golden")

_MAX_REPR = 600          # don't lock in a monstrous assertion
_UNSTABLE = (" at 0x", "<function ", "<lambda>")   # default object reprs aren't reproducible
_TIMEISH = re.compile(r"datetime|date|time", re.IGNORECASE)


def _eligible(contract: TargetContract, scenario_set: ScenarioSet):
    """Scenarios safe to characterize: plain in-memory calls that are reproducible.

    Excludes file/error scenarios, and — critically — clock/RNG-reading units UNLESS the
    scenario pins the time via a datetime/date parameter (else the lock is a time-bomb that
    passes today and fails later).
    """
    units = {u.name: u for u in contract.units}
    out = []
    for s in scenario_set.scenarios:
        if s.tmp_files or s.expect_error:
            continue
        u = units.get(s.unit)
        if u is None:
            continue
        if not u.reads_clock:
            out.append(s)
            continue
        time_params = [p.name for p in u.params if p.annotation and _TIMEISH.search(p.annotation)]
        if time_params and any(tp in s.inputs for tp in time_params):
            out.append(s)                    # clock fallback bypassed by a pinned time → deterministic
        else:
            logger.info("Skipping golden for %s — unit reads the clock and time isn't pinned.", s.id)
    return out


_PROBE_TIMEOUT = 60   # seconds — defense-in-depth (P6): the probe executes the TARGET's own code


def _run_probe(probe: Path, python_executable: str | None, cwd: Path) -> tuple[dict[str, dict], str]:
    try:
        proc = subprocess.run([python_executable or sys.executable, str(probe)],
                              cwd=str(cwd), capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Golden probe exceeded %ds — skipping characterization this run.", _PROBE_TIMEOUT)
        return {}, "probe timed out"
    out: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in d:
            out[d["id"]] = d
    return out, proc.stderr


def capture_goldens(adapter: ModuleType, contract: TargetContract, scenario_set: ScenarioSet,
                    *, cwd: Path, out_dir: Path, python_executable: str | None = None) -> dict[str, dict]:
    """Run the probe TWICE and keep only results that match across both runs.

    The double run auto-filters non-deterministic calls (datetime.now()/random/IO) — locking
    those would create a time-bomb test. Non-fatal: returns {} on any failure.
    """
    if not hasattr(adapter, "probe_source"):
        return {}
    eligible = _eligible(contract, scenario_set)
    if not eligible:
        return {}

    out_dir.mkdir(parents=True, exist_ok=True)
    probe = out_dir / f"_golden_probe_{datetime.now():%Y%m%d_%H%M%S}.py"
    probe.write_text(adapter.probe_source(contract, eligible), encoding="utf-8")
    try:
        run1, err1 = _run_probe(probe, python_executable, cwd)
        run2, _ = _run_probe(probe, python_executable, cwd)
    finally:
        probe.unlink(missing_ok=True)

    captures: dict[str, dict] = {}
    for sid, d1 in run1.items():
        d2 = run2.get(sid)
        if d1.get("ok") and d2 and d2.get("ok") and d1.get("repr") == d2.get("repr"):
            captures[sid] = d1                 # reproducible → safe to lock
        elif d1.get("ok") and d2 and d1.get("repr") != d2.get("repr"):
            logger.info("Skipping golden for %s — result not reproducible across runs.", sid)
    if not captures and err1:
        logger.warning("Golden probe produced no captures; stderr tail:\n%s", err1[-500:])
    return captures


def apply_goldens(scenario_set: ScenarioSet, captures: dict[str, dict]) -> int:
    """Rewrite assertions to lock captured results. Returns how many were locked."""
    locked = 0
    for s in scenario_set.scenarios:
        cap = captures.get(s.id)
        if not cap or not cap.get("ok"):
            continue
        rep = cap.get("repr", "")
        if not rep or len(rep) > _MAX_REPR or any(u in rep for u in _UNSTABLE):
            continue                         # unstable/oversized → keep the LLM's assertion
        s.assertion = f"repr(result) == {rep!r}"
        if "characterization" not in s.tags:
            s.tags.append("characterization")
        locked += 1
    if locked:
        logger.info("Golden mode: locked %d characterization assertion(s).", locked)
    return locked
