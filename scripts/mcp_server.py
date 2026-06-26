"""AI Test Pilot — MCP server (stdio, FastMCP).

Exposes the engine as MCP tools so it's drivable from Claude Code on ANY project. Built on the
official `mcp` SDK's FastMCP (stdio transport); tool schemas are derived from the type hints +
docstrings below. Reuses the full pipeline via `run_pipeline`, so there is zero logic duplication.

Tools (ARCHITECTURE §7): introspect · generate_tests · triage_failures · run_metrics · accept_run.
Human-in-the-loop boundary: tools write to `scripts/outputs/` ONLY — never into a target repo.

stdout is reserved for the JSON-RPC transport; the shared logger writes to stderr, and tool bodies
run under a stdout→stderr redirect so a stray print can't corrupt the protocol stream.

Register in Claude Code (settings → MCP servers):
  "ai-test-pilot": {
    "command": "python",
    "args": ["C:\\\\...\\\\projects\\\\ai-test-pilot\\\\scripts\\\\mcp_server.py"]
  }
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# Make the project importable as `scripts` ahead of any site-packages shadow.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

from scripts.config import load_config
from scripts.core import ledger, registry
from scripts.core.models import RunRequest, TargetRef
from scripts.logger import get_logger

logger = get_logger("mcp_server")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

mcp = FastMCP("ai-test-pilot")


@contextlib.contextmanager
def _quiet():
    """Keep stdout clean for the transport while a tool runs."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# ── tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
def introspect(target: str, adapter: str = "", selector: str = "") -> str:
    """Preview the deterministic test contract for a Python module (units, constructible types,
    unresolved typed params). Cheap — no LLM tokens. `selector` scopes to comma-separated names."""
    with _quiet():
        cfg = load_config(None)
        adapter_name = adapter or cfg.run.adapter
        ad = registry.get_adapter(adapter_name)
        c = ad.introspect(TargetRef(adapter=adapter_name, locator=target, selector=selector or None))
        out = [f"{len(c.units)} unit(s) in {c.module}:"]
        for u in c.units:
            unresolved = f"  UNRESOLVED: {', '.join(u.complex_params)}" if u.complex_params else ""
            out.append(f"- {u.name}{u.signature or '()'} -> {u.returns or '?'}  "
                       f"[pure={u.is_pure} clock={u.reads_clock}]{unresolved}")
        if c.types:
            out.append("Constructible types: " + ", ".join(c.types))
        return "\n".join(out)


@mcp.tool()
def generate_tests(target: str, adapter: str = "", selector: str = "", count: int = 0,
                   golden: bool = False, fixtures: bool = False, fixture_domain: str = "",
                   no_context: bool = False) -> str:
    """Full pipeline: introspect → generate (LLM) → materialize → run → triage → record. Proposes
    pytest files into scripts/outputs/ (never the target repo). `golden` locks assertions to real
    results; `fixtures`+`fixture_domain` seed inputs from synthetic-data-factory."""
    with _quiet():
        from scripts.pipeline import run_pipeline
        cfg = load_config(None)
        req = RunRequest(
            target=target, adapter=adapter or None, selector=selector or None,
            count=count or None, fixtures=fixtures, fixture_domain=fixture_domain or None,
            no_context=no_context, golden=golden,
        )
        r = run_pipeline(cfg, req)
        lines = [
            f"run {r.run_id}: {r.passed} passed / {r.failed} failed / {r.errored} error "
            f"of {r.generated} generated",
            f"tests:     {r.test_file}",
            f"scenarios: {r.scenarios_file}",
            f"report:    {r.report_file}",
        ]
        if r.fixture_file:
            lines.append(f"fixture:   {r.fixture_file}")
        lines += [f"caveat: {c}" for c in r.caveats]
        lines.append(f"\nReview the proposed tests, then call accept_run(run_id='{r.run_id}', kept=N).")
        return "\n".join(lines)


@mcp.tool()
def triage_failures(run_id: str) -> str:
    """Return the failure-triage section (verdicts + evidence) of a prior run's report."""
    with _quiet():
        cfg = load_config(None)
        report = _PROJECT_ROOT / cfg.run.output_dir / "reports" / f"report_{run_id}.md"
        if not report.is_file():
            return f"No report found for run_id {run_id} ({report})."
        text = report.read_text(encoding="utf-8")
        marker = "## Failure triage"
        return text[text.index(marker):] if marker in text else text


@mcp.tool()
def run_metrics(adapter: str = "", target: str = "") -> str:
    """Ledger stats: prompt-version acceptance per adapter, and acceptance for a target if given."""
    with _quiet():
        cfg = load_config(None)
        ledger_path = _PROJECT_ROOT / cfg.ledger.path
        adapter_name = adapter or cfg.run.adapter
        out = [f"Ledger: {ledger_path}"]
        stats = ledger.prompt_version_stats(adapter_name, ledger_path)
        if stats:
            out.append("Prompt-version acceptance (accepted runs):")
            out += [f"  {v}: {acc:.0%} (n={n})" for v, acc, n in stats]
        else:
            out.append("No accepted runs yet — call accept_run to start tracking acceptance.")
        if target:
            ta = ledger.target_acceptance(adapter_name, target, ledger_path)
            out.append(f"Target {target}: "
                       + (f"{ta[0]:.0%} acceptance (n={ta[1]})" if ta else "no accepted runs yet"))
        return "\n".join(out)


@mcp.tool()
def accept_run(run_id: str, kept: int) -> str:
    """Record how many proposed tests you kept for a run (backfills acceptance for propose-tuning)."""
    with _quiet():
        cfg = load_config(None)
        ledger_path = _PROJECT_ROOT / cfg.ledger.path
        ok = ledger.backfill_acceptance(run_id, int(kept), ledger_path)
        return (f"Recorded: run {run_id} kept {kept}." if ok
                else f"run_id {run_id} not found in the ledger.")


def main() -> int:
    logger.info("AI Test Pilot MCP server (FastMCP / stdio) starting.")
    try:
        mcp.run(transport="stdio")
        return 0
    except Exception:                       # a launching MCP client can then detect the failure
        logger.exception("MCP server crashed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
