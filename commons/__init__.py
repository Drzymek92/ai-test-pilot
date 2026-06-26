"""commons — small, project-agnostic helper modules vendored into this repo.

Single-task, stdlib-only, type-hinted utilities with no project-specific state that use
`logging.getLogger(__name__)` so the caller's logging config applies. Imported via a lightweight
path bootstrap (a walk-up to the dir holding `commons/`), so they work from a source checkout with
no install step. Currently holds `commons.evals.regression` — the before/after run-comparison
kernel used by the bug-detection quality gate and the benchmark comparator.
"""
