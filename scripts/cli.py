"""AI Test Pilot — CLI: argument parsing + subcommand dispatch.

Thin layer over the pipeline (`scripts/pipeline.py`): each subcommand parses its own args, loads config,
calls into `core`/`pipeline`, prints a summary, and maps pipeline exceptions to the exit-code contract.

Exit-code contract (usable non-interactively / in CI):
    0  success — the tool ran, generated tests, and wrote a report (a generated-test failure is a
       triage finding in the report, not a tool error).
    1  internal/unexpected error.
    2  usage error (bad arguments / missing --target) — argparse's own code.
    3  target error — the module can't be introspected (missing/unreadable/syntax) — a clean skip.
    4  LLM error — generation failed after retries/timeout; the tool never half-generates.
    5  quality regression — the `quality` gate found a metric regression vs the baseline (P5).
    6  budget exceeded — an estimate exceeded the token cap with on_over=abort (P4).
    7  detection regression — the `detect` kill-rate gate regressed vs baseline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project root importable as `scripts`, ahead of any site-packages `scripts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import load_config
from scripts.core import registry
from scripts.core.errors import BudgetError, LLMError, TargetError
from scripts.core.models import RunRequest
from scripts.logger import get_logger
from scripts.pipeline import build_budget, run_pipeline

logger = get_logger("cli")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Exit-code contract (see module docstring + LIMITATIONS.md).
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_TARGET = 3
EXIT_LLM = 4
EXIT_QUALITY = 5
EXIT_BUDGET = 6
EXIT_DETECTION = 7


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI Test Pilot — LLM-driven test generator.")
    p.add_argument("--target", help="path to the target module (python adapter)")
    p.add_argument("--adapter", help="adapter name (default: from config)")
    p.add_argument("--selector", help="comma-separated function names to scope introspection")
    p.add_argument("--count", type=int, help="max scenarios to request")
    p.add_argument("--model", help="override the LLM model")
    p.add_argument("--prompt-version", help="prompt version (default: from config)")
    p.add_argument("--config", help="path to ai_test_pilot.toml")
    p.add_argument("--no-run", action="store_true", help="generate + materialize but skip running")
    p.add_argument("--smoke", action="store_true", help="one tiny live LLM call, then exit")
    # P1 — reproducibility: scenario cache/lock
    p.add_argument("--no-cache", action="store_true",
                   help="bypass the scenario cache (always call the LLM; don't read or write the cache)")
    p.add_argument("--refresh-cache", action="store_true",
                   help="ignore any cached scenarios and regenerate, overwriting the cache entry")
    # data-factory fixture provider
    p.add_argument("--fixtures", action="store_true",
                   help="seed scenario inputs with realistic data from synthetic-data-factory")
    p.add_argument("--fixture-domain", help="data-factory domain to generate (overrides config)")
    p.add_argument("--fixture-entity", help="for a relational domain, which entity table to sample")
    p.add_argument("--fixture-rows", type=int, help="rows to request from the data-factory")
    # project domain-context (value/assertion semantics)
    p.add_argument("--context", help="explicit path to a project.md/README to use as domain context")
    p.add_argument("--no-context", action="store_true",
                   help="disable auto-detection of agent/project.md|README domain context")
    p.add_argument("--no-cut-source", action="store_true",
                   help="don't feed the unit's own source into generation (P3a CUT context is on by default)")
    p.add_argument("--golden", action="store_true",
                   help="characterization mode: run each call once and lock assertions to the result")
    # Approach 1 — feedback-driven regeneration (coverage-gap-driven extra rounds; python adapter)
    p.add_argument("--feedback", action="store_true",
                   help="after first generation, feed UNCOVERED target lines back to the LLM and "
                        "regenerate to reach them (extra LLM round[s] → tokens; python adapter only)")
    p.add_argument("--no-feedback", action="store_true",
                   help="disable the coverage-feedback loop even if enabled in config")
    # deterministic validator-rejection tests (no LLM; python adapter)
    p.add_argument("--reject-tests", dest="reject_tests", action="store_true",
                   help="also emit deterministic tests that a validator-gated type REFUSES an "
                        "out-of-contract value at construction (no LLM; python adapter)")
    # web adapter — deep-Playwright options
    p.add_argument("--serve", action="store_true",
                   help="web: serve the target's directory over localhost http so the generated tests "
                        "get a base_url + auth_state(storage_state) fixture and a real origin")
    p.add_argument("--web-async", action="store_true",
                   help="web: tag every scenario `async` so the asyncio Playwright variant is emitted")
    return p.parse_args(argv)


def _accept_cmd(rest: list[str]) -> int:
    """`accept <run_id> --kept N` — backfill how many proposed tests the human kept."""
    ap = argparse.ArgumentParser(prog="main.py accept")
    ap.add_argument("run_id")
    ap.add_argument("--kept", type=int, required=True, help="number of generated tests kept")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    ledger_path = _PROJECT_ROOT / cfg.ledger.path
    from scripts.core import ledger
    if ledger.backfill_acceptance(a.run_id, a.kept, ledger_path):
        print(f"✓ recorded acceptance for run {a.run_id}: kept {a.kept}")
        return 0
    print(f"✗ run_id {a.run_id} not found in the ledger ({ledger_path})")
    return 1


def _resolve_draft(arg: str, out_base: Path) -> Path:
    """A draft is either a path to a generated test file, or a run_id to look one up."""
    p = Path(arg)
    if p.is_file():
        return p
    matches = [m for m in (out_base / "tests").glob(f"*_{arg}.py")]
    if matches:
        return sorted(matches)[0]
    raise SystemExit(f"No draft test file found for '{arg}' (path or run_id).")


def _promote_cmd(rest: list[str]) -> int:
    """`promote <test_file|run_id> [--into <suite.py>] [--approx]` — clean a draft for the suite."""
    ap = argparse.ArgumentParser(prog="main.py promote")
    ap.add_argument("draft", help="path to a generated test file, or a run_id")
    ap.add_argument("--into", help="existing suite file to append non-duplicate tests into")
    ap.add_argument("--approx", action="store_true",
                    help="wrap float-bearing assertions in pytest.approx(...)")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    out_base = _PROJECT_ROOT / cfg.run.output_dir
    from scripts.core import promote

    draft_path = _resolve_draft(a.draft, out_base)
    draft_src = draft_path.read_text(encoding="utf-8")

    if a.into:
        dest = Path(a.into)
        if not dest.is_file():
            raise SystemExit(f"--into target not found: {dest}")
        summary = promote.promote_into(draft_src, dest, approx=a.approx)
        print(f"\nPromoted from {draft_path.name} -> {summary['dest']}")
        print(f"  added:   {', '.join(summary['added']) or '(none)'}")
        if summary["skipped_duplicates"]:
            print(f"  skipped (already present): {', '.join(summary['skipped_duplicates'])}")
        if summary["imports_merged"]:
            print(f"  imports merged: {', '.join(summary['imports_merged'])}")
        if summary["imports_added"]:
            print(f"  imports added:  {', '.join(summary['imports_added'])}")
        return 0

    cleaned = promote.cleaned_source(draft_src, approx=a.approx)
    dest = out_base / "promoted" / draft_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(cleaned, encoding="utf-8")
    print(f"\nCleaned draft -> {dest}\n  (review, then copy into your suite, or re-run with --into)")
    return 0


def _discover_cmd(rest: list[str]) -> int:
    """`discover <project_name|path>` — list testable targets across a project's scripts/.

    `--changed` / `--since <ref>` restrict the scan to git-changed modules — the incremental
    path: regenerate tests only for what moved, not the whole tree (deterministic, zero tokens).
    """
    ap = argparse.ArgumentParser(prog="main.py discover")
    ap.add_argument("project", help="sibling project name (projects/<name>) or a path")
    ap.add_argument("--changed", action="store_true",
                    help="only modules changed in the working tree vs HEAD (git)")
    ap.add_argument("--since", metavar="REF",
                    help="only modules changed since a git ref (e.g. main, v1.0, HEAD~3)")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    from scripts.core import discover as disc

    adapter = registry.get_adapter(cfg.run.adapter)
    root = disc.resolve_project_root(a.project, sibling_base=_PROJECT_ROOT.parent)
    if not root.is_dir():
        raise SystemExit(f"Project not found: {a.project} (looked under {_PROJECT_ROOT.parent})")

    only = scope = None
    if a.changed or a.since:
        only = disc.git_changed_py(root, since=a.since)
        if only is None:
            raise SystemExit(f"Not a git work tree (or git unavailable): {root}")
        scope = f"changed since {a.since}" if a.since else "changed vs HEAD"
        if not only:
            print(disc.format_report(root, [], scope=scope) + "\n(no changed Python files)")
            return 0
    reports = disc.discover(root, adapter, only=only)
    print(disc.format_report(root, reports, scope=scope))
    return 0


def _quality_cmd(rest: list[str]) -> int:
    """`quality [--manifest p] [--update-baseline] [--tol N]` — run the curated quality gate (P5).

    Exit 0 = pass (or baseline just set), 5 = a metric regressed vs the baseline.
    """
    ap = argparse.ArgumentParser(prog="main.py quality")
    ap.add_argument("--manifest", help="path to a quality_targets.toml (default: benchmark/)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="store the current panel as the new baseline (and pass)")
    ap.add_argument("--tol", type=float, default=0.0, help="ignore metric moves of this size or less")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    from scripts.core import quality
    manifest = Path(a.manifest) if a.manifest else None
    result = quality.run_quality(cfg, _PROJECT_ROOT, manifest=manifest,
                                 update_baseline=a.update_baseline, tol=a.tol)
    p = result["panel"]
    print(f"\nQuality gate — model {result['model'] or '(default)'}")
    print(f"  coverage={p['coverage']}  pass_rate={p['pass_rate']}  fp_rate={p['fp_rate']}  "
          f"error_rate={p['error_rate']}  smell_density={p['smell_density']}  acceptance={p['acceptance']}")
    print(f"  report: {result['report_md']}")
    if result.get("baseline_updated"):
        print("  baseline updated.")
        return EXIT_OK
    if not result["baseline_compared"]:
        print("  no baseline yet — run with --update-baseline to set one.")
        return EXIT_OK
    if result["gate_pass"]:
        print("  PASS — no regression vs baseline.")
        return EXIT_OK
    print(f"  REGRESSION on: {', '.join(result['comparison']['regressed'])}")
    return EXIT_QUALITY


def _detect_cmd(rest: list[str]) -> int:
    """`detect [...]` — bug-detection (kill-rate) eval: prove the generated tests CATCH bugs.

    Generates suites from CORRECT code (QuixBugs verified pairs + in-repo AST mutants), then re-runs
    them against the buggy versions and reports the fraction caught. Optional ablation shows whether
    cut-source/golden earn their kill rate. Exit 0 = pass (or baseline set), 7 = a kill-rate regressed.
    """
    ap = argparse.ArgumentParser(prog="main.py detect")
    ap.add_argument("--corpus", choices=["both", "quixbugs", "mutation"],
                    help="which corpora to run (default: from [detection].corpus)")
    ap.add_argument("--subset", type=int, help="max QuixBugs programs (bounds token spend)")
    ap.add_argument("--full", action="store_true", help="use the whole QuixBugs corpus (no subset)")
    ap.add_argument("--ablation", dest="ablation", action="store_true", default=None,
                    help="run the feature ablation (default: from config)")
    ap.add_argument("--no-ablation", dest="ablation", action="store_false",
                    help="skip the ablation (mutation corpus runs the full variant only)")
    ap.add_argument("--no-clone", action="store_true",
                    help="don't clone QuixBugs if absent (use only what's cached / mutation corpus)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="store the current kill-rate panel as the new baseline (and pass)")
    ap.add_argument("--tol", type=float, default=0.0, help="ignore kill-rate moves of this size or less")
    ap.add_argument("--manifest", help="path to a detection_targets.toml (default: benchmark/)")
    ap.add_argument("--max-mutants", type=int, help="per-target mutant cap (default: from config)")
    ap.add_argument("--feedback", action="store_true",
                    help="Approach 1: run the coverage-feedback loop for every generation in this eval "
                         "(measure the kill-rate lift vs a baseline run without it; spends extra tokens)")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    d = cfg.detection
    from scripts.core import detection

    subset = None if a.full else (a.subset if a.subset is not None else d.subset_size)
    result = detection.run_detection(
        cfg, _PROJECT_ROOT,
        manifest=Path(a.manifest) if a.manifest else None,
        subset=subset, corpus=a.corpus or d.corpus,
        ablation=d.ablation if a.ablation is None else a.ablation,
        update_baseline=a.update_baseline, tol=a.tol, clone=not a.no_clone,
        max_mutants=a.max_mutants if a.max_mutants is not None else d.max_mutants,
        seed=d.mutation_seed, quixbugs_url=d.quixbugs_url, feedback=a.feedback,
    )
    q = result["quixbugs"]
    print(f"\nBug-detection eval — model {result['model'] or '(default)'}")
    if q.get("available"):
        print(f"  QuixBugs: {q['killed']}/{q['n']} bugs killed  (kill rate {q['kill_rate']})")
    for name, v in result["mutation"].get("variants", {}).items():
        print(f"  mutation[{name}]: {v['killed']}/{v['mutants']} killed  (kill rate {v['kill_rate']})")
    if result["ablation_deltas"]:
        deltas = "  ".join(f"{k}={v:+.3f}" for k, v in result["ablation_deltas"].items())
        print(f"  feature contribution: {deltas}")
    print(f"  tokens: {result['tokens']['in']}+{result['tokens']['out']}")
    print(f"  report: {result['report_json']}")
    if result.get("baseline_updated"):
        print("  baseline updated.")
        return EXIT_OK
    if not result["baseline_compared"]:
        print("  no baseline yet — run with --update-baseline to set one.")
        return EXIT_OK
    if result["gate_pass"]:
        print("  PASS — no kill-rate regression vs baseline.")
        return EXIT_OK
    print(f"  REGRESSION on: {', '.join(result['comparison']['regressed'])}")
    return EXIT_DETECTION


def _holdout_cmd(rest: list[str]) -> int:
    """`holdout [...]` — the LOCKED anti-overfit run on held-out HumanEval code (run ONCE).

    Generates suites from held-out reference solutions, seeds bugs with our AST mutator, and reports
    a single generalization kill rate. Deliberately separate from `detect`: it writes ONLY to
    benchmark/eval/holdout/ and never touches detection_baseline.json or EVAL_DETECTION.md (the dev
    series), so this corpus can never become a development target. Always exits 0 — it is not gated.
    """
    ap = argparse.ArgumentParser(prog="main.py holdout")
    ap.add_argument("--subset", type=int, help="max HumanEval problems (bounds token spend; default: all)")
    ap.add_argument("--max-mutants", type=int, help="per-problem mutant cap (default: from [detection])")
    ap.add_argument("--no-clone", action="store_true",
                    help="don't clone HumanEval if absent (use only what's cached)")
    ap.add_argument("--feedback", action="store_true",
                    help="also run the Approach-1 coverage-feedback loop (measures A1 generalization)")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    d = cfg.detection
    from scripts.core import detection

    result = detection.run_holdout(
        cfg, _PROJECT_ROOT,
        subset=a.subset, clone=not a.no_clone,
        max_mutants=a.max_mutants if a.max_mutants is not None else d.max_mutants,
        seed=d.mutation_seed, humaneval_url=d.humaneval_url, feedback=a.feedback,
    )
    he = result["humaneval"]
    print(f"\nHumanEval HOLDOUT (locked) — model {result['model'] or '(default)'}"
          f"{'  [+feedback]' if result['feedback'] else ''}")
    if not he.get("available"):
        print("  corpus unavailable (clone failed or --no-clone with no cache).")
        print(f"  report: {result['report_json']}")
        return EXIT_OK
    print(f"  kill rate: {he['kill_rate']}  ({he['killed']}/{he['mutants']} non-equivalent mutants killed)")
    print(f"  problems: {he['problems']}  ·  with runnable suite: {he['problems_with_green']}"
          f"  ·  equivalent excluded: {he['equivalent']}")
    print(f"  tokens: {result['tokens']['in']}+{result['tokens']['out']}")
    print(f"  report: {result['report_json']}")
    return EXIT_OK


def _sweep_cmd(rest: list[str]) -> int:
    """`sweep <project> [--since REF] [--all]` — "test the diff": generate tests for the git-changed
    (default) testable modules of a project, under the per-sweep token cap (P4-3)."""
    ap = argparse.ArgumentParser(prog="main.py sweep")
    ap.add_argument("project", help="sibling project name (projects/<name>) or a path")
    ap.add_argument("--since", metavar="REF", help="changed since a git ref (default: working tree vs HEAD)")
    ap.add_argument("--all", action="store_true", help="sweep every testable module, not just git-changed")
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    cfg = load_config(a.config)
    from scripts.core import budget as budget_mod
    from scripts.core import discover as disc

    adapter = registry.get_adapter(cfg.run.adapter)
    root = disc.resolve_project_root(a.project, sibling_base=_PROJECT_ROOT.parent)
    if not root.is_dir():
        raise SystemExit(f"Project not found: {a.project} (looked under {_PROJECT_ROOT.parent})")

    only = None
    if not a.all:
        only = disc.git_changed_py(root, since=a.since)
        if only is None:
            raise SystemExit(f"Not a git work tree: {root}. Use --all to sweep everything.")
        if not only:
            print("No changed Python files — nothing to sweep.")
            return EXIT_OK
    reports = [r for r in disc.discover(root, adapter, only=only) if r.testable]
    if not reports:
        print("No testable-now modules in scope.")
        return EXIT_OK

    bud = build_budget(cfg)
    total_in = total_out = 0
    rows: list[str] = []
    rc = EXIT_OK
    for r in reports:
        if bud.max_tokens_per_sweep > 0 and (total_in + total_out) >= bud.max_tokens_per_sweep:
            note = (f"sweep cap {bud.max_tokens_per_sweep} reached after "
                    f"{total_in + total_out} tokens — stopping before {r.rel}.")
            (logger.error if bud.on_over == "abort" else logger.warning)(note)
            rc = EXIT_BUDGET if bud.on_over == "abort" else rc
            rows.append(f"  (stopped: budget cap reached before {r.rel})")
            break
        try:
            rep = run_pipeline(cfg, RunRequest(target=r.target_path(root),
                                               selector=",".join(r.testable)))
        except (TargetError, LLMError, BudgetError) as exc:
            rows.append(f"  {r.rel}: skipped ({type(exc).__name__})")
            continue
        total_in += rep.tokens_in
        total_out += rep.tokens_out
        cached = " [cached]" if rep.tokens_in == 0 and rep.tokens_out == 0 else ""
        rows.append(f"  {r.rel}: {rep.passed}/{rep.generated} pass · {rep.failed} fail · "
                    f"{rep.errored} err · {rep.tokens_in + rep.tokens_out} tok{cached}")

    spend = budget_mod.cost(total_in, total_out, bud)
    print("\nSweep — test the diff:")
    print("\n".join(rows))
    print(f"  TOTAL spend: {total_in}+{total_out} tokens"
          + (f" (~${spend:.4f})" if spend else ""))
    return rc


_SUBCOMMANDS = {
    "accept": _accept_cmd, "promote": _promote_cmd, "discover": _discover_cmd,
    "quality": _quality_cmd, "sweep": _sweep_cmd, "detect": _detect_cmd, "holdout": _holdout_cmd,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _SUBCOMMANDS:
        return _SUBCOMMANDS[argv[0]](argv[1:])

    args = _parse_args(argv)

    if args.smoke:
        from scripts.llm_client import smoke_test
        try:
            reply = smoke_test(model=args.model)
            logger.info("LLM smoke OK — reply: %r", reply.strip())
            return 0
        except Exception:
            logger.exception("LLM smoke test FAILED.")
            return 1

    try:
        cfg = load_config(args.config)
        report = run_pipeline(cfg, RunRequest.from_namespace(args))
    except TargetError as exc:
        logger.error("Target skipped: %s", exc)        # exit 3 — not a tool bug
        return EXIT_TARGET
    except LLMError as exc:
        logger.error("Generation aborted: %s", exc)     # exit 4 — never half-generated
        return EXIT_LLM
    except BudgetError as exc:
        logger.error("Budget guardrail: %s", exc)        # exit 6 — deliberate over-cap stop
        return EXIT_BUDGET
    except Exception:
        logger.exception("Pipeline failed.")
        return EXIT_INTERNAL

    for c in report.caveats:
        print(f"\n⚠️  {c}")
    spend = (f"  spend:  {report.tokens_in}+{report.tokens_out} tokens"
             + (f" (~${report.cost_est:.4f})" if report.cost_est else "")
             + (" [cached: 0 this run]" if report.tokens_in == 0 and report.tokens_out == 0 else ""))
    fb_line = (f"\n  feedback: {report.feedback_rounds} round(s), +{report.feedback_added} scenario(s)"
               if report.feedback_rounds else "")
    print(
        f"\n✓ {report.passed} passed · {report.failed} failed · {report.errored} error "
        f"/ {report.generated} generated\n"
        f"  tests:  {report.test_file}\n"
        f"  report: {report.report_file}\n"
        f"{spend}{fb_line}"
    )
    return EXIT_OK
