"""Adapter contract (ARCHITECTURE.md §8).

An adapter is a stateless module registered by name. The core never imports an
adapter directly — only through registry.py. Adding a target type = one new module
implementing this Protocol, zero core edits.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.core.models import TargetContract, TargetRef, TestScenario


@runtime_checkable
class Adapter(Protocol):
    name: str

    def introspect(self, ref: TargetRef) -> TargetContract:
        """Deterministic: inspect the target, return its testable units. No tokens."""

    def file_header(self, contract: TargetContract, scenarios: list[TestScenario] = ()) -> str:
        """Preamble for the generated test file: imports (incl. constructed types) + path bootstrap."""

    def emit(self, scenario: TestScenario, contract: TargetContract) -> str:
        """Render one scenario to test source code. Deterministic templating."""

    def test_function_name(self, scenario: TestScenario) -> str:
        """The generated test's function name — lets the runner map results to scenarios."""

    def runner_cmd(self, test_path: Path) -> list[str]:
        """Argv to execute the generated tests, e.g. ["pytest", "-q", str(path)]."""
