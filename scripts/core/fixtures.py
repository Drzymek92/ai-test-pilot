"""Data-factory fixture provider — the synthetic-data-factory cooperation feature.

When enabled (--fixtures), ai-test-pilot asks the synthetic-data-factory to produce a
realistic, schema-valid dataset and threads a sample of it into scenario generation, so
the LLM's `inputs` use realistic field values instead of guessed literals. The dataset
is persisted as a test artifact under outputs/fixtures/.

The factory is invoked as a SUBPROCESS in its own project root — that reuses the tool
wholesale (no duplication) and sidesteps the cross-project `scripts` package-name
collision entirely. Failure is non-fatal: the provider warns and returns None so the
pipeline continues without fixtures.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from scripts.core.models import ScenarioSet
from scripts.logger import get_logger

logger = get_logger("fixtures")

_MAX_PROMPT_SAMPLE = 5     # records shown to the LLM
_MAX_RECORDS = 200         # cap loaded into memory


@dataclass
class FixtureBundle:
    domain: str
    records: list[dict] = field(default_factory=list)
    path: Path | None = None        # copied artifact under our outputs/fixtures/
    source: Path | None = None      # the factory's original output file
    entity: str | None = None

    def sample(self, n: int = _MAX_PROMPT_SAMPLE) -> list[dict]:
        return self.records[:n]


def _load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))[:_MAX_RECORDS]


def _pick_output(outputs: Path, before: set[Path], domain: str, entity: str | None) -> Path | None:
    """Choose the most relevant CSV the factory just produced."""
    new_csvs = sorted(
        (p for p in outputs.glob("*.csv") if p not in before and "_chat_" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not new_csvs:
        return None
    if entity:
        for p in new_csvs:
            if f"_{entity}_" in p.name or p.stem.endswith(entity):
                return p
    # else: prefer the largest (most rows) — usually the richest table.
    return max(new_csvs, key=lambda p: p.stat().st_size)


def generate_fixture(
    *,
    domain: str,
    rows: int,
    project_path: str | Path,
    out_dir: Path,
    entity: str | None = None,
    python_executable: str | None = None,
) -> FixtureBundle | None:
    """Run the synthetic-data-factory for `domain` and load a sample as a fixture.

    Returns None (non-fatal) if the factory is missing or the run fails.
    """
    factory_root = Path(project_path)
    if not factory_root.is_absolute():
        factory_root = (Path(__file__).resolve().parents[2] / factory_root).resolve()
    main_py = factory_root / "scripts" / "main.py"
    if not main_py.is_file():
        logger.warning("Data-factory not found at %s; skipping fixtures.", main_py)
        return None

    py = python_executable or sys.executable
    factory_outputs = factory_root / "scripts" / "outputs"
    before = set(factory_outputs.glob("*.csv")) if factory_outputs.is_dir() else set()

    cmd = [py, str(main_py), "--domain", domain, "--n", str(rows), "--no-judge"]
    logger.info("Invoking data-factory: %s (cwd=%s)", " ".join(cmd), factory_root)
    try:                                               # P6: bound the child (it may call an LLM)
        proc = subprocess.run(cmd, cwd=str(factory_root), capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("Data-factory exceeded 300s; skipping fixtures.")
        return None
    if proc.returncode != 0:
        logger.warning(
            "Data-factory run failed (exit %d); skipping fixtures.\n%s",
            proc.returncode, (proc.stderr or proc.stdout or "")[-800:],
        )
        return None

    src = _pick_output(factory_outputs, before, domain, entity)
    if src is None:
        logger.warning("Data-factory produced no usable CSV for '%s'; skipping fixtures.", domain)
        return None

    records = _load_csv(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"fixture_{domain}_{datetime.now():%Y%m%d_%H%M%S}{src.suffix}"
    shutil.copy2(src, dest)
    logger.info("Fixture ready: %d record(s) from %s -> %s", len(records), src.name, dest)
    return FixtureBundle(domain=domain, records=records, path=dest, source=src, entity=entity)


def _columns(fixture: FixtureBundle) -> list[str]:
    return list(fixture.records[0].keys()) if fixture.records else []


def prompt_block(fixture: FixtureBundle) -> str:
    """A compact block injected into the generation prompt."""
    sample = json.dumps(fixture.sample(), indent=2, ensure_ascii=False, default=str)
    cols = ", ".join(_columns(fixture))
    fname = fixture.path.name if fixture.path else f"{fixture.domain}.csv"
    return (
        f"\n\n## Realistic seed data (from synthetic-data-factory, domain '{fixture.domain}')\n"
        f"These are real example records for this kind of data:\n```json\n{sample}\n```\n"
        "When a parameter naturally takes a value of this shape, draw realistic values from "
        "these records into `inputs` instead of inventing placeholders, and set "
        f'"fixture": "{fixture.domain}" on any scenario that does so.\n'
        f"\nThe FULL dataset is also available as a real CSV file named `{fname}` with columns "
        f"[{cols}]. For a function that takes a path to a file of this data, add a tmp_files entry "
        f'`{{"param": "<the path param>", "filename": "{fname}", "from_fixture": true}}` — the tool '
        "fills it with the real file's contents (do NOT hand-author `text` for that entry).\n"
    )


def bind_fixture_files(scenario_set: ScenarioSet, fixture: FixtureBundle | None) -> int:
    """Fill every `from_fixture` tmp_file with the real factory file's contents.

    Deterministic post-generation step: the LLM only flags the intent; the tool supplies the
    actual bytes so the generated test exercises the genuine factory data. Returns the count bound.
    """
    if fixture is None or not fixture.path or not fixture.path.is_file():
        return 0
    content = fixture.path.read_text(encoding="utf-8")
    fname = fixture.path.name
    bound = 0
    for scenario in scenario_set.scenarios:
        used = False
        for tf in scenario.tmp_files:
            if tf.from_fixture:
                tf.text = content
                if not tf.filename or "." not in tf.filename:
                    tf.filename = fname
                bound += 1
                used = True
        if used and not scenario.fixture:
            scenario.fixture = fixture.domain
    if bound:
        logger.info("Bound %d fixture file(s) from %s into scenarios.", bound, fname)
    return bound
