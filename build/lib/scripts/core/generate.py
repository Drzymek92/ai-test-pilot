"""Stage 2 — GENERATE. The one genuinely fuzzy step: the LLM proposes scenarios.

Deterministic scaffolding around a single LLM call: build a prompt from the
introspection contract, call the gateway, then parse + validate the response into a
schema-checked ScenarioSet. Invalid JSON or schema violations trigger one repair retry
(ARCHITECTURE.md §2/§4). The LLM only ever returns JSON — code is rendered later.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from scripts.core.models import ScenarioSet, TargetContract, TestScenario
from scripts.llm_client import llm_call
from scripts.logger import get_logger

logger = get_logger("generate")

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _system_prompt(prompt_version: str, kind: str = "python") -> str:
    path = _PROMPT_DIR / f"scenarios_{kind}_{prompt_version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt version not found: {path}")
    return path.read_text(encoding="utf-8")


def _contract_block(contract: TargetContract) -> str:
    lines = [f"Module: {contract.module}", ""]
    for u in contract.units:
        lines.append(f"### {u.name}{u.signature or '()'}")
        if u.returns:
            lines.append(f"- returns: {u.returns}")
        if u.docstring:
            lines.append(f"- docstring: {u.docstring.strip()}")
        else:
            lines.append("- docstring: NONE — exact behaviour is unspecified; assert invariants "
                         "(type/shape/length), NOT guessed exact values; tag such scenarios 'uncertain'")
        if u.raises:
            lines.append(f"- raises: {', '.join(u.raises)}")
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
        for t in contract.types.values():
            if t.kind == "enum":
                lines.append(f"- enum {t.name}: members {', '.join(t.enum_members)} "
                             f'(use {{"$enum": "{t.name}.<MEMBER>"}})')
            else:
                fld = ", ".join(
                    f"{f.name}: {f.annotation}{' =default' if f.has_default else ''}" for f in t.fields)
                lines.append(f"- {t.kind} {t.name}({fld})")
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


def _parse_scenarios(raw: str, contract: TargetContract) -> list[TestScenario]:
    items = _extract_json(raw)
    valid = _valid_units(contract)
    scenarios: list[TestScenario] = []
    for i, item in enumerate(items):
        scenario = TestScenario(**item)
        if scenario.unit not in valid:
            raise ValueError(
                f"Scenario {i} targets unknown unit '{scenario.unit}'. "
                f"Allowed: {sorted(valid)}."
            )
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
    temperature: float = 0.2,
    prompt_version: str = "v1",
    repair_retries: int = 1,
    fixture_block: str = "",
    context_block: str = "",
    fewshot_block: str = "",
) -> ScenarioSet:
    kind = getattr(adapter, "prompt_kind", "python")
    describe = getattr(adapter, "describe_contract", None)
    block = describe(contract) if describe else _contract_block(contract)
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

    last_err: Exception | None = None
    prompt = human
    for attempt in range(repair_retries + 1):
        raw = llm_call(prompt, system=system, model=model, temperature=temperature)
        try:
            scenarios = _parse_scenarios(raw, contract)
            logger.info(
                "Generated %d scenario(s) for %s (attempt %d).",
                len(scenarios), contract.ref.locator, attempt + 1,
            )
            return ScenarioSet(
                target=contract.ref,
                scenarios=scenarios,
                model=model or "",
                prompt_version=prompt_version,
            )
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
