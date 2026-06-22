"""Stage 3 — MATERIALIZE. Deterministic: scenario JSON → test source on disk.

Zero tokens. Asks the adapter for a file header + one rendered function per scenario,
concatenates, and writes write-temp-then-rename so a half-written file is never left.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

from scripts.core.models import ScenarioSet, TargetContract
from scripts.logger import get_logger

logger = get_logger("materialize")


def materialize(
    adapter: ModuleType,
    contract: TargetContract,
    scenario_set: ScenarioSet,
    out_path: Path,
) -> Path:
    """Render the full test file for a ScenarioSet and write it to out_path."""
    parts = [adapter.file_header(contract, scenario_set.scenarios)]
    for scenario in scenario_set.scenarios:
        parts.append(adapter.emit(scenario, contract))
    source = "\n".join(parts).rstrip() + "\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(source, encoding="utf-8")
    os.replace(tmp, out_path)   # atomic rename
    logger.info("Wrote %d test(s) -> %s", len(scenario_set.scenarios), out_path)

    # Optional sibling files (e.g. web adapter served mode → a conftest.py carrying fixtures).
    if hasattr(adapter, "extra_files"):
        for fname, fsrc in adapter.extra_files(contract, scenario_set).items():
            extra_path = out_path.parent / fname
            extra_path.write_text(fsrc, encoding="utf-8")
            logger.info("Wrote sibling file -> %s", extra_path)

    # Optional artifact export (e.g. web adapter → idiomatic TypeScript Playwright).
    if hasattr(adapter, "typescript"):
        ts_path = out_path.with_suffix(".spec.ts")
        header = ('import { test, expect } from "@playwright/test";\n\n'
                  f"const BASE_URL = {contract.module!r};\n\n")
        ts = header + "\n".join(adapter.typescript(s, contract) for s in scenario_set.scenarios)
        ts_path.write_text(ts, encoding="utf-8")
        logger.info("Exported TypeScript artifact -> %s", ts_path)

    return out_path
