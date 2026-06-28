"""Deterministic validator-rejection scenarios.

The complement of constant-seeding: where constant-seeding lifts a validator's valid set so the model builds
VALID objects, this INVERTS the same parse to test the type's REJECTION contract. For a pydantic type
whose `@field_validator` has a cleanly-invertible guard (membership set / numeric range), the adapter
records a provably-invalid value on `FieldSpec.reject_example`; here we assemble a no-LLM scenario that
constructs the type with that invalid value and asserts construction raises `pydantic.ValidationError`.

This catches a bug class the valid-only suite is blind to: a weakened/removed validator (see
`benchmark/mutation.py::validator_weakening_mutants`). Fully deterministic — zero tokens, ast-only
upstream. Honest scope: only fields with an invertible `@field_validator` are covered, and a
scenario is emitted only when every OTHER required field/param is a fillable primitive (so a placeholder
can't trip a different validator); cross-field `@model_validator` rejection is deferred.
"""
from __future__ import annotations

import re
from typing import Any

from scripts.core.models import TargetContract, TestScenario, TypeSpec

# Type-appropriate VALID placeholders for the other (non-target) required fields/params.
_PLACEHOLDER: dict[str, Any] = {"str": "x", "int": 0, "float": 0.0, "bool": False}


def _placeholder(annotation: str | None) -> Any:
    """A minimal valid value for a primitive annotation, or None when it isn't safely fillable."""
    base = (annotation or "").split("[")[0].strip()
    if base in _PLACEHOLDER:
        return _PLACEHOLDER[base]
    if base == "Decimal":
        return {"$call": "Decimal", "args": ["0"]}
    return None


def _idents(annotation: str | None) -> list[str]:
    return re.findall(r"[A-Za-z_]\w*", annotation or "")


def _type_args(ts: TypeSpec, target_field: str, reject_value: Any) -> dict[str, Any] | None:
    """Constructor args: the target field gets the INVALID value; every other REQUIRED field gets a
    valid primitive placeholder (defaulted fields are omitted to accept their default, which also keeps
    a cross-field model_validator that keys off a non-default field from tripping). None = can't fill."""
    args: dict[str, Any] = {}
    for f in ts.fields:
        if f.name == target_field:
            args[f.name] = reject_value
        elif f.has_default:
            continue
        else:
            ph = _placeholder(f.annotation)
            if ph is None:
                return None
            args[f.name] = ph
    return args


def rejection_scenarios(contract: TargetContract) -> list[TestScenario]:
    """One deterministic rejection scenario per (unit, param, invertible-validator-field)."""
    out: list[TestScenario] = []
    seen: set[tuple[str, str, str]] = set()
    for unit in contract.units:
        for p in unit.params:
            type_name = next((i for i in _idents(p.annotation) if i in contract.types), None)
            if not type_name:
                continue
            ts = contract.types[type_name]
            if ts.kind != "pydantic":
                continue
            for f in ts.fields:
                if f.reject_example is None:
                    continue
                key = (unit.name, p.name, f.name)
                if key in seen:
                    continue
                type_args = _type_args(ts, f.name, f.reject_example)
                if type_args is None:
                    continue
                inputs: dict[str, Any] = {}
                fillable = True
                for q in unit.params:
                    if q.name == p.name:
                        inputs[q.name] = {"$type": type_name, "args": type_args}
                    else:
                        ph = _placeholder(q.annotation)
                        if ph is None:
                            fillable = False
                            break
                        inputs[q.name] = ph
                if not fillable:
                    continue
                seen.add(key)
                out.append(TestScenario(
                    id=f"reject_{unit.name}_{p.name}_{f.name}",
                    title=f"{type_name}.{f.name}: an invalid value is rejected at construction",
                    unit=unit.name,
                    expected=f"constructing {type_name} with an out-of-contract {f.name} raises ValidationError",
                    expect_error="ValidationError",
                    inputs=inputs,
                    rationale=(f"rejection test: the @field_validator on {type_name}.{f.name} must "
                               "reject out-of-contract values (a weakened validator is otherwise unseen)."),
                    tags=["rejection"],
                ))
    return out
