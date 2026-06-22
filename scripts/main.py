"""AI Test Pilot — CLI entry point.

Pipeline: introspect → generate (LLM→JSON) → materialize → run → triage → record.
Stages 1/3/4 cost zero tokens; stage 2 is a single batched LLM call.

Examples:
    python scripts/main.py --target path/to/module.py --selector func_a,func_b
    python scripts/main.py --target path/to/module.py --golden
    python scripts/main.py --adapter web_playwright --target path/to/page.html
    python scripts/main.py --smoke
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Make the project root importable as `scripts`, ahead of any site-packages `scripts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import AppConfig, load_config
from scripts.core import registry
from scripts.core.generate import generate_scenarios
from scripts.core.materialize import materialize
from scripts.core.models import RunReport, RunResult, ScenarioSet, TargetRef
from scripts.core.runner import run_tests
from scripts.logger import get_logger

logger = get_logger("main")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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
    p.add_argument("--golden", action="store_true",
                   help="characterization mode: run each call once and lock assertions to the result")
    return p.parse_args(argv)


def _write_report(
    report: RunReport, scenario_set: ScenarioSet, results: list[RunResult], out_dir: Path,
    verdicts: list | None = None, tuning_notes: list[str] | None = None,
) -> Path:
    by_id = {r.scenario_id: r for r in results}
    lines = [
        f"# AI Test Pilot — run {report.run_id}",
        "",
        f"- **Target:** `{report.target}`",
        f"- **Adapter:** {report.adapter}",
        f"- **Model / prompt:** {report.model or '(default)'} / {report.prompt_version}",
        f"- **When:** {report.ts:%Y-%m-%d %H:%M:%S}",
        f"- **Result:** {report.passed} passed · {report.failed} failed · "
        f"{report.errored} error / {report.generated} generated",
        f"- **Test file:** `{report.test_file}`",
        f"- **Scenarios:** `{report.scenarios_file}`",
    ]
    if report.fixture_file:
        lines.append(f"- **Fixture (synthetic-data-factory):** `{report.fixture_file}`")
    if report.context_file:
        lines.append(f"- **Domain context:** `{report.context_file}`")
    for c in report.caveats:
        lines.append(f"- ⚠️ **Caveat:** {c}")
    lines += [
        "",
        "## Scenarios",
        "",
        "| id | unit | tags | fixture | status | signal |",
        "|---|---|---|---|---|---|",
    ]
    for s in scenario_set.scenarios:
        r = by_id.get(s.id)
        status = r.status if r else "—"
        signal = (r.signal if r else "") or ""
        tags = ", ".join(s.tags)
        lines.append(f"| {s.id} | {s.unit} | {tags} | {s.fixture or ''} | {status} | {signal} |")

    verdict_by_id = {v.scenario_id: v for v in (verdicts or [])}
    failures = [r for r in results if r.status in ("failed", "error")]
    if failures:
        lines += ["", "## Failure triage (real_bug vs. bad_scenario vs. flaky vs. env_issue)", ""]
        for r in failures:
            v = verdict_by_id.get(r.scenario_id)
            verdict = (f"**{v.verdict}** ({v.confidence:.0%}, {v.source}) — {v.evidence}"
                       if v else "(untriaged)")
            lines += [f"### {r.scenario_id} — {r.status} ({r.signal})", "", f"- Triage: {verdict}"]
            if v and v.suggested_fix:
                lines.append(f"- Suggested fix: {v.suggested_fix}")
            lines += ["", "```", r.captured.strip() or "(no output)", "```", ""]

    if tuning_notes:
        lines += ["", "## Tuning suggestions (propose — not applied automatically)", ""]
        lines += [f"- {n}" for n in tuning_notes]
        lines += ["", "_Backfill acceptance for tuning: `python scripts/main.py accept "
                  f"{report.run_id} --kept N`_"]

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report_{report.run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_pipeline(cfg: AppConfig, args: argparse.Namespace) -> RunReport:
    adapter_name = args.adapter or cfg.run.adapter
    adapter = registry.get_adapter(adapter_name)

    if not args.target:
        raise SystemExit("--target is required (path to the module under test).")

    ref = TargetRef(adapter=adapter_name, locator=args.target, selector=args.selector)
    out_base = (_PROJECT_ROOT / cfg.run.output_dir)
    ts = _timestamp()

    # 1 — INTROSPECT (deterministic, zero tokens)
    contract = adapter.introspect(ref)
    logger.info("Introspected %d unit(s) from %s.", len(contract.units), args.target)

    caveats: list[str] = []
    complex_units = [u for u in contract.units if u.complex_params]
    if complex_units:
        detail = "; ".join(f"{u.name}({', '.join(u.complex_params)})" for u in complex_units)
        msg = ("Target has functions taking COMPLEX TYPED objects this tool cannot yet "
               f"construct: {detail}. Expect few/no runnable tests for these — they need "
               "manual fixtures or typed-input construction (not yet supported).")
        logger.warning(msg)
        caveats.append(msg)

    # 1.5 — FIXTURES (optional): realistic seed data from synthetic-data-factory
    fixture_block = ""
    fixture_path: str | None = None
    bundle = None
    if args.fixtures or cfg.fixtures.enabled:
        from scripts.core.fixtures import generate_fixture, prompt_block
        domain = args.fixture_domain or cfg.fixtures.domain
        if not domain:
            raise SystemExit("--fixtures requires --fixture-domain (or [fixtures].domain in config).")
        bundle = generate_fixture(
            domain=domain,
            rows=args.fixture_rows or cfg.fixtures.rows,
            project_path=cfg.fixtures.project_path,
            out_dir=out_base / "fixtures",
            entity=args.fixture_entity,
        )
        if bundle:
            fixture_block = prompt_block(bundle)
            fixture_path = str(bundle.path)

    # 1.6 — PROJECT CONTEXT (optional): domain semantics for realistic values/assertions
    context_block_text = ""
    context_path: str | None = None
    if cfg.generation.use_context and not args.no_context:
        from scripts.core.context import load_context
        context_block_text, ctx = load_context(
            args.target, explicit=args.context, max_chars=cfg.generation.context_max_chars)
        context_path = str(ctx) if ctx else None

    # 1.7 — resolve prompt version (propose tuning: 'auto' → ledger-best for this adapter)
    ledger_path = _PROJECT_ROOT / cfg.ledger.path
    from scripts.core import tuning
    requested_pv = args.prompt_version or cfg.run.prompt_version
    prompt_version, pv_note = tuning.select_prompt_version(
        adapter_name, requested_pv, ledger_path=ledger_path,
        min_runs=cfg.tuning.min_runs_for_selection)
    if pv_note:
        logger.info(pv_note)

    # 2 — GENERATE (one LLM call, schema-validated)
    scenario_set = generate_scenarios(
        contract,
        adapter=adapter,
        count=args.count or cfg.run.scenario_count,
        model=args.model,
        temperature=cfg.generation.temperature,
        prompt_version=prompt_version,
        repair_retries=cfg.generation.repair_retries,
        fixture_block=fixture_block,
        context_block=context_block_text,
    )

    # 2.5 — bind real factory file contents into any `from_fixture` tmp_files (deterministic)
    if bundle is not None:
        from scripts.core.fixtures import bind_fixture_files
        bind_fixture_files(scenario_set, bundle)

    # 2.6 — GOLDEN (optional): run each call once and lock assertions to the captured result
    if args.golden or cfg.generation.golden:
        from scripts.core.golden import apply_goldens, capture_goldens
        captures = capture_goldens(adapter, contract, scenario_set,
                                   cwd=_PROJECT_ROOT, out_dir=out_base / "tests")
        apply_goldens(scenario_set, captures)

    # persist scenarios JSON
    scen_path = out_base / "scenarios" / f"scenarios_{ts}.json"
    scen_path.parent.mkdir(parents=True, exist_ok=True)
    scen_path.write_text(scenario_set.model_dump_json(indent=2), encoding="utf-8")

    # 3 — MATERIALIZE (deterministic)
    mod_stem = Path(args.target).stem
    test_path = out_base / "tests" / f"test_{mod_stem}_{ts}.py"
    materialize(adapter, contract, scenario_set, test_path)

    # 4 — RUN (deterministic) — run from this project root so `pytest` + sys.path bootstrap work
    results: list[RunResult] = []
    if not args.no_run:
        results = run_tests(adapter, test_path, scenario_set, cwd=_PROJECT_ROOT)

    # 5 — TRIAGE (deterministic table + LLM only for ambiguous failures)
    verdicts: list = []
    if results and cfg.triage.enabled and (sum(r.status != "passed" for r in results) > 0):
        from scripts.core.triage import triage
        verdicts = triage(results, scenario_set, contract,
                          llm_for_ambiguous=cfg.triage.llm_for_ambiguous, model=args.model)
    triage_counts = {}
    for v in verdicts:
        triage_counts[v.verdict] = triage_counts.get(v.verdict, 0) + 1

    report = RunReport(
        run_id=ts,
        ts=datetime.now(),
        adapter=adapter_name,
        target=args.target,
        model=scenario_set.model,
        prompt_version=scenario_set.prompt_version,
        generated=len(scenario_set.scenarios),
        passed=sum(r.status == "passed" for r in results),
        failed=sum(r.status == "failed" for r in results),
        errored=sum(r.status == "error" for r in results),
        test_file=str(test_path),
        scenarios_file=str(scen_path),
        fixture_file=fixture_path,
        context_file=context_path,
        caveats=caveats,
    )

    # 6 — RECORD to the ledger (self-tracking)
    from scripts.core import ledger
    from scripts.core.models import RunRecord
    ledger.append(RunRecord(
        run_id=report.run_id, ts=report.ts, adapter=adapter_name, target=args.target,
        model=scenario_set.model, prompt_version=scenario_set.prompt_version,
        generated=report.generated, passed=report.passed, failed=report.failed + report.errored,
        triage=triage_counts,
    ), ledger_path)

    # 7 — TUNE (propose): deterministic suggestions for the report
    tuning_notes = []
    if cfg.tuning.mode != "off":
        tuning_notes = tuning.suggestions(
            adapter=adapter_name, target=args.target, prompt_version=scenario_set.prompt_version,
            triage_counts=triage_counts, ledger_path=ledger_path,
            min_runs=cfg.tuning.min_runs_for_selection, escalate_below=cfg.tuning.escalate_below_accept)

    report.report_file = str(_write_report(
        report, scenario_set, results, out_base / "reports",
        verdicts=verdicts, tuning_notes=tuning_notes))
    return report


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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "accept":
        return _accept_cmd(argv[1:])

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
        report = run_pipeline(cfg, args)
    except Exception:
        logger.exception("Pipeline failed.")
        return 1

    for c in report.caveats:
        print(f"\n⚠️  {c}")
    print(
        f"\n✓ {report.passed} passed · {report.failed} failed · {report.errored} error "
        f"/ {report.generated} generated\n"
        f"  tests:  {report.test_file}\n"
        f"  report: {report.report_file}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
