"""Project-level target discovery for the standardized repo layout.

Given a project (by name or path), scan its `scripts/` tree, introspect every module
deterministically (ast only — no import, no LLM, zero tokens), and bucket the public
functions into:

  * testable-now   — no complex/unconstructable params; ready for a generate run
  * needs-fixtures — has params the tool can't build from primitives (domain objects,
                     callables, third-party types) — needs --fixtures or manual setup

It then prints a ready-to-run `--target ... --selector ...` command per module, so the
"grep the tree and hand-pick functions" step the workflow used to require is one call.

Layout assumption (the user's standard): sibling projects live under `projects/<name>/`,
each with a `scripts/` package. A path is also accepted for anything off-layout.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from scripts.core.models import TargetRef

# Framework/plumbing modules that are not worth proposing as test targets by default.
_SKIP_FILES = {"__init__.py", "logger.py", "llm_client.py", "prompt_compressor.py", "main.py"}
_SKIP_DIR_PARTS = {"outputs", "__pycache__", ".pytest_cache", "tests"}


@dataclass
class ModuleReport:
    path: Path
    rel: str
    testable: list[str] = field(default_factory=list)            # public, no complex params
    needs_fixtures: list[tuple[str, list[str]]] = field(default_factory=list)  # (name, complex)
    error: str | None = None

    def target_path(self, project_root: Path) -> str:
        """Path to pass to --target. main.py runs from the ai-test-pilot root, so workflow
        targets are written relative to it (e.g. ../<project>/scripts/...)."""
        return f"../{project_root.name}/{self.rel}"


def resolve_project_root(arg: str, *, sibling_base: Path) -> Path:
    """Resolve a project name or path to its root dir.

    A name resolves against `sibling_base` (the directory holding sibling projects);
    a path is used as-is. Falls back to treating the name as a path.
    """
    p = Path(arg)
    if p.is_dir():
        return p.resolve()
    candidate = sibling_base / arg
    if candidate.is_dir():
        return candidate.resolve()
    return p.resolve()


def git_changed_py(project_root: Path, since: str | None = None) -> set[Path] | None:
    """Resolved paths of changed `.py` files under `project_root`, via git (zero tokens).

    `since` is a git ref to diff against (e.g. `main`, a tag, `HEAD~3`); when None we diff the
    working tree + index against HEAD ("what I've touched"). Returns a set of absolute Paths, or
    None if the project isn't in a git work tree (caller falls back to a full scan).
    """
    try:
        top = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None  # not a git repo, or git unavailable
    repo_root = Path(top)

    diff_args = ["git", "-C", str(project_root), "diff", "--name-only"]
    diff_args += [since] if since else ["HEAD"]
    names = subprocess.run(diff_args, capture_output=True, text=True, check=True).stdout.splitlines()
    if since is None:  # working-tree mode also includes brand-new (untracked) modules
        names += subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()

    proj = project_root.resolve()
    changed: set[Path] = set()
    for name in names:
        if not name.endswith(".py"):
            continue
        p = (repo_root / name).resolve()
        if not p.exists():  # skip deletions/renames-away — nothing to test
            continue
        try:
            p.relative_to(proj)  # keep only files inside this project
        except ValueError:
            continue
        changed.add(p)
    return changed


def discover(project_root: Path, adapter, only: set[Path] | None = None) -> list[ModuleReport]:
    scripts_dir = project_root / "scripts"
    search_root = scripts_dir if scripts_dir.is_dir() else project_root

    reports: list[ModuleReport] = []
    for path in sorted(search_root.rglob("*.py")):
        if only is not None and path.resolve() not in only:
            continue
        if path.name in _SKIP_FILES:
            continue
        if _SKIP_DIR_PARTS & set(path.parts):
            continue
        rel = path.relative_to(project_root).as_posix()
        rep = ModuleReport(path=path, rel=rel)
        try:
            contract = adapter.introspect(TargetRef(adapter=adapter.name, locator=str(path)))
        except Exception as exc:  # no functions / parse error — skip but note it
            rep.error = type(exc).__name__
            # only surface real parse errors, not "no functions" noise
            if exc.__class__.__name__ != "ValueError":
                reports.append(rep)
            continue
        for u in contract.units:
            if u.name.startswith("_"):
                continue
            if u.complex_params:
                rep.needs_fixtures.append((u.name, list(u.complex_params)))
            else:
                rep.testable.append(u.name)
        if rep.testable or rep.needs_fixtures:
            reports.append(rep)
    return reports


def format_report(project_root: Path, reports: list[ModuleReport], scope: str | None = None) -> str:
    title = f"# Discoverable test targets — {project_root.name}"
    if scope:
        title += f"  ({scope})"
    lines = [title, ""]
    n_testable = sum(len(r.testable) for r in reports)
    n_fixtures = sum(len(r.needs_fixtures) for r in reports)
    lines.append(f"{n_testable} testable-now function(s) · {n_fixtures} need fixtures "
                 f"· {len(reports)} module(s)\n")

    for r in reports:
        if r.error:
            lines.append(f"## {r.rel}\n- (skipped: {r.error})\n")
            continue
        lines.append(f"## {r.rel}")
        if r.testable:
            lines.append(f"- testable-now: {', '.join(r.testable)}")
            lines.append(f"  - `python scripts/main.py --target {r.target_path(project_root)} "
                         f"--selector {','.join(r.testable)} --golden`")
        if r.needs_fixtures:
            for name, complex_params in r.needs_fixtures:
                lines.append(f"- needs-fixtures: {name}  (complex: {'; '.join(complex_params)})")
        lines.append("")
    return "\n".join(lines)
