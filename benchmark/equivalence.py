"""Equivalent-mutant detection — make the kill rate HONEST (4-lite).

A mutant that produces identical behaviour to the correct code on every input is *equivalent* — it
isn't a real bug, so a test "failing to kill" it is not a detection miss. Counting equivalent mutants
in the denominator understates the suite. Equivalence is undecidable in general; the practical proxy
(trivial-compiler-equivalence / fuzzing) is: run correct-vs-mutant on a broad input sample of the same
SHAPE as the suite's own inputs — if they never disagree, treat the mutant as equivalent.

Crucially the fuzz set must go BEYOND the scenario inputs (the mutant survived precisely because it
agreed with correct on those), so we perturb each scenario input into many same-shape values. Only
plain-literal inputs are fuzzable (typed `$type`/`$call` objects are skipped → "unknown", left in the
denominator — conservative). Runs in a subprocess on trusted code (eval-time execution, like golden).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("equivalence")


def _is_literal(v) -> bool:
    """True if a value is a plain JSON literal tree (no $type/$call/$enum grammar)."""
    if isinstance(v, dict):
        if any(k in v for k in ("$type", "$call", "$enum")):
            return False
        return all(_is_literal(x) for x in v.values())
    if isinstance(v, list):
        return all(_is_literal(x) for x in v)
    return isinstance(v, (str, int, float, bool)) or v is None


def literal_inputs(scenarios, green_ids: set[str]) -> list[dict]:
    """The green scenarios' inputs that are entirely plain literals (fuzzable)."""
    out = []
    for s in scenarios:
        if s.id in green_ids and s.inputs and all(_is_literal(v) for v in s.inputs.values()):
            out.append(dict(s.inputs))
    return out


def _perturb(value):
    """A few same-shape alternative values for one argument (deterministic)."""
    if isinstance(value, bool):
        return [True, False]
    if isinstance(value, int):
        return [0, 1, -1, 2, 7, value + 1, value - 1, -value]
    if isinstance(value, float):
        return [0.0, 1.0, -1.0, value + 1, value / 2 if value else 1.0]
    if isinstance(value, str):
        return ["", "a", "abc", "Ab,Cd", value, value + "x", value.upper()]
    if isinstance(value, list):
        if value and all(isinstance(x, int) for x in value):
            return [[], [0], [1, 2, 3], [5, 5, 5], [-1, -2, -3], list(reversed(value)), value + [0]]
        return [[], value, value[:1]]
    return [value]


def fuzz_inputs(base: dict, *, cap: int = 24) -> list[dict]:
    """Same-shape perturbations of one scenario's kwargs (vary one arg at a time + the base)."""
    samples = [dict(base)]
    for key, val in base.items():
        for alt in _perturb(val):
            cand = dict(base)
            cand[key] = alt
            samples.append(cand)
    # de-dup (json key) and cap
    seen, out = set(), []
    for s in samples:
        k = json.dumps(s, sort_keys=True, default=str)
        if k not in seen:
            seen.add(k)
            out.append(s)
        if len(out) >= cap:
            break
    return out


_PROBE = '''\
import sys, json
sys.path.insert(0, {root!r})
sys.path.insert(0, {work!r})
import _aitp_eq_correct as _C
import _aitp_eq_mutant as _M
_fn = {fn!r}
_inputs = json.loads({inputs!r})
ran = diffs = 0
for _kw in _inputs:
    try:
        _rc = (True, repr(getattr(_C, _fn)(**_kw)))
    except Exception as _e:
        _rc = (False, type(_e).__name__)
    try:
        _rm = (True, repr(getattr(_M, _fn)(**_kw)))
    except Exception as _e:
        _rm = (False, type(_e).__name__)
    ran += 1
    if _rc != _rm:
        diffs += 1
print("AITP_EQ:" + json.dumps({{"ran": ran, "diffs": diffs}}))
'''


def is_equivalent(correct_src: str, mutant_src: str, fn_name: str, samples: list[dict],
                  *, work_dir: Path, project_root: Path, timeout: float = 8.0) -> str:
    """'equivalent' (agree on all samples) | 'distinct' (disagree somewhere) | 'unknown'.

    Writes correct + mutant as two sibling modules in `work_dir` (which should sit inside the target's
    package so project-local imports resolve) and diff-tests them in one subprocess.
    """
    if not samples:
        return "unknown"
    cmod = work_dir / "_aitp_eq_correct.py"
    mmod = work_dir / "_aitp_eq_mutant.py"
    probe = work_dir / "_aitp_eq_probe.py"
    try:
        cmod.write_text(correct_src, encoding="utf-8")
        mmod.write_text(mutant_src, encoding="utf-8")
        probe.write_text(_PROBE.format(root=str(project_root), work=str(work_dir),
                                       fn=fn_name, inputs=json.dumps(samples)), encoding="utf-8")
        proc = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                              cwd=str(project_root), timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Equivalence probe failed for %s: %s", fn_name, exc)
        return "unknown"
    finally:
        for p in (cmod, mmod, probe):
            p.unlink(missing_ok=True)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("AITP_EQ:"):
            data = json.loads(line[len("AITP_EQ:"):])
            if data["ran"] == 0:
                return "unknown"
            return "equivalent" if data["diffs"] == 0 else "distinct"
    return "unknown"
