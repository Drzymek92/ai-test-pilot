"""Before/after benchmark comparator — diff two `cov.py` runs and flag per-file
coverage regressions.

Replaces eyeballing BENCHMARK_extractors.md vs BENCHMARK_extractors_after.md with a
reproducible, exit-coded check. Reuses the shared before/after kernel in
`commons.evals.regression` (the same one the skill eval harness uses).

Usage:
    python benchmark/compare.py --before before.json --after after.json [--min-drop 0.0]
  Each input is a cov.py summary (a plain JSON file, or its stdout line prefixed
  "COVJSON:"). Exit 2 if any source file's coverage % dropped by more than --min-drop.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# repo-root bootstrap so `commons` imports without install:
# benchmark/compare.py -> benchmark -> ai-test-pilot -> projects -> Claude_Projects
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from commons.evals.regression import metric_regressions  # noqa: E402


def _load(path: Path) -> dict:
    """Load a cov.py summary — tolerate a plain JSON file or a 'COVJSON:{...}' line."""
    text = path.read_text(encoding="utf-8")
    for line in reversed(text.splitlines()):
        if line.startswith("COVJSON:"):
            return json.loads(line[len("COVJSON:"):])
    return json.loads(text)


def _pct_map(summary: dict) -> dict[str, float]:
    return {name: float(f["pct"]) for name, f in summary.get("files", {}).items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff two cov.py runs for coverage regressions.")
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--min-drop", type=float, default=0.0,
                    help="ignore coverage drops of this many pct points or fewer")
    args = ap.parse_args()

    before = _pct_map(_load(Path(args.before)))
    after = _pct_map(_load(Path(args.after)))
    diff = metric_regressions(before, after, min_delta=args.min_drop)
    print(json.dumps(diff, indent=2))
    if diff["regressed"]:
        print(f"\n[REGRESSION] coverage dropped on: {', '.join(diff['regressed'])}",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
