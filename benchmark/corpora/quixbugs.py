"""QuixBugs corpus loader — the VERIFIED external half of the bug-detection eval.

QuixBugs (https://github.com/jkoppel/QuixBugs) is a benchmark of 40 classic algorithms, each
shipped as a single-line-buggy version (`python_programs/<name>.py`) and a human-verified correct
version (`correct_python_programs/<name>.py`). We point the tool at the CORRECT version, generate a
suite, then re-run it against the BUGGY version: a real, independently-curated bug the suite either
catches or misses. No per-program dependency install (pure-Python single-function modules).

We clone once into a gitignored cache and select only SELF-CONTAINED programs (no intra-repo helper
imports such as `node`/`load_testdata`) so a generated test imports cleanly with the standard
`sys.path` bootstrap. Deterministic subset selection (sorted by name, optional seeded sample).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("quixbugs")

DEFAULT_URL = "https://github.com/jkoppel/QuixBugs.git"
_STDLIB = set(sys.stdlib_module_names)


@dataclass(frozen=True)
class BugPair:
    """One verified bug: a correct module and its single-line-buggy counterpart."""
    name: str            # program/function name (file stem); also the generation selector
    correct_path: Path
    buggy_path: Path

    @property
    def selector(self) -> str:
        return self.name


def ensure_corpus(cache_dir: Path, *, url: str = DEFAULT_URL, clone: bool = True) -> Path | None:
    """Return the QuixBugs checkout dir, cloning it on first use. None if absent and clone=False
    (or the clone fails). A shallow clone keeps it small; failure is non-fatal (the caller skips
    the external corpus and can still run the in-repo mutation corpus)."""
    repo = cache_dir / "QuixBugs"
    if (repo / "correct_python_programs").is_dir():
        return repo
    if not clone:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning QuixBugs (shallow) into %s ...", repo)
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, str(repo)],
                       capture_output=True, text=True, timeout=180, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        logger.warning("QuixBugs clone failed (%s) — skipping the external corpus.", detail.strip()[:200])
        return None
    return repo if (repo / "correct_python_programs").is_dir() else None


def _is_self_contained(path: Path) -> bool:
    """True if the module imports only stdlib (no QuixBugs-internal helpers like `node`)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
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


def load_pairs(repo: Path, *, max_count: int | None = None, seed: int = 0) -> list[BugPair]:
    """Enumerate self-contained correct<->buggy pairs, deterministically subset to `max_count`.

    Subset is a *prefix* of the name-sorted list (not a random sample) so a smaller subset is always
    contained in a larger one — re-running with a bigger budget only adds programs, never reshuffles
    the tracked series. `seed` is reserved for future randomized sampling and currently unused.
    """
    correct_dir = repo / "correct_python_programs"
    buggy_dir = repo / "python_programs"
    pairs: list[BugPair] = []
    for cpath in sorted(correct_dir.glob("*.py")):
        name = cpath.stem
        bpath = buggy_dir / cpath.name
        if not bpath.is_file():
            continue
        if not (_is_self_contained(cpath) and _is_self_contained(bpath)):
            continue
        # Must actually define a function named after the file (the entry point the suite targets).
        if not _defines(cpath, name):
            continue
        pairs.append(BugPair(name=name, correct_path=cpath, buggy_path=bpath))
    if max_count is not None:
        pairs = pairs[:max_count]
    return pairs


def _defines(path: Path, fn_name: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
               for n in tree.body)
