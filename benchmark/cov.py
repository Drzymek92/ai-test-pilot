"""Zero-dependency line-coverage harness (stdlib `trace`).

Runs a pytest test file under trace.Trace(count=1) and reports line coverage for the
source file(s) whose path contains --grep. No coverage.py / no install needed.

Usage:
    python benchmark/cov.py --test <test_file> --cwd <project_root> --grep extractors
Prints a JSON summary to stdout (last line) and writes annotated .cover files to --cover.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import trace
from pathlib import Path

_COVERED = re.compile(r"^\s*\d+:")        # "   12: code"  → executed
_MISSED = ">>>>>>"                          # ">>>>>> code"  → executable but never run


def _parse_cover(path: Path) -> dict:
    executable = covered = 0
    missed_lines: list[int] = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.startswith(_MISSED):
            executable += 1
            missed_lines.append(i)
        elif _COVERED.match(line):
            executable += 1
            covered += 1
    pct = round(100 * covered / executable, 1) if executable else 0.0
    return {"executable": executable, "covered": covered,
            "missed": executable - covered, "pct": pct, "missed_lines": missed_lines}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--grep", default="extractors")
    ap.add_argument("--cover", default="benchmark/_cover")
    args = ap.parse_args()

    cwd = str(Path(args.cwd).resolve())
    test = str(Path(args.test).resolve())
    coverdir = Path(args.cover).resolve()
    coverdir.mkdir(parents=True, exist_ok=True)

    os.chdir(cwd)
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    import pytest

    tracer = trace.Trace(count=1, trace=0)
    rc = tracer.runfunc(pytest.main, ["-q", "-p", "no:cacheprovider", test])
    results = tracer.results()
    results.write_results(summary=False, coverdir=str(coverdir))

    matches = [p for p in coverdir.glob("*.cover") if args.grep in p.name]
    summary = {"pytest_rc": int(rc), "files": {}}
    for p in matches:
        summary["files"][p.name] = _parse_cover(p)
    print("COVJSON:" + json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
