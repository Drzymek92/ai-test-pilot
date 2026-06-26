"""Run-report rendering — the Markdown summary of a pipeline run.

Pure formatting, no orchestration: it takes the typed run objects and writes a human-readable report.
Split out of the CLI/pipeline so the pipeline owns orchestration and this module owns presentation
(MOD: one concern per module). Detection/holdout/quality keep their own eval-specific writers.
"""
from __future__ import annotations

from pathlib import Path

from scripts.core.models import RunReport, RunResult, ScenarioSet


def write_report(
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
    if report.feedback_rounds:
        lines.append(f"- **Coverage feedback (Approach 1):** {report.feedback_rounds} round(s), "
                     f"+{report.feedback_added} scenario(s) targeting uncovered lines")
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
