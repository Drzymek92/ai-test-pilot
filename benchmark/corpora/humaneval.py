"""HumanEval (EvalPlus) holdout loader — the LOCKED anti-overfit corpus.

This is the held-out CODE the generator never sees during development (decided 2026-06-25, see
`design/IMPROVEMENT_APPROACHES.md` -> "Held-out evaluation"). The dev corpora (QuixBugs + the in-repo
mutation set) are used to BUILD and tune the feedback loop (A1) and typed-input construction (A3); this
corpus exists to prove those improvements GENERALIZE rather than overfit our own harness.

HumanEval (https://github.com/openai/human-eval) ships 164 diverse single-file functions, each with a
`prompt` (signature + docstring), a human-written `canonical_solution` (the reference body), and an
`entry_point` (the function name). We reconstruct the correct module as `prompt + canonical_solution`,
point the tool at it, and seed bugs with OUR deterministic AST mutator (`benchmark/mutation.py`) — so
the held-out axis is the CODE distribution (everyday string/list/logic, a different distribution from
QuixBugs' classic algorithms), not the bug generator. EvalPlus's contribution is extra *test inputs*,
which we do NOT use (we use the mutator), so the base HumanEval reference solutions are exactly the
held-out code the design calls for.

Faithful to the project's ethos: a shallow git clone into the gitignored `_cache/` (no pip install, no
admin), stdlib-only parsing (`gzip` + `json`), and SELF-CONTAINED problems only (stdlib imports), so a
generated test imports cleanly with the standard `sys.path` bootstrap.

**Rule: do NOT look at holdout numbers while developing.** Baseline -> tune on dev -> single holdout run.
"""
from __future__ import annotations

import ast
import gzip
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("humaneval")

DEFAULT_URL = "https://github.com/openai/human-eval.git"
_STDLIB = set(sys.stdlib_module_names)


@dataclass(frozen=True)
class Problem:
    """One held-out problem: a reference-correct single-function module to mutate."""
    name: str            # entry-point function name (also the generation/mutation selector)
    task_id: str         # e.g. "HumanEval/0" (stable ordering key)
    source: str          # the reconstructed correct module: prompt + canonical_solution

    @property
    def selector(self) -> str:
        return self.name


def ensure_corpus(cache_dir: Path, *, url: str = DEFAULT_URL, clone: bool = True) -> Path | None:
    """Return the path to the HumanEval `*.jsonl.gz` data file, cloning the repo on first use.

    Mirrors `quixbugs.ensure_corpus`: a shallow clone into the gitignored cache; None if absent and
    `clone=False` (or the clone fails). Failure is non-fatal — the caller reports the corpus as
    unavailable. The dataset is `data/HumanEval.jsonl.gz` in the openai/human-eval repo.
    """
    repo = cache_dir / "human-eval"
    data = _find_data_file(repo)
    if data is not None:
        return data
    if not clone:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning HumanEval (shallow) into %s ...", repo)
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, str(repo)],
                       capture_output=True, text=True, timeout=180, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        logger.warning("HumanEval clone failed (%s) — skipping the holdout corpus.", detail.strip()[:200])
        return None
    return _find_data_file(repo)


def _find_data_file(repo: Path) -> Path | None:
    """Locate the HumanEval JSONL data file (gz or plain) inside a checkout."""
    if not repo.is_dir():
        return None
    for pattern in ("data/HumanEval.jsonl.gz", "data/*.jsonl.gz", "data/HumanEval.jsonl", "data/*.jsonl"):
        hits = sorted(repo.glob(pattern))
        if hits:
            return hits[0]
    return None


def _read_records(data_file: Path) -> list[dict]:
    """Parse the (optionally gzipped) JSONL dataset into a list of records."""
    opener = gzip.open if data_file.suffix == ".gz" else open
    with opener(data_file, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _is_self_contained(source: str) -> bool:
    """True if the module imports only stdlib (so the generated test imports cleanly)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level == 0 and top and top not in _STDLIB:
                return False
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _STDLIB:
                    return False
    return True


def _defines(source: str, fn_name: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
               for n in tree.body)


def _task_num(task_id: str) -> int:
    """Numeric suffix of a task id (`HumanEval/12` -> 12) for a stable, intuitive ordering."""
    try:
        return int(task_id.rsplit("/", 1)[-1])
    except ValueError:
        return 1 << 30


def load_problems(data_file: Path, *, max_count: int | None = None, seed: int = 0) -> list[Problem]:
    """Enumerate self-contained single-function problems, deterministically subset to `max_count`.

    The reconstructed correct module is `prompt + canonical_solution`. We keep only problems whose
    module imports stdlib-only and actually defines the entry point at top level. Subset is a PREFIX
    of the task-id-sorted list (not a random sample), so a smaller subset is always contained in a
    larger one — re-running with a bigger budget only adds problems, never reshuffles. `seed` is
    reserved for future randomized sampling and currently unused.
    """
    records = _read_records(data_file)
    problems: list[Problem] = []
    for rec in sorted(records, key=lambda r: _task_num(r.get("task_id", ""))):
        entry = rec.get("entry_point")
        prompt = rec.get("prompt", "")
        solution = rec.get("canonical_solution", "")
        if not (entry and prompt and solution):
            continue
        source = prompt + solution
        if not _is_self_contained(source):
            continue
        if not _defines(source, entry):
            continue
        problems.append(Problem(name=entry, task_id=rec["task_id"], source=source))
    if max_count is not None:
        problems = problems[:max_count]
    return problems
