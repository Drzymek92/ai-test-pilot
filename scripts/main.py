"""AI Test Pilot — entry point.

The implementation is split by concern:
  - `scripts/cli.py`      — argument parsing + subcommand dispatch + the exit-code contract.
  - `scripts/pipeline.py` — `run_pipeline`: the 7-stage pipeline (driven by a typed RunRequest).
  - `scripts/core/report.py` — run-report Markdown rendering.

This module stays a thin launcher so `python scripts/main.py …` and the `ai-test-pilot` console
entry point keep working, and re-exports the names callers historically imported from here.

Exit-code contract: 0 ok · 1 internal · 2 usage · 3 target · 4 LLM · 5 quality · 6 budget · 7 detection.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable as `scripts`, ahead of any site-packages `scripts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cli import (  # noqa: F401  (re-exported for back-compat)
    EXIT_BUDGET,
    EXIT_DETECTION,
    EXIT_INTERNAL,
    EXIT_LLM,
    EXIT_OK,
    EXIT_QUALITY,
    EXIT_TARGET,
    main,
)
from scripts.pipeline import run_pipeline  # noqa: F401  (historically imported from scripts.main)

if __name__ == "__main__":
    sys.exit(main())
