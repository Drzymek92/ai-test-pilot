"""Stage 2 — GENERATE. The one genuinely fuzzy step: the LLM proposes scenarios.

Deterministic scaffolding around a single LLM call: build a prompt from the
introspection contract, call the gateway, then parse + validate the response into a
schema-checked ScenarioSet. Invalid JSON or schema violations trigger one repair retry.
The LLM only ever returns JSON — code is rendered later.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from scripts.core import cache as scenario_cache
from scripts.core.models import ScenarioSet, TargetContract, TestScenario
from scripts.llm_client import llm_call, resolve_model
from scripts.logger import get_logger

logger = get_logger("generate")

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _system_prompt(prompt_version: str, kind: str = "python") -> str:
    path = _PROMPT_DIR / f"scenarios_{kind}_{prompt_version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt version not found: {path}")
    return path.read_text(encoding="utf-8")


def _truncate_source(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n# ...(source truncated for token budget)"


def _contract_block(contract: TargetContract, *, include_source: bool = True,
                    source_max_chars: int = 1200) -> str:
    lines = [f"Module: {contract.module}", ""]
    for u in contract.units:
        lines.append(f"### {u.name}{u.signature or '()'}")
        if u.returns:
            lines.append(f"- returns: {u.returns}")
        if u.docstring:
            lines.append(f"- docstring: {u.docstring.strip()}")
        else:
            lines.append("- docstring: NONE — derive behaviour from the source below; if still "
                         "uncertain assert invariants (type/shape/length), NOT guessed exact "
                         "values, and tag such scenarios 'uncertain'")
        if u.raises:
            lines.append(f"- raises: {', '.join(u.raises)}")
        if include_source and u.source:
            # P3a: the unit's own code, so assertions can target SPECIFIC computed behaviour.
            lines.append("- source (the code under test — base assertions on what it actually "
                         "computes, not the function name):")
            lines.append("```python")
            lines.append(_truncate_source(u.source, source_max_chars))
            lines.append("```")
        if not u.is_pure:
            lines.append("- IMPURE (file/IO/side effects): do NOT pass a path that does not exist. "
                         "Use `tmp_files` to create real inputs, or restrict to cases needing no file.")
        if u.complex_params:
            lines.append(f"- UNRESOLVED TYPED PARAMS: {', '.join(u.complex_params)}. These domain "
                         "objects could NOT be introspected, so they cannot be constructed — passing a "
                         "plain dict will fail at attribute access. OMIT scenarios needing them, or "
                         "restrict to cases (e.g. None inputs) that don't. NEVER fabricate them as dicts.")
        lines.append("")

    if contract.types:
        lines.append("## Constructible types (build these with the $type grammar — NOT plain dicts)")
        has_constraints = False
        for t in contract.types.values():
            if t.kind == "enum":
                lines.append(f"- enum {t.name}: members {', '.join(t.enum_members)} "
                             f'(use {{"$enum": "{t.name}.<MEMBER>"}})')
            else:
                parts = []
                for f in t.fields:
                    seg = f"{f.name}: {f.annotation}"
                    if f.constraint:                      # P3b-2: respect the value constraints
                        seg += f" [{f.constraint}]"
                        has_constraints = True
                    if f.has_default:
                        seg += " =default"
                    parts.append(seg)
                lines.append(f"- {t.kind} {t.name}({', '.join(parts)})")
        if has_constraints:
            lines.append("  NOTE: fields in [brackets] carry constraints (e.g. [gt=0] = strictly "
                         "greater than 0, [min_length=1] = non-empty) — choose values that SATISFY "
                         "them, or the object fails to construct.")
        lines.append("")
    return "\n".join(lines)


def _extract_json(text: str) -> list[dict]:
    """Strip optional markdown fences and parse a JSON array."""
    cleaned = _FENCE.sub("", text).strip()
    data = json.loads(cleaned)
    if isinstance(data, dict) and "scenarios" in data:   # tolerate a wrapped object
        data = data["scenarios"]
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of scenarios.")
    return data


def _valid_units(contract: TargetContract) -> set[str]:
    return {u.name for u in contract.units}


def _parse_scenarios(raw: str, contract: TargetContract, adapter=None) -> list[TestScenario]:
    items = _extract_json(raw)
    valid = _valid_units(contract)
    # Allow-list value-grammar symbols against the tool-resolved types (adapter-specific;
    # the python adapter exposes validate_scenario). A violation is a ValueError, so it
    # routes through the repair-retry loop below — the model can't author code tokens.
    validate = getattr(adapter, "validate_scenario", None)
    scenarios: list[TestScenario] = []
    for i, item in enumerate(items):
        scenario = TestScenario(**item)
        if scenario.unit not in valid:
            raise ValueError(
                f"Scenario {i} targets unknown unit '{scenario.unit}'. "
                f"Allowed: {sorted(valid)}."
            )
        if validate is not None:
            validate(scenario, contract)
        scenarios.append(scenario)
    if not scenarios:
        raise ValueError("Model returned an empty scenario list.")
    return scenarios


def generate_scenarios(
    contract: TargetContract,
    *,
    adapter=None,
    count: int = 6,
    model: str | None = None,
    temperature: float = 0.0,
    prompt_version: str = "v1",
    repair_retries: int = 1,
    fixture_block: str = "",
    context_block: str = "",
    fewshot_block: str = "",
    cache_dir: Path | None = None,
    use_cache: bool = False,
    refresh_cache: bool = False,
    llm_timeout: float | None = None,
    llm_retries: int = 2,
    include_source: bool = True,
    source_max_chars: int = 1200,
    budget=None,
) -> ScenarioSet:
    kind = getattr(adapter, "prompt_kind", "python")
    describe = getattr(adapter, "describe_contract", None)
    block = describe(contract) if describe else _contract_block(
        contract, include_source=include_source, source_max_chars=source_max_chars)
    system = _system_prompt(prompt_version, kind)
    noun = "Playwright web" if kind == "web" else "pytest"
    human = (
        f"Propose up to {count} {noun} scenarios for the following target.\n\n"
        f"{block}\n"
        f"{context_block}"
        f"{fixture_block}"
        f"{fewshot_block}"
        f"Return ONLY the JSON array."
    )

    # P1 — reproducibility: replay an identical (target + prompt + model + temp + count) run.
    key = scenario_cache.cache_key(
        system=system, human=human, model=resolve_model(model),
        temperature=temperature, count=count)
    if use_cache and cache_dir is not None and not refresh_cache:
        cached = scenario_cache.load(cache_dir, key)
        if cached is not None:
            cached.tokens_in = cached.tokens_out = 0      # P4: a replay spends nothing this run
            return cached

    # P4-2 — estimate before spending; enforce the per-run cap (cache miss only).
    if budget is not None and budget.enabled:
        from scripts.core import budget as budget_mod
        est = budget_mod.estimate_call(system, human, adapter=getattr(adapter, "name", "python_pytest"),
                                       count=count, budget=budget)
        budget_mod.enforce(est["total"], budget.max_tokens_per_run,
                           on_over=budget.on_over, scope="run")

    last_err: Exception | None = None
    prompt = human
    spent_in = spent_out = 0
    for attempt in range(repair_retries + 1):
        res = llm_call(prompt, system=system, model=model, temperature=temperature,
                       timeout=llm_timeout, retries=llm_retries, return_usage=True)
        # Real client returns (text, usage); tolerate a plain-string return (test fakes).
        raw, usage = res if isinstance(res, tuple) else (res, {"input_tokens": 0, "output_tokens": 0})
        spent_in += usage["input_tokens"]
        spent_out += usage["output_tokens"]
        try:
            scenarios = _parse_scenarios(raw, contract, adapter)
            logger.info(
                "Generated %d scenario(s) for %s (attempt %d).",
                len(scenarios), contract.ref.locator, attempt + 1,
            )
            scenario_set = ScenarioSet(
                target=contract.ref,
                scenarios=scenarios,
                model=model or "",
                prompt_version=prompt_version,
                tokens_in=spent_in,
                tokens_out=spent_out,
            )
            if use_cache and cache_dir is not None:
                scenario_cache.store(cache_dir, key, scenario_set)
            return scenario_set
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_err = exc
            logger.warning("Scenario parse failed (attempt %d): %s", attempt + 1, exc)
            prompt = (
                f"{human}\n\nYour previous response was invalid: {exc}\n"
                f"Return ONLY a valid JSON array matching the schema."
            )

    raise RuntimeError(
        f"Scenario generation failed after {repair_retries + 1} attempt(s): {last_err}"
    )
