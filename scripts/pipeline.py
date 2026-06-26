"""AI Test Pilot — the pipeline (shared core, driven by a typed RunRequest).

`run_pipeline` runs the seven stages: introspect → generate (LLM→JSON) → materialize → run → triage →
record → tune. Stages 1/3/4/6 cost zero tokens; stage 2 is a single batched LLM call; stage 5 calls
the LLM only for ambiguous failures. Every driver — the CLI (`scripts/cli.py`), `sweep`, `quality`,
`detection`, and the MCP server — calls this one function with a `RunRequest`.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Make the project root importable as `scripts`, ahead of any site-packages `scripts`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.config import AppConfig, autoscale_count
from scripts.core import registry
from scripts.core.budget import Budget
from scripts.core.errors import TargetError
from scripts.core.generate import generate_scenarios
from scripts.core.materialize import materialize
from scripts.core.models import RunReport, RunRequest, RunResult, ScenarioSet, TargetRef
from scripts.core.report import write_report
from scripts.core.runner import run_tests
from scripts.logger import get_logger

logger = get_logger("pipeline")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_budget(cfg: AppConfig) -> Budget:
    b = cfg.budget
    return Budget(
        max_tokens_per_run=b.max_tokens_per_run, max_tokens_per_sweep=b.max_tokens_per_sweep,
        on_over=b.on_over, price_in=b.price_per_mtok_in, price_out=b.price_per_mtok_out,
        default_out_per_scenario=b.default_out_per_scenario,
        ledger_path=_PROJECT_ROOT / cfg.ledger.path,
    )


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_pipeline(cfg: AppConfig, args: RunRequest) -> RunReport:
    adapter_name = args.adapter or cfg.run.adapter
    adapter = registry.get_adapter(adapter_name)

    if not args.target:
        raise SystemExit("--target is required (path to the module under test).")

    ref = TargetRef(adapter=adapter_name, locator=args.target, selector=args.selector)
    out_base = (_PROJECT_ROOT / cfg.run.output_dir)
    ts = _timestamp()

    # 1 — INTROSPECT (deterministic, zero tokens). A malformed/missing target is a clean
    # 'skip with reason' (exit 3), not a crash — important when sweeping a whole project (P2).
    introspect_kwargs = {}
    if adapter_name == "python_pytest" and cfg.typed_inputs.builders:
        introspect_kwargs["builders"] = dict(cfg.typed_inputs.builders)   # A3(c): user builder hatch
    try:
        contract = adapter.introspect(ref, **introspect_kwargs)
    except (SyntaxError, FileNotFoundError, UnicodeDecodeError) as exc:
        raise TargetError(f"cannot introspect {args.target}: {type(exc).__name__}: {exc}") from exc
    logger.info("Introspected %d unit(s) from %s.", len(contract.units), args.target)

    # 1.1 — web served mode: tests get a localhost origin + base_url/auth_state fixtures.
    if args.serve:
        target_path = Path(args.target)
        contract.serve_dir = str(target_path.resolve().parent)
        contract.page_path = "/" + target_path.name
        contract.module = "http://localhost:3000" + contract.page_path  # TS artifact BASE_URL
        try:
            page_text = target_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            page_text = ""
        feats = []
        if "fetch(" in page_text or "/api/" in page_text:
            feats.append("api")
        if "WebSocket" in page_text:
            feats.append("websocket")
        contract.page_features = feats
        logger.info("Served mode: serving %s, page %s, features=%s",
                    contract.serve_dir, contract.page_path, feats or "none")

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

    # 1.8 — AUTO TUNING (opt-in): inject previously-accepted scenarios as few-shot exemplars.
    # Deterministic (reads persisted scenario JSON) — no extra LLM call, just a few prompt tokens.
    fewshot_text = ""
    fs_note: str | None = None
    if cfg.tuning.mode == "auto":
        fewshot_text, fs_note = tuning.fewshot_block(
            adapter=adapter_name, target=args.target, ledger_path=ledger_path, out_base=out_base,
            min_rate=cfg.tuning.fewshot_min_rate, max_examples=cfg.tuning.fewshot_max_examples,
            max_chars=cfg.tuning.fewshot_max_chars)
        if fs_note:
            logger.info(fs_note)

    # 2 — GENERATE (one LLM call, schema-validated; cached for reproducibility — P1)
    # Knobs shared by the first call and any feedback round (Approach 1) — fixtures/few-shot are
    # round-0 only, so they stay out of this shared dict.
    base_gen_kwargs = dict(
        adapter=adapter,
        model=args.model,
        temperature=cfg.generation.temperature,
        prompt_version=prompt_version,
        repair_retries=cfg.generation.repair_retries,
        context_block=context_block_text,
        cache_dir=out_base / "cache",
        use_cache=cfg.generation.cache and not args.no_cache,
        refresh_cache=args.refresh_cache,
        llm_timeout=cfg.generation.llm_timeout,
        llm_retries=cfg.generation.llm_retries,
        include_source=cfg.generation.cut_source and not args.no_cut_source,
        source_max_chars=cfg.generation.cut_source_max_chars,
        budget=build_budget(cfg),
    )
    # Scale the scenario budget with the number of units so multi-function targets aren't under-tested
    # (a fixed 6 gives a 10-function module 0.6 scenarios/fn; --count still overrides).
    effective_count = autoscale_count(cfg.run, len(contract.units), args.count)
    if effective_count != cfg.run.scenario_count:
        logger.info("Scenario budget: %d (%d unit(s) x %d/unit, capped %d).",
                    effective_count, len(contract.units), cfg.run.scenarios_per_unit, cfg.run.scenario_max)
    scenario_set = generate_scenarios(
        contract,
        count=effective_count,
        fixture_block=fixture_block,
        fewshot_block=fewshot_text,
        **base_gen_kwargs,
    )

    # 2.5 — bind real factory file contents into any `from_fixture` tmp_files (deterministic)
    if bundle is not None:
        from scripts.core.fixtures import bind_fixture_files
        bind_fixture_files(scenario_set, bundle)

    # 2.55 — FEEDBACK (Approach 1, opt-in): measure the suite's coverage gap on the target and
    # regenerate ADDITIONAL scenarios aimed at the uncovered lines. Coverage-only signal (never any
    # bug/mutant info) → can't overfit a held-out bug set. Python adapter only; capped at max_rounds.
    fb = {"rounds": 0, "added": 0}
    fb_on = (cfg.feedback.enabled or args.feedback) and not args.no_feedback
    is_python = getattr(adapter, "prompt_kind", "python") == "python"
    if fb_on and is_python and contract.units:
        from functools import partial

        from scripts.core import feedback as feedback_mod

        def _feedback_gen(block: str, count: int) -> ScenarioSet:
            return generate_scenarios(contract, count=count, feedback_block=block, **base_gen_kwargs)

        rebind = None
        if bundle is not None:
            from scripts.core.fixtures import bind_fixture_files as _bind
            rebind = lambda ss: _bind(ss, bundle)  # noqa: E731
        fb = feedback_mod.run_feedback(
            adapter=adapter, contract=contract, scenario_set=scenario_set,
            target_file=Path(args.target), project_root=_PROJECT_ROOT,
            probe_dir=out_base / "_feedback", generate_fn=_feedback_gen,
            max_rounds=cfg.feedback.max_rounds, count=cfg.feedback.count,
            min_uncovered=cfg.feedback.min_uncovered_lines,
            max_uncovered_shown=cfg.feedback.max_uncovered_shown, rebind=rebind,
            measure_fn=partial(feedback_mod.measure_uncovered, timeout=cfg.feedback.probe_timeout),
        )
        scenario_set.tokens_in += fb["tokens_in"]
        scenario_set.tokens_out += fb["tokens_out"]

    # 2.6 — GOLDEN (optional): run each call once and lock assertions to the captured result
    if args.golden or cfg.generation.golden:
        from scripts.core.golden import apply_goldens, capture_goldens
        captures = capture_goldens(adapter, contract, scenario_set,
                                   cwd=_PROJECT_ROOT, out_dir=out_base / "tests")
        apply_goldens(scenario_set, captures)

    # web: force the asyncio Playwright variant for every scenario when requested
    if args.web_async:
        for s in scenario_set.scenarios:
            if "async" not in s.tags:
                s.tags.append("async")

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
        results = run_tests(adapter, test_path, scenario_set, cwd=_PROJECT_ROOT,
                            per_test_timeout=cfg.generation.per_test_timeout)

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
        tokens_in=scenario_set.tokens_in,
        tokens_out=scenario_set.tokens_out,
        feedback_rounds=fb["rounds"],
        feedback_added=fb["added"],
    )

    # 6 — RECORD to the ledger (self-tracking)
    from scripts.core import budget as budget_mod
    from scripts.core import ledger
    from scripts.core.models import RunRecord
    cost_est = budget_mod.cost(scenario_set.tokens_in, scenario_set.tokens_out, build_budget(cfg))
    report.cost_est = cost_est
    ledger.append(RunRecord(
        run_id=report.run_id, ts=report.ts, adapter=adapter_name, target=args.target,
        model=scenario_set.model, prompt_version=scenario_set.prompt_version,
        generated=report.generated, passed=report.passed, failed=report.failed + report.errored,
        triage=triage_counts,
        tokens_in=scenario_set.tokens_in, tokens_out=scenario_set.tokens_out, cost_est=cost_est,
    ), ledger_path)

    # 7 — TUNE (propose): deterministic suggestions for the report
    tuning_notes = []
    if cfg.tuning.mode != "off":
        tuning_notes = tuning.suggestions(
            adapter=adapter_name, target=args.target, prompt_version=scenario_set.prompt_version,
            triage_counts=triage_counts, ledger_path=ledger_path,
            min_runs=cfg.tuning.min_runs_for_selection, escalate_below=cfg.tuning.escalate_below_accept)
        if fs_note:
            tuning_notes = [f"{fs_note} — applied to this run."] + tuning_notes

    report.report_file = str(write_report(
        report, scenario_set, results, out_base / "reports",
        verdicts=verdicts, tuning_notes=tuning_notes))
    return report
