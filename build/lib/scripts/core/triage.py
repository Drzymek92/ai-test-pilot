"""Stage 5 — TRIAGE. Why did a failure fail: real_bug | bad_scenario | flaky | env_issue.

Deterministic-first (the efficiency ladder): a signal table classifies the clear cases for free
(fabricated path, dict-for-typed-param, import error, a broken golden lock = behaviour change).
The LLM is called ONCE, only for the genuinely ambiguous failures (mostly assertion mismatches on
LLM-written assertions). Token-frugal by construction; passes need no triage.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from scripts.core.generate import _extract_json
from scripts.core.models import RunResult, ScenarioSet, TargetContract, TestScenario, TriageVerdict
from scripts.llm_client import llm_call
from scripts.logger import get_logger

logger = get_logger("triage")

_TRIAGE_SYSTEM = (
    "You are triaging a failing auto-generated pytest case. Decide WHY it failed:\n"
    "- real_bug: the function under test is wrong (the test's expectation is reasonable).\n"
    "- bad_scenario: the test's inputs/expectation are wrong (the function is fine).\n"
    "- flaky: non-deterministic (time/order/random) rather than a true failure.\n"
    "- env_issue: import/setup/dependency problem, not the logic.\n"
    "Return ONLY a JSON array; one object per scenario_id given, with fields: "
    '{"scenario_id", "verdict", "confidence" (0-1 float), "evidence" (short), '
    '"suggested_fix" (short or null)}.'
)


def _deterministic(r: RunResult, scn: TestScenario) -> TriageVerdict | None:
    """High-confidence rules. Returns None when the failure is genuinely ambiguous."""
    sig = r.signal or ""
    cap = r.captured or ""

    def v(verdict, conf, evidence, fix=None):
        return TriageVerdict(scenario_id=r.scenario_id, verdict=verdict, confidence=conf,
                             evidence=evidence, suggested_fix=fix, source="deterministic")

    if sig in ("ModuleNotFoundError", "ImportError") or r.signal == "not_collected":
        return v("env_issue", 0.8, f"import/collection failure ({sig or 'not collected'})",
                 "check the target module imports / sys.path bootstrap")
    if sig == "FileNotFoundError" and not scn.tmp_files:
        return v("bad_scenario", 0.9, "references a file path that does not exist",
                 "use tmp_files to create a real input file")
    if sig == "AttributeError" and "object has no attribute" in cap and "'dict'" in cap:
        return v("bad_scenario", 0.9, "passed a plain dict where a typed object is required",
                 "construct the typed object via the $type grammar")
    if sig == "NameError":
        return v("bad_scenario", 0.8, "references an undefined symbol (bad import/typo)")
    if sig == "TypeError" and ("argument" in cap or "positional" in cap or "keyword" in cap):
        return v("bad_scenario", 0.85, "call shape doesn't match the signature")
    if sig == "AssertionError" and "characterization" in scn.tags:
        return v("real_bug", 0.7, "a locked characterization assertion broke → behaviour changed",
                 "review the change; update the golden only if intended")
    return None                                    # ambiguous → LLM (or default)


def _llm_triage(items: list[tuple[RunResult, TestScenario]], contract: TargetContract,
                model: str | None) -> list[TriageVerdict]:
    docs = {u.name: (u.docstring or "") for u in contract.units}
    payload = [{
        "scenario_id": r.scenario_id, "unit": s.unit, "unit_docstring": docs.get(s.unit, ""),
        "inputs": s.inputs, "expected": s.expected, "assertion": s.assertion,
        "failure": r.captured[:500],
    } for r, s in items]
    prompt = ("Triage each failing case below. Function docstrings describe intended behaviour.\n\n"
              + json.dumps(payload, indent=2, default=str) + "\n\nReturn ONLY the JSON array.")
    try:
        raw = llm_call(prompt, system=_TRIAGE_SYSTEM, model=model)
        out: list[TriageVerdict] = []
        for d in _extract_json(raw):
            d["source"] = "llm"
            out.append(TriageVerdict(**d))
        valid = {r.scenario_id for r, _ in items}
        out = [v for v in out if v.scenario_id in valid]
        if out:
            return out
        raise ValueError("LLM returned no usable verdicts")
    except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError) as exc:
        logger.warning("LLM triage failed (%s); defaulting ambiguous failures.", exc)
        return [_default(r) for r, _ in items]


def _default(r: RunResult) -> TriageVerdict:
    return TriageVerdict(scenario_id=r.scenario_id, verdict="bad_scenario", confidence=0.3,
                         evidence="ambiguous assertion failure; LLM triage unavailable",
                         source="deterministic")


def triage(results: list[RunResult], scenario_set: ScenarioSet, contract: TargetContract,
           *, llm_for_ambiguous: bool = True, model: str | None = None) -> list[TriageVerdict]:
    by_id = {s.id: s for s in scenario_set.scenarios}
    failures = [r for r in results if r.status in ("failed", "error")]
    verdicts: list[TriageVerdict] = []
    ambiguous: list[tuple[RunResult, TestScenario]] = []
    for r in failures:
        scn = by_id.get(r.scenario_id)
        if scn is None:
            continue
        det = _deterministic(r, scn)
        if det is not None:
            verdicts.append(det)
        else:
            ambiguous.append((r, scn))
    if ambiguous:
        verdicts += _llm_triage(ambiguous, contract, model) if llm_for_ambiguous \
            else [_default(r) for r, _ in ambiguous]
    return verdicts


def counts(verdicts: list[TriageVerdict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in verdicts:
        out[v.verdict] = out.get(v.verdict, 0) + 1
    return out
